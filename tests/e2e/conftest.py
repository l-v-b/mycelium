"""Fixtures + configuration for the mycelium e2e suite.

Environment contract (all optional except MYCELIUM_E2E_URL for black-box runs):

  Black-box (run anywhere — compose personal/team, or live SIT):
    MYCELIUM_E2E_URL          full MCP endpoint, e.g. http://localhost:9101/sse
    MYCELIUM_E2E_TRANSPORT    "sse" (default) | "http"
    MYCELIUM_E2E_TOKEN        bearer token (when going through ContextForge)
    MYCELIUM_E2E_MODE         "personal" (default) | "team"  -> sets expectations
    MYCELIUM_E2E_COMMIT_TIMEOUT  seconds to await an async team commit (default 90)
    MYCELIUM_E2E_ALLOW_DIARY  "1" to permit diary writes (pollutes the day file;
                              on by default in compose, off against live SIT)

  White-box (hermetic compose, or kubectl against SIT):
    MYCELIUM_E2E_EXEC             exec prefix (default "docker exec";
                                  "kubectl exec -n <ns>" for in-cluster)
    MYCELIUM_E2E_SERVER_CONTAINER  server container/pod name (enables white-box)
    MYCELIUM_E2E_WORKER_CONTAINER  worker container/pod name
    MYCELIUM_E2E_REDIS_CONTAINER   redis container/pod name (enables stream probes)
    MYCELIUM_E2E_DATA_DIR          vault/chroma root inside the pod (default /data)

  Parity (compose only — needs BOTH stacks up):
    MYCELIUM_E2E_URL_PERSONAL  personal-stack MCP endpoint
    MYCELIUM_E2E_URL_TEAM      team-stack MCP endpoint
"""
from __future__ import annotations

import os
import time
import uuid
from typing import Callable, Optional

import pytest

from .introspect import Introspector
from .mcpclient import MyceliumClient

# All drawers/notes the suite creates carry this marker so we can find and
# purge them, and so we never collide with real content.
E2E_WING = "_e2e"
E2E_TAG = "_e2e_artifact"


# --------------------------------------------------------------------------- #
# markers
# --------------------------------------------------------------------------- #
def pytest_configure(config: pytest.Config) -> None:
    for name, desc in [
        ("blackbox", "talks only MCP; runs against any reachable stack"),
        ("whitebox", "needs docker/kubectl exec into the pod (disk/redis/chroma)"),
        ("parity", "compares a personal stack against a team stack"),
        ("team_only", "asserts team-mode async behaviour"),
        ("personal_only", "asserts personal-mode synchronous behaviour"),
        ("known_race", "documents a known defect via xfail; flips to xpass if fixed"),
    ]:
        config.addinivalue_line("markers", f"{name}: {desc}")


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
class Settings:
    def __init__(self) -> None:
        self.url = os.environ.get("MYCELIUM_E2E_URL", "")
        self.transport = os.environ.get("MYCELIUM_E2E_TRANSPORT", "sse")
        self.token = os.environ.get("MYCELIUM_E2E_TOKEN") or None
        self.mode = os.environ.get("MYCELIUM_E2E_MODE", "personal")
        self.commit_timeout = float(os.environ.get("MYCELIUM_E2E_COMMIT_TIMEOUT", "90"))
        self.allow_diary = os.environ.get("MYCELIUM_E2E_ALLOW_DIARY", "1") == "1"
        self.url_personal = os.environ.get("MYCELIUM_E2E_URL_PERSONAL", "")
        self.url_team = os.environ.get("MYCELIUM_E2E_URL_TEAM", "")

    @property
    def is_team(self) -> bool:
        return self.mode == "team"


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings()


# --------------------------------------------------------------------------- #
# clients
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def client(settings: Settings) -> MyceliumClient:
    if not settings.url:
        pytest.skip("MYCELIUM_E2E_URL not set — black-box tests need a reachable stack")
    c = MyceliumClient(settings.url, transport=settings.transport, token=settings.token)
    if not c.ping():
        pytest.fail(f"mycelium not reachable / not responding to MCP ping at {settings.url}")
    return c


