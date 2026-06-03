"""Tests for per-call author resolution (Phase 2 team-mode identity).

Mocks current_identity so these run without an HTTP request / FastMCP. The
header-parsing in current_identity itself is verified empirically against a real
ContextForge call (it depends on the live transport).
"""
from __future__ import annotations

import pytest

from mycelium import config, identity


def test_personal_no_identity_uses_config_author(monkeypatch):
    monkeypatch.setattr(config, "DEPLOYMENT_MODE", "personal")
    monkeypatch.setattr(config, "AUTHOR", "rain-mycelium-service")
    monkeypatch.setattr(identity, "current_identity", lambda: None)
    assert identity.resolve_author() == "rain-mycelium-service"


def test_team_no_identity_fails_closed(monkeypatch):
    monkeypatch.setattr(config, "DEPLOYMENT_MODE", "team")
    monkeypatch.setattr(identity, "current_identity", lambda: None)
    with pytest.raises(PermissionError):
        identity.resolve_author()


def test_team_with_identity_uses_email(monkeypatch):
    monkeypatch.setattr(config, "DEPLOYMENT_MODE", "team")
    monkeypatch.setattr(
        identity, "current_identity",
        lambda: identity.Identity(author="liam.blignaut@rain.co.za", teams=["dba"]),
    )
    assert identity.resolve_author() == "liam.blignaut@rain.co.za"


def test_personal_prefers_propagated_identity_if_present(monkeypatch):
    monkeypatch.setattr(config, "DEPLOYMENT_MODE", "personal")
    monkeypatch.setattr(config, "AUTHOR", "fallback")
    monkeypatch.setattr(
        identity, "current_identity",
        lambda: identity.Identity(author="real@user"),
    )
    assert identity.resolve_author() == "real@user"
