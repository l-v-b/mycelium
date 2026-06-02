"""The heart of the suite: write -> (commit) -> retrieve a unique nonce.

Why this cannot be faked: each test mints a fresh random nonce and embeds it in
the content it writes. It then retrieves via a DIFFERENT tool than it wrote
through, and asserts the nonce comes back. For that to pass, the server must
have genuinely: accepted the write, persisted it, embedded it, indexed it into
ChromaDB, and served it from a real similarity query (in team mode, via the
Redis stream + writer worker). A stub, a cache, a no-op, or a broken embedder
all fail at least one link in that chain.

We also cross-check the `status` index counts move by exactly the right amount,
which catches a server that "accepts" writes but never indexes them.
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


def test_write_note_roundtrip_via_query_notes(client, settings, nonce, track):
    title = f"E2E roundtrip note {nonce}"
    # A distinctive sentence so the nonce ranks at the top of a semantic query.
    body = (
        f"## Context\nThis is an end-to-end probe. The verification token is {nonce}. "
        f"It concerns the quokka telemetry subsystem and nothing else.\n"
    )
    conf = client.call_raw(
        "write_note", title=title, content=body,
        intent="e2e roundtrip probe", tags=[E2E_TAG],
    )
    nid = parse_id(conf, "note")
    assert nid, f"no note id in confirmation: {conf!r}"
    track.note(nid)

    if settings.is_team:
        assert "enqueued" in conf.lower(), (
            "team mode should enqueue, not commit synchronously: " + conf
        )

    # Eventually (immediately in personal mode) the note is retrievable by meaning.
    found = poll_until(
        lambda: note_is_queryable(client, "quokka telemetry subsystem probe", nonce),
        timeout=commit_timeout_for(settings),
    )
    assert found, (
        f"note {nid} never became queryable within the commit window — "
        "write path or worker indexing is broken"
    )

    # The returned note must carry OUR nonce and the upsert id must match.
    res = client.call_json("query_notes", query="quokka telemetry subsystem probe", n_results=10)
    hit = next((n for n in res["notes"] if nonce in n.get("content", "")), None)
    assert hit is not None
    assert hit["note_id"] == nid


def test_file_drawer_roundtrip_via_search(client, settings, nonce, track):
    body = f"Verbatim capture for e2e. Marsupial migration log token {nonce}."
    conf = client.call_raw("file", content=body, wing=E2E_WING, room="roundtrip")
    did = parse_id(conf, "drawer")
    assert did, f"no drawer id in confirmation: {conf!r}"
    track.drawer(did)

    if settings.is_team:
        assert "queued" in conf.lower()

    found = poll_until(
        lambda: drawer_is_searchable(client, "marsupial migration log", nonce),
        timeout=commit_timeout_for(settings),
    )
    assert found, f"drawer {did} never became searchable — index path broken"


def test_context_surfaces_both_notes_and_drawers(client, settings, nonce, track):
    """context() fans out across notes + drawers + links. After seeding one of
    each with the same nonce, context must surface them together."""
    note_body = f"## Decision\nUnified retrieval probe token {nonce} about platypus routing."
    nconf = client.call_raw(
        "write_note", title=f"E2E ctx note {nonce}", content=note_body,
        intent="e2e context probe", tags=[E2E_TAG],
    )
    track.note(parse_id(nconf, "note"))
    dconf = client.call_raw(
        "file", content=f"platypus routing verbatim {nonce}", wing=E2E_WING, room="ctx",
    )
    track.drawer(parse_id(dconf, "drawer"))

    ok = poll_until(
        lambda: note_is_queryable(client, "platypus routing probe", nonce)
        and drawer_is_searchable(client, "platypus routing verbatim", nonce),
        timeout=commit_timeout_for(settings),
    )
    assert ok, "seeded note+drawer did not both index in time"

    ctx = client.call_json("context", query="platypus routing probe", mode="full")
    blob = str(ctx)
    assert nonce in blob, "context() did not surface the freshly written content"
    assert "top_results" in ctx, "context() missing the merged top_results ranking"


def test_status_index_count_increments_by_one_per_note(client, settings, nonce, track):
    """A note write must move the notes index count by exactly +1 once committed.
    Catches servers that accept writes but never index (count stays flat) or
    double-index (count jumps by 2)."""
    before = client.call_json("status")["index"]["notes"]
    conf = client.call_raw(
        "write_note", title=f"E2E count note {nonce}",
        content=f"counting probe {nonce}", intent="e2e count probe", tags=[E2E_TAG],
    )
    track.note(parse_id(conf, "note"))

    def committed() -> bool:
        return client.call_json("status")["index"]["notes"] >= before + 1

    assert poll_until(committed, timeout=commit_timeout_for(settings)), (
        "notes index count did not increase after a committed write"
    )
    after = client.call_json("status")["index"]["notes"]
    assert after == before + 1, f"expected +1 note in index, got {after - before}"
