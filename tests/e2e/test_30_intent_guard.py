"""The v2.0.0 breaking-change guard: write_note requires a non-empty intent.

This is the one behavioural change personal users saw at v2.0.0, and it must
hold identically in team mode. A regression here means provenance (source_intent)
silently stops being captured.
"""
from __future__ import annotations

import pytest

from .conftest import E2E_TAG, parse_id
from .mcpclient import ToolError

pytestmark = pytest.mark.blackbox


@pytest.mark.parametrize("bad_intent", ["", "   ", "\t\n"])
def test_empty_intent_is_rejected(client, nonce, bad_intent):
    with pytest.raises(ToolError) as exc:
        client.call_raw(
            "write_note",
            title=f"E2E intent guard {nonce}",
            content=f"should never persist {nonce}",
            intent=bad_intent,
            tags=[E2E_TAG],
        )
    assert "intent" in str(exc.value).lower()


def test_rejected_write_did_not_persist(client, nonce):
    """A rejected write must leave nothing behind — no orphan note becomes
    queryable later."""
    try:
        client.call_raw(
            "write_note", title=f"E2E ghost {nonce}",
            content=f"ghost token {nonce}", intent="", tags=[E2E_TAG],
        )
    except ToolError:
        pass
    res = client.call_json("query_notes", query=f"ghost token {nonce}", n_results=5)
    assert all(nonce not in n.get("content", "") for n in res["notes"])


def test_valid_intent_accepted(client, nonce, track):
    conf = client.call_raw(
        "write_note", title=f"E2E intent ok {nonce}",
        content=f"valid {nonce}", intent="capturing a genuine decision", tags=[E2E_TAG],
    )
    nid = parse_id(conf, "note")
    assert nid
    track.note(nid)
