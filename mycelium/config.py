from __future__ import annotations

import json
import os
import subprocess
import warnings
from pathlib import Path

DATA_DIR   = Path(os.environ.get("MYCELIUM_DATA_DIR", "/data"))
VAULT_DIR  = DATA_DIR / "vault"
CHROMA_DIR = DATA_DIR / "chroma"

NOTES_DIR    = VAULT_DIR / "notes"
DRAWERS_DIR  = VAULT_DIR / "drawers"
DIARY_DIR    = VAULT_DIR / "diary"
CONCEPTS_DIR = VAULT_DIR / "concepts"
LINKS_DIR    = VAULT_DIR / "links"

HOST = os.environ.get("MYCELIUM_HOST", "0.0.0.0")
PORT = int(os.environ.get("MYCELIUM_PORT", "9002"))


# ---------------------------------------------------------------------------
# Deployment mode — server-side
# ---------------------------------------------------------------------------
# NOTE: this is distinct from the client-side `mode` field in
# ~/.mycelium/config.json (which `mycelium init --mode client|server` writes
# to control local CLI behaviour). This env var controls the SERVER's write
# path: personal = direct sync writes; team = via Redis Streams + writer worker.
#
# Renamed 2026-05-26 from MYCELIUM_MODE to MYCELIUM_DEPLOYMENT_MODE to
# eliminate that ambiguity. MYCELIUM_MODE still accepted as a deprecated alias.
_new_mode = os.environ.get("MYCELIUM_DEPLOYMENT_MODE")
_old_mode = os.environ.get("MYCELIUM_MODE")
if _new_mode is not None:
    DEPLOYMENT_MODE = _new_mode
elif _old_mode is not None:
    warnings.warn(
        "MYCELIUM_MODE is deprecated; rename to MYCELIUM_DEPLOYMENT_MODE. "
        "The old name will be removed in a future major version.",
        DeprecationWarning,
        stacklevel=2,
    )
    DEPLOYMENT_MODE = _old_mode
else:
    DEPLOYMENT_MODE = "personal"


# ---------------------------------------------------------------------------
# Author identity stamped on every disk write
# ---------------------------------------------------------------------------
# Resolution order:
#   1. MYCELIUM_AUTHOR env var (explicit)
#   2. git config user.email (most personal stacks have this)
#   3. "unknown"
#
# Future team mode (Phase 3.3 / 3.4) will override this on a per-call basis
# from the auth context (EntraID claim via ContextForge) — the server-level
# default remains the fallback when no per-call author is provided.

def _resolve_default_author() -> str:
    env = os.environ.get("MYCELIUM_AUTHOR")
    if env:
        return env
    try:
        out = subprocess.run(
            ["git", "config", "--get", "user.email"],
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "unknown"


AUTHOR = _resolve_default_author()


# Git auto-commit. Set to empty string to disable.
VAULT_GIT_AUTHOR_NAME  = os.environ.get("MYCELIUM_GIT_AUTHOR", "mycelium")
VAULT_GIT_AUTHOR_EMAIL = os.environ.get("MYCELIUM_GIT_EMAIL", "mycelium@localhost")

# Team-mode Redis (only relevant when MYCELIUM_DEPLOYMENT_MODE=team).
REDIS_URL = os.environ.get("MYCELIUM_REDIS_URL", "redis://localhost:6379/0")


# ---------------------------------------------------------------------------
# User config from ~/.mycelium/config.json
# ---------------------------------------------------------------------------
# Loaded once at startup. Used today for per-source bias overrides; future
# extension points (external_source_biases, enabled_adapters) land in
# subsequent PRs but the loader is in place.
_user_cfg: dict = {}
_user_cfg_path = Path.home() / ".mycelium" / "config.json"
if _user_cfg_path.exists():
    try:
        _user_cfg = json.loads(_user_cfg_path.read_text())
        if not isinstance(_user_cfg, dict):
            _user_cfg = {}
    except (json.JSONDecodeError, OSError):
        _user_cfg = {}

# Per-source bias applied to raw cosine distance when ranking across sources.
# LOWER bias = HIGHER priority in the merged top_results ranking.
#   Notes are curated synthesis (highest signal).
#   Drawers are raw verbatim (high-volume, lower per-item signal).
#   Links are connectors (useful but rarely standalone answers).
# Tune empirically without redeploy via env vars.
SOURCE_BIAS_NOTE   = float(os.environ.get("MYCELIUM_BIAS_NOTE",   "-0.05"))
SOURCE_BIAS_DRAWER = float(os.environ.get("MYCELIUM_BIAS_DRAWER",  "0.00"))
SOURCE_BIAS_LINK   = float(os.environ.get("MYCELIUM_BIAS_LINK",    "0.10"))

# External-source biases for federated context (PR-C will use this).
# Keyed by adapter name, e.g. {"gitlab-config": 0.05, "backstage": 0.0}.
# Defaults to empty — adapters fall back to 0.0 bias if not listed.
EXTERNAL_SOURCE_BIASES: dict[str, float] = {
    k: float(v) for k, v in (_user_cfg.get("external_source_biases") or {}).items()
}

# Retrieval log — JSONL of every read-path tool call (cross-wing retrieval
# baseline for the scope-aware-retrieval / user-poisoning work; see
# note_86ea3fb995350faa). Set to empty string to disable.
RETRIEVAL_LOG_PATH = os.environ.get(
    "MYCELIUM_RETRIEVAL_LOG",
    str(DATA_DIR / "logs" / "retrieval.jsonl"),
)
