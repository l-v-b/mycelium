from __future__ import annotations

import os
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

# Personal mode: embedded ChromaDB, no Redis, sync indexing.
# Team mode (Phase 3): ChromaDB server + Redis Streams workers.
DEPLOYMENT_MODE = os.environ.get("MYCELIUM_MODE", "personal")  # "personal" | "team"

# Git auto-commit. Set to empty string to disable.
VAULT_GIT_AUTHOR_NAME  = os.environ.get("MYCELIUM_GIT_AUTHOR", "mycelium")
VAULT_GIT_AUTHOR_EMAIL = os.environ.get("MYCELIUM_GIT_EMAIL", "mycelium@localhost")
