"""Source adapter protocol + registry.

Each external (or internal) data source implements a `SourceClient` and
calls `register(...)` at module import time. The federation fanout
(`fanout.py`) reads from the registry.

Adapters are loaded via the allowlist mechanism in `federation/__init__.py`
to keep the security boundary explicit — no rogue adapter can register
itself without being listed in the user's config.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class SourceResult:
    """One ranked hit from a source adapter."""
    source: str                 # adapter name, e.g. "mycelium-notes"
    rank: float                 # [0, 1], higher = more relevant (before bias)
    id: str                     # source-internal id
    title: str | None = None
    snippet: str = ""           # truncated content for the response
    metadata: dict = field(default_factory=dict)  # source-specific extras


@runtime_checkable
class SourceClient(Protocol):
    """Adapters implement this protocol. Async preferred (parallel fanout)."""
    name: str

    async def search(self, query: str, k: int) -> list[SourceResult]: ...


# Registry populated by adapter modules at import time.
_REGISTRY: dict[str, SourceClient] = {}


def register(client: SourceClient) -> None:
    """Adapters call this in their module-level code."""
    _REGISTRY[client.name] = client


def get(name: str) -> SourceClient | None:
    return _REGISTRY.get(name)


def all_names() -> list[str]:
    return list(_REGISTRY.keys())
