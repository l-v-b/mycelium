"""Deterministic dedupe + upsert semantics (no LLM in the path).

Contract (from write_path/dedupe.py, threshold 0.5):
  - write_note upserts by title slug -> same title twice = ONE note, same id.
  - a near-duplicate with a DIFFERENT title returns a Warning pointing at the
    existing note (the write still proceeds — warning, not rejection).
  - a same-title rewrite does NOT warn (it's an intended update).
"""
from __future__ import annotations

import pytest

from .conftest import (
    E2E_TAG,
    E2E_WING,
    commit_timeout_for,
    drawer_is_searchable,
    note_is_queryable,
    parse_id,
    poll_until,
)

pytestmark = pytest.mark.blackbox


def test_same_title_is_idempotent_upsert(client, settings, nonce, track):
    title = f"E2E upsert {nonce}"
    body = f"upsert probe {nonce} concerning echidna scheduling"
    c1 = client.call_raw("write_note", title=title, content=body,
                         intent="e2e upsert 1", tags=[E2E_TAG])
    nid1 = parse_id(c1, "note")
    track.note(nid1)
    assert poll_until(
        lambda: note_is_queryable(client, "echidna scheduling probe", nonce),
        timeout=commit_timeout_for(settings),
    )
    before = client.call_json("status")["index"]["notes"]

    # Rewrite same title with extended content.
    c2 = client.call_raw("write_note", title=title, content=body + " — revised",
                         intent="e2e upsert 2", tags=[E2E_TAG])
    nid2 = parse_id(c2, "note")
    assert nid2 == nid1, "same title must map to the same note id (upsert)"
    # Same-title rewrite must NOT raise a dedupe warning.
    assert "warning" not in c2.lower(), f"same-title rewrite should not warn: {c2}"

    def flat() -> bool:
        return client.call_json("status")["index"]["notes"] == before

    # Count must not grow (it's an update, not a new row). Give the worker time.
    assert poll_until(flat, timeout=commit_timeout_for(settings)) or \
        client.call_json("status")["index"]["notes"] == before, \
        "upsert must not create a second index row"


def test_near_duplicate_different_title_warns(client, settings, nonce, track):
    shared = (
        f"## Context\nThe wombat ingestion pipeline batches records nightly and "
        f"retries on transient failure. Token {nonce}."
    )
    c1 = client.call_raw("write_note", title=f"Wombat ingestion A {nonce}",
                         content=shared, intent="e2e dup base", tags=[E2E_TAG])
    track.note(parse_id(c1, "note"))
    # Dedupe queries ChromaDB, so the first note must be COMMITTED before the
    # second write can see it (matters in team mode).
    assert poll_until(
        lambda: note_is_queryable(client, "wombat ingestion pipeline nightly", nonce),
        timeout=commit_timeout_for(settings),
    ), "base note never committed; cannot test dedupe"

    c2 = client.call_raw("write_note", title=f"Wombat ingestion B {nonce}",
                         content=shared, intent="e2e dup near", tags=[E2E_TAG])
    track.note(parse_id(c2, "note"))
    assert "warning" in c2.lower(), (
        "a near-identical note with a different title must trigger a dedupe "
        f"warning. Got: {c2}"
    )


def test_check_duplicate_tool(client, settings, nonce, track):
    """check_duplicate is a pre-file probe over DRAWERS (content/wing/room),
    not notes. After filing content, checking the same content must flag it."""
    content = f"Numbat caching layer eviction policy probe {nonce}."
    conf = client.call_raw("file", content=content, wing=E2E_WING, room="dup")
    track.drawer(parse_id(conf, "drawer"))
    assert poll_until(
        lambda: drawer_is_searchable(client, "numbat caching eviction policy", nonce),
        timeout=commit_timeout_for(settings),
    ), "filed drawer never indexed; cannot test check_duplicate"

    res = client.call_json("check_duplicate", content=content, wing=E2E_WING, room="dup")
    assert res.get("duplicate") is True, f"check_duplicate missed an obvious dup: {res}"
    assert any(nonce in s.get("text", "") for s in res.get("similar", []))
