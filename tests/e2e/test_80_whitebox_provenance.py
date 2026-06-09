"""Provenance fields land on disk — even though the read path hides them.

Reading the source established that context()/query_notes()/search() never
return `author`, `committed_at`, or `source_intent`. So the ONLY way to verify
those are actually being stamped (the foundation the team-ownership work builds
on) is to look at the file on the pod's volume.

These tests double as the baseline for the ownership/provenance work: when the
read path starts surfacing author, `test_read_path_hides_author` will flip to
xfail and should be updated.
"""
from __future__ import annotations

import pytest

from .conftest import (
    E2E_TAG,
    commit_timeout_for,
    note_is_queryable,
    parse_id,
    poll_until,
)

pytestmark = pytest.mark.whitebox


def test_note_provenance_stamped_on_disk(client, settings, require_whitebox, nonce, track):
    intro = require_whitebox
    intent = f"capturing the e2e provenance probe {nonce}"
    conf = client.call_raw(
        "write_note", title=f"E2E provenance {nonce}",
        content=f"provenance probe {nonce} about bandicoot indexing",
        intent=intent, tags=[E2E_TAG],
    )
    nid = parse_id(conf, "note")
    track.note(nid)

    fm = poll_until(lambda: intro.note_frontmatter(nid) is not None, timeout=20)
    assert fm, "note file never appeared on disk"
    meta = intro.note_frontmatter(nid)

    # author: present and not the unresolved sentinel.
    assert meta.get("author"), "author frontmatter missing"
    assert meta["author"] != "unknown", (
        "author resolved to 'unknown' — set MYCELIUM_AUTHOR (or the future "
        "gateway-derived identity) on the server"
    )
    # source_intent: exactly what we passed.
    assert meta.get("source_intent") == intent, (
        f"source_intent not stamped verbatim: {meta.get('source_intent')!r}"
    )
    # committed_at + created keys exist.
    assert "committed_at" in meta
    assert meta.get("created"), "created timestamp missing"


def test_drawer_provenance_stamped_on_disk(client, require_whitebox, nonce, track):
    intro = require_whitebox
    conf = client.call_raw("file", content=f"provenance drawer {nonce}",
                          wing="_e2e", room="prov", source="e2e probe")
    did = parse_id(conf, "drawer")
    track.drawer(did)
    fm = poll_until(lambda: intro.drawer_frontmatter(did) is not None, timeout=30)
    assert fm, "drawer file never appeared on disk"
    meta = intro.drawer_frontmatter(did)
    assert meta.get("author"), "drawer author frontmatter missing"
    assert meta.get("wing") == "_e2e"
    assert "committed_at" in meta


@pytest.mark.blackbox
@pytest.mark.xfail(
    reason="documents the CURRENT gap: the read path does not surface author. "
    "When the ownership work lands and query_notes returns author, this xpasses "
    "and should be promoted to a positive assertion.",
    strict=False,
)
def test_read_path_hides_author(client, settings, nonce, track):
    conf = client.call_raw(
        "write_note", title=f"E2E author-leak {nonce}",
        content=f"author visibility probe {nonce} about possum routing",
        intent="e2e author visibility", tags=[E2E_TAG],
    )
    track.note(parse_id(conf, "note"))
    assert poll_until(
        lambda: note_is_queryable(client, "possum routing probe", nonce),
        timeout=commit_timeout_for(settings),
    )
    res = client.call_json("query_notes", query="possum routing probe", n_results=10)
    hit = next((n for n in res["notes"] if nonce in n.get("content", "")), None)
    assert hit is not None
    # Currently fails (author absent) -> xfail. Will xpass once surfaced.
    assert "author" in hit, "read path now surfaces author"
