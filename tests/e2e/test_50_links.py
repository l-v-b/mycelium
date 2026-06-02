"""Typed link graph: add_link / query_links / find_links / delete_link, the
ended_at historical filter, and context() graph expansion.

Links are written synchronously in BOTH modes (no team branch on add_link),
so no commit-wait is needed here — which itself is part of the contract.
"""
from __future__ import annotations

import pytest

from .conftest import E2E_TAG, commit_timeout_for, note_is_queryable, parse_id, poll_until

pytestmark = pytest.mark.blackbox


def _concept(nonce: str, suffix: str) -> tuple[str, str]:
    return (f"e2e_concept_{suffix}_{nonce}", f"E2E Concept {suffix} {nonce}")


def test_add_query_find_delete_link(client, nonce):
    src_id, src_label = _concept(nonce, "src")
    tgt_id, tgt_label = _concept(nonce, "tgt")
    desc = f"E2E link probe {nonce}: the bilby module depends on the dunnart module."

    conf = client.call_raw(
        "add_link",
        source_label=src_label, source_type="concept", source_id=src_id,
        target_label=tgt_label, target_type="concept", target_id=tgt_id,
        relation_type="depends_on", description=desc,
    )
    lid = parse_id(conf, "link")
    assert lid, f"no link id in: {conf!r}"

    try:
        # query_links by entity: outgoing edge present.
        ql = client.call_json("query_links", entity_id=src_id)
        out_targets = [e["target"]["id"] for e in ql.get("outgoing", [])]
        assert tgt_id in out_targets, f"outgoing link missing: {ql}"

        # find_links by semantic description.
        fl = client.call_json("find_links", query="bilby depends on dunnart module")
        assert any(l["link_id"] == lid for l in fl.get("links", [])), \
            "link not found by semantic description"
    finally:
        d = client.call_raw("delete_link", link_id=lid)
        assert "deleted" in d.lower() or "error" not in d.lower()

    # After delete it is gone from the semantic index.
    fl2 = client.call_json("find_links", query="bilby depends on dunnart module")
    assert all(l["link_id"] != lid for l in fl2.get("links", []))


def test_ended_at_excluded_from_default_find(client, nonce):
    src_id, src_label = _concept(nonce, "hsrc")
    tgt_id, tgt_label = _concept(nonce, "htgt")
    conf = client.call_raw(
        "add_link",
        source_label=src_label, source_type="concept", source_id=src_id,
        target_label=tgt_label, target_type="concept", target_id=tgt_id,
        relation_type="related_to",
        description=f"E2E historical link {nonce}: quoll superseded by potoroo.",
        ended_at="2020-01-01T00:00:00Z",
    )
    lid = parse_id(conf, "link")
    try:
        fl = client.call_json("find_links", query="quoll superseded by potoroo")
        # Default find excludes historical (ended_at != "") links.
        assert all(l["link_id"] != lid for l in fl.get("links", [])), \
            "historical (ended_at set) link must be excluded from default find_links"
    finally:
        client.call_raw("delete_link", link_id=lid)


def test_context_expands_links_from_returned_note(client, settings, nonce, track):
    """When a returned note is the source of a link, context() surfaces that
    edge — via graph expansion (links_from_results) for edges whose text does
    not match the query, or via the semantic links list otherwise.

    The link's labels/description here deliberately share NO terms with the
    query, so the only way context can surface it is graph expansion from the
    matched note's id.
    """
    nconf = client.call_raw(
        "write_note", title=f"E2E linked note {nonce}",
        content=f"## Context\nThe galah orchestration layer {nonce}.",
        intent="e2e link expansion", tags=[E2E_TAG],
    )
    nid = parse_id(nconf, "note")
    track.note(nid)
    assert poll_until(
        lambda: note_is_queryable(client, "galah orchestration layer", nonce),
        timeout=commit_timeout_for(settings),
    )

    # Target/description intentionally orthogonal to the query ("galah …") so a
    # semantic link search won't match it — isolating the graph-expansion path.
    tgt_id = f"e2e_concept_{nonce}"
    lconf = client.call_raw(
        "add_link",
        source_label=f"node {nonce}", source_type="note", source_id=nid,
        target_label=f"sibling {nonce}", target_type="concept", target_id=tgt_id,
        relation_type="related_to", description=f"opaque internal edge {nonce}",
    )
    lid = parse_id(lconf, "link")
    try:
        ctx = client.call_json("context", query="galah orchestration layer", mode="full")
        # The edge must appear somewhere context returns it: graph expansion
        # (expected here) or the semantic links list (fallback).
        targets = [e["target"]["id"] for e in ctx.get("links_from_results", [])]
        targets += [l["target"]["id"] for l in ctx.get("links", [])]
        assert tgt_id in targets, (
            "context surfaced neither a graph-expanded nor a semantic edge from "
            f"the matched note. links_from_results={ctx.get('links_from_results')} "
            f"links={ctx.get('links')}"
        )
    finally:
        client.call_raw("delete_link", link_id=lid)