@pytest.fixture(scope="session", autouse=True)
def _warmup(settings: Settings):
    """Safety-net warm-up of the embedder on the primary stack.

    The image bakes the MiniLM ONNX model, so first-embed is normally instant.
    This still front-loads one write->commit cycle (which in team mode exercises
    the worker's embedder too) so no model/index lazy-init lands inside a
    timing-sensitive assertion. Decoupled from the `client` fixture so it never
    cascades a skip onto parity tests (which set only *_PERSONAL/_TEAM URLs).
    """
    import uuid as _uuid

    if not settings.url:
        yield
        return

    c = MyceliumClient(settings.url, transport=settings.transport, token=settings.token)
    token = "warmup" + _uuid.uuid4().hex
    did = None
    try:
        conf = c.call_raw("file", content=f"warmup {token}", wing=E2E_WING, room="warmup", source="e2e probe")
        did = parse_id(conf, "drawer")
        poll_until(lambda: drawer_is_searchable(c, "warmup", token), timeout=180)
    except Exception:
        pass
    finally:
        if did:
            try:
                c.call_raw("delete_drawer", drawer_id=did)
            except Exception:
                pass
    yield


@pytest.fixture(scope="session")
def introspect() -> Introspector:
    return Introspector()


@pytest.fixture(scope="session")
def require_whitebox(introspect: Introspector) -> Introspector:
    if not introspect.enabled:
        pytest.skip("white-box probes unavailable (MYCELIUM_E2E_SERVER_CONTAINER unset)")
    return introspect


# --------------------------------------------------------------------------- #
# per-test nonce + cleanup
# --------------------------------------------------------------------------- #
@pytest.fixture
def nonce() -> str:
    """A unique, unguessable token. Embedded in written content so that a
    successful retrieval *cannot* be faked — the server can only return this
    token if it genuinely stored and indexed what we wrote in THIS run."""
    return "e2e" + uuid.uuid4().hex


@pytest.fixture
def track(client: MyceliumClient):
    """Register created entities for teardown. Always cleans up, even on failure,
    so repeated runs against a live stack don't accumulate junk."""
    notes: list[str] = []
    drawers: list[str] = []
    links: list[str] = []

    class Tracker:
        def note(self, note_id: str) -> str:
            notes.append(note_id)
            return note_id

        def drawer(self, drawer_id: str) -> str:
            drawers.append(drawer_id)
            return drawer_id

        def link(self, link_id: str) -> str:
            links.append(link_id)
            return link_id

    yield Tracker()

    for nid in notes:
        try:
            client.call_raw("delete_note", note_id=nid)
        except Exception:
            pass
    for did in drawers:
        try:
            client.call_raw("delete_drawer", drawer_id=did)
        except Exception:
            pass
    for lid in links:
        try:
            client.call_raw("delete_link", link_id=lid)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# helpers shared across test modules
# --------------------------------------------------------------------------- #
def poll_until(predicate: Callable[[], bool], timeout: float, interval: float = 2.0) -> bool:
    """Poll until predicate() is truthy or timeout elapses. Returns the final
    truthiness. Used for the team-mode eventual-consistency window."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    try:
        return bool(predicate())
    except Exception:
        return False


def note_is_queryable(client: MyceliumClient, query: str, nonce: str) -> bool:
    """True once the note containing `nonce` is returned by query_notes."""
    res = client.call_json("query_notes", query=query, n_results=10)
    return any(nonce in (n.get("content", "") + n.get("title", "")) for n in res.get("notes", []))


def drawer_is_searchable(client: MyceliumClient, query: str, nonce: str) -> bool:
    res = client.call_json("search", query=query, n_results=10)
    return any(nonce in r.get("text", "") for r in res.get("results", []))


def parse_id(confirmation: str, prefix: str) -> Optional[str]:
    """Extract a note_/drawer_/link_ id from a confirmation string."""
    import re

    m = re.search(rf"{prefix}_[0-9a-f]{{16}}", confirmation)
    return m.group(0) if m else None


def commit_timeout_for(settings: Settings) -> float:
    """Personal mode commits synchronously; allow only a short settle. Team mode
    gets the full configured async window."""
    return settings.commit_timeout if settings.is_team else 15.0
