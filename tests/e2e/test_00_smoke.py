"""Smoke: the stack is up, speaks MCP, and exposes the expected tool surface."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.blackbox

# The 22-tool surface mycelium is documented to expose. If a deploy drops or
# renames a tool, agents break silently — so we pin the set explicitly.
EXPECTED_TOOLS = {
    "context", "search", "file", "get_drawer", "update_drawer", "delete_drawer",
    "list_wings", "list_rooms", "list_drawers", "check_duplicate", "write_note",
    "context_federated", "delete_note", "query_notes", "context_titles",
    "add_link", "query_links", "find_links", "delete_link", "diary_write",
    "diary_read", "get_aaak_spec", "get_taxonomy", "get_room_drawers", "status",
}


def test_server_responds_to_ping(client):
    assert client.ping() is True


def test_tool_surface_present(client):
    tools = set(client.list_tools())
    missing = EXPECTED_TOOLS - tools
    assert not missing, f"deploy is missing expected MCP tools: {sorted(missing)}"


def test_status_shape(client):
    st = client.call_json("status")
    assert set(st) >= {"vault", "index"}
    assert set(st["vault"]) >= {"notes", "drawers", "diary_days"}
    assert set(st["index"]) >= {"notes", "drawers", "links"}
    for v in st["vault"].values():
        assert isinstance(v, int)
    for v in st["index"].values():
        assert isinstance(v, int)


def test_taxonomy_shape(client):
    tax = client.call_json("get_taxonomy")
    assert "taxonomy" in tax and isinstance(tax["taxonomy"], dict)


def test_federation_default_adapters_registered(client):
    """context_federated must fan out across the three built-in mycelium
    adapters; a broken federation import would drop them."""
    res = client.call_json("context_federated", query="mycelium", n_results=5)
    assert "results" in res and "source_diagnostics" in res
    diag = res["source_diagnostics"]
    for adapter in ("mycelium-notes", "mycelium-drawers", "mycelium-links"):
        assert adapter in diag, f"federation adapter {adapter} not registered: {list(diag)}"
