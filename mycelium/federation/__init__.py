"""Federated read-path for mycelium.

Per-source MCP tools surface external (and internal) data sources as
independent retrieval surfaces. A federated `context()` MCP tool fans out
across selected sources via asyncio.gather, rank-fuses results with
per-source bias weights, and returns top-N to the user agent.

No LLM aggregator. Cross-source synthesis is the user agent's job —
this module just delivers ranked, deterministic candidates.

Adapter discovery is allowlist-based: only adapters listed in the user's
~/.mycelium/config.json `enabled_adapters` field are imported and
registered. Default allowlist covers the in-process mycelium adapters
(notes, drawers, links). External adapters land as they're implemented.
"""
from __future__ import annotations

import importlib

from mycelium import config


DEFAULT_ENABLED_ADAPTERS = [
    "mycelium-notes",
    "mycelium-drawers",
    "mycelium-links",
]


def load_adapters() -> list[str]:
    """Import + register the adapters listed in ~/.mycelium/config.json.

    Called once during server startup (before the federation tool is invoked).
    Returns the list of adapter names that were actually loaded (a subset of
    the requested allowlist — missing modules are silently skipped).
    """
    requested = config._user_cfg.get("enabled_adapters") or DEFAULT_ENABLED_ADAPTERS
    loaded: list[str] = []
    for name in requested:
        mod_name = name.replace("-", "_")
        try:
            importlib.import_module(f"mycelium.federation.adapters.{mod_name}")
            loaded.append(name)
        except ModuleNotFoundError:
            # Adapter not yet implemented; skip silently.
            pass
        except Exception:
            # Adapter import raised (e.g. missing optional dep). Skip
            # but don't crash the whole server.
            pass
    return loaded
