"""Per-call end-user identity, resolved from ContextForge's propagated headers.

When `identity_propagation` is enabled on the rain-mycelium gateway, ContextForge
injects the authenticated end-user's identity onto every forwarded request as
`X-Forwarded-User-*` HTTP headers (see CF mcpgateway/utils/identity_propagation.py:
build_identity_headers). We read them here to stamp the real per-user author on
team-mode writes instead of a static service identity.

Trust model (MVP): the Kubernetes NetworkPolicy restricts ingress to ContextForge
pods only, so these headers can ONLY originate from CF — no other pod can reach
mycelium to forge them. HMAC signature verification (CF `sign_claims` →
`X-Forwarded-User-Claims-Signature`) is deferred; the network boundary is the
trust anchor for now. To add signing later: verify the HMAC here with the shared
`identity_claims_secret`.

Personal mode: no gateway in front, so no headers — current_identity() returns
None and resolve_author() falls back to config.AUTHOR (unchanged behaviour).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# CF default header prefix (identity_propagation_headers_prefix); the HTTP layer
# lowercases header names.
_PREFIX = "x-forwarded-user"


@dataclass
class Identity:
    """An end-user identity propagated by ContextForge."""

    author: str                       # email (CF user_id == email for EntraID)
    teams: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    is_admin: bool = False


def current_identity() -> Identity | None:
    """Resolve the end-user identity from the inbound request headers.

    Returns None when there is no propagated identity — either no active HTTP
    request (e.g. CLI/personal mode) or ContextForge identity propagation is not
    enabled on the gateway. Never raises.
    """
    try:
        from fastmcp.server.dependencies import get_http_headers
        headers = get_http_headers(include_all=True)
    except Exception:  # no active HTTP request / FastMCP unavailable
        return None
    if not headers:
        return None

    email = (headers.get(f"{_PREFIX}-email") or headers.get(f"{_PREFIX}-id") or "").strip()
    if not email:
        return None

    def _csv(key: str) -> list[str]:
        return [x.strip() for x in headers.get(key, "").split(",") if x.strip()]

    return Identity(
        author=email,
        teams=_csv(f"{_PREFIX}-teams"),
        groups=_csv(f"{_PREFIX}-groups"),
        is_admin=headers.get(f"{_PREFIX}-admin", "").strip().lower() == "true",
    )


def resolve_author() -> str:
    """Resolve the author to stamp on a disk write.

    - team mode: FAIL-CLOSED — require a ContextForge-propagated identity; raise
      if absent (an unauthenticated/direct write must not be silently attributed).
    - personal mode: use the propagated identity if somehow present, else the
      configured default (MYCELIUM_AUTHOR / git config / "unknown").
    """
    from mycelium import config

    ident = current_identity()
    if config.DEPLOYMENT_MODE == "team":
        if ident is None:
            logger.warning("team-mode write rejected: no propagated end-user identity")
            raise PermissionError(
                "team mode: write rejected — no authenticated end-user identity. "
                "ContextForge identity propagation (X-Forwarded-User-*) header is "
                "missing; direct/unauthenticated writes are not allowed."
            )
        logger.info("team-mode write author=%s teams=%s", ident.author, ident.teams)
        return ident.author
    return ident.author if ident is not None else config.AUTHOR
