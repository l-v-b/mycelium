"""Deletes/updates are SYNCHRONOUS even in team mode — and the race that implies.

Verified from source: update_drawer / delete_drawer / delete_note have no team
branch; they mutate ChromaDB directly in-process, unlike write_note/file/
diary_write which go through the queue. The suite pins this real contract so a
future change is noticed, and documents the resurrection race it creates.
"""
from __future__ import annotations

import pytest

from .conftest import (
    E2E_TAG,
    commit_timeout_for,
    drawer_is_searchable,
    note_is_queryable,
    parse_id,
    poll_until,
)

pytestmark = pytest.mark.blackbox


def test_delete_drawer_is_immediate(client, settings, nonce, track):
    """A committed drawer disappears from search promptly after delete — no
    multi-minute queue lag (deletes don't go through the worker)."""
    conf = client.call_raw("file", content=f"sync-delete verbatim {nonce}",
                          wing="_e2e", room="del")
    did = parse_id(conf, "drawer")
    assert poll_until(
        lambda: drawer_is_searchable(client, "sync-delete verbatim", nonce),
        timeout=commit_timeout_for(settings),
    ), "drawer never indexed; cannot test delete"

    out = client.call_raw("delete_drawer", drawer_id=did)
    assert "deleted" in out.lower()

    # Synchronous: gone within a few seconds, not the async commit window.
    gone = poll_until(
        lambda: not drawer_is_searchable(client, "sync-delete verbatim", nonce),
        timeout=15,
    )
    assert gone, "delete_drawer should remove from the index synchronously"


@pytest.mark.known_race
@pytest.mark.xfail(
    reason="sync delete races async commit: deleting a still-pending note lets "
    "the worker resurrect it. Tracked in the ownership/provenance TODO note. "
    "Flips to xpass if the delete path learns to tombstone pending writes.",
    strict=False,
)
def test_deleting_pending_note_does_not_resurrect(client, settings, nonce, track):
    if not settings.is_team:
        pytest.skip("resurrection race only manifests in team mode")

    # Enqueue a write (pending, not yet committed)...
    conf = client.call_raw(
        "write_note", title=f"E2E resurrect {nonce}",
        content=f"resurrection probe {nonce} about kookaburra batching",
        intent="e2e resurrection", tags=[E2E_TAG],
    )
    nid = parse_id(conf, "note")
    track.note(nid)

    # ...and immediately delete it (synchronous chroma delete — finds nothing
    # yet because the worker hasn't committed).
    client.call_raw("delete_note", note_id=nid)

    # DESIRED behaviour: it stays gone. CURRENT behaviour: the worker later
    # commits the disk draft and the note reappears (this assertion fails ->
    # xfail).
    resurrected = poll_until(
        lambda: note_is_queryable(client, "kookaburra batching probe", nonce),
        timeout=settings.commit_timeout,
    )
    assert not resurrected, "deleted-while-pending note was resurrected by the worker"
