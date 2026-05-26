"""Payload size caps + per-source token budgets for federated responses.

Goals:
* Cap total bytes per response to avoid bloating the user agent's context
  budget (5 KB default).
* Truncate snippets fairly across sources when the cap is hit.
* Emit response_size + response_truncated metrics.
"""
from __future__ import annotations

import json

from mycelium import metrics


DEFAULT_PAYLOAD_CAP_BYTES = 5120  # 5 KB


def cap_payload(
    results: list[dict],
    cap_bytes: int = DEFAULT_PAYLOAD_CAP_BYTES,
) -> tuple[list[dict], int]:
    """Trim snippets so the serialized result list stays under cap_bytes.

    Strategy:
        1. Compute current serialized size.
        2. If under cap, return unchanged.
        3. Otherwise: halve each result's snippet (proportional to its
           current length) until the total fits or all snippets reach 50 chars.

    Returns (trimmed_results, truncation_count) — truncation_count is the
    number of results whose snippet was shortened.
    """
    if not results:
        return results, 0

    current = len(json.dumps(results))
    if current <= cap_bytes:
        return results, 0

    truncated_count = 0
    out = [dict(r) for r in results]
    iterations = 0

    while iterations < 10:
        iterations += 1
        current = len(json.dumps(out))
        if current <= cap_bytes:
            break

        # Halve every snippet > 50 chars.
        any_trimmed = False
        for r in out:
            snip = r.get("snippet", "")
            if len(snip) > 50:
                r["snippet"] = snip[: max(50, len(snip) // 2)]
                any_trimmed = True
        if not any_trimmed:
            # All snippets at or below 50 chars; can't trim further.
            break
        truncated_count += sum(1 for r in out if r.get("snippet", "") != results[out.index(r)].get("snippet", ""))

    if truncated_count:
        metrics.incr("response_truncated_total", value=1.0)
    return out, truncated_count


def estimate_tokens(payload_bytes: int) -> int:
    """Approximate token count from byte size. Bytes/4 is the standard cheap
    proxy — accuracy is model-specific, but this captures shape over time."""
    return payload_bytes // 4
