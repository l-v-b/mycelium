"""JSONL log of every write-path tool call.

Parallel to mycelium.retrieval_log but for writes. Captures KB growth,
wing distribution of new captures, cross-wing link rate (interesting
paired with cross-wing retrieval rate from the read-path log), and
prune/update activity.

Same cheap-and-non-blocking discipline as the read-path logger: write
failures are swallowed with a warning, never raised — write logging
must never break a write.

Disabled by setting MYCELIUM_WRITE_LOG to an empty string.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mycelium.config import WRITE_LOG_PATH

_logger = logging.getLogger(__name__)


def log_write(
    op: str,
    fields: dict[str, Any] | None = None,
    caller: str = "agent",
) -> None:
    """Append one JSONL record describing a write-path call.

    `op` is the write operation name: write_note, file, update_drawer,
    add_link, delete_note, delete_drawer, delete_link, diary_write.

    `fields` is operation-specific metadata — see each call site for
    what gets captured. Keep payloads compact (no full content bodies).

    Silently no-ops if the log path is empty or the write fails.
    Never raises.
    """
    if not WRITE_LOG_PATH:
        return

    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "op": op,
        "caller": caller,
    }
    if fields:
        record.update(fields)

    try:
        path = Path(WRITE_LOG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")
    except Exception as e:
        _logger.warning("write log write failed: %s", e)
