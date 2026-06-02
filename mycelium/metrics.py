"""Prometheus metric definitions.

Lazily-instantiated: importing this module does NOT pull in prometheus_client
unless metrics are actually accessed. Most code paths call `record(...)` or
`incr(...)` helpers, which become no-ops if prometheus_client isn't installed.

This keeps prometheus_client an OPTIONAL dependency (install the `[metrics]`
extra to enable Prometheus export). Without it, metric calls are no-ops.
"""
from __future__ import annotations

from typing import Any

_metrics: dict[str, Any] = {}
_initialized = False
_available = False


def _init() -> None:
    """Lazy initialization of all metric objects.

    Called on first access. If prometheus_client isn't installed, metrics
    become no-ops (calls succeed but record nothing).
    """
    global _initialized, _available
    if _initialized:
        return
    _initialized = True
    try:
        from prometheus_client import Counter, Gauge, Histogram
    except ImportError:
        _available = False
        return
    _available = True

    _metrics["write_total"] = Counter(
        "mycelium_write_total", "Writes by tool, mode, outcome",
        ["tool", "mode", "outcome"],
    )
    _metrics["write_latency_ms"] = Histogram(
        "mycelium_write_latency_ms", "End-to-end write latency including dedupe",
        ["tool", "mode"],
    )
    _metrics["dedupe_warning_total"] = Counter(
        "mycelium_dedupe_warning_total",
        "Near-duplicate warnings issued during write_note",
    )
    _metrics["queue_xlen"] = Gauge(
        "mycelium_queue_xlen",
        "Current Redis stream length (team mode only)",
    )
    _metrics["oldest_pending_age_seconds"] = Gauge(
        "mycelium_oldest_pending_age_seconds",
        "Age of the oldest committed_at: null file in the vault (team mode only)",
    )
    _metrics["writer_restart_total"] = Counter(
        "mycelium_writer_restart_total",
        "Writer worker process restarts",
    )
    _metrics["writer_error_total"] = Counter(
        "mycelium_writer_error_total",
        "Writer worker loop errors",
    )
    _metrics["writer_skipped_total"] = Counter(
        "mycelium_writer_skipped_total",
        "Stream messages whose file no longer exists on disk (treated as ack-and-drop)",
    )
    _metrics["gc_reenqueued_total"] = Counter(
        "mycelium_gc_reenqueued_total",
        "Orphaned drafts re-enqueued by writer startup GC sweep",
    )
    _metrics["commit_total"] = Counter(
        "mycelium_commit_total",
        "Successful chroma commits by kind (team mode only)",
        ["kind"],
    )

    # Federation metrics (PR-C).
    _metrics["read_total"] = Counter(
        "mycelium_read_total",
        "Federation hits per source",
        ["source"],
    )
    _metrics["read_latency_ms"] = Histogram(
        "mycelium_read_latency_ms",
        "Per-source latency in federated context() fanout",
        ["source"],
    )
    _metrics["read_error_total"] = Counter(
        "mycelium_read_error_total",
        "Federation per-source errors",
        ["source"],
    )
    _metrics["response_size_bytes"] = Histogram(
        "mycelium_response_size_bytes",
        "Federated response payload size (bytes)",
    )
    _metrics["response_size_tokens_estimate"] = Histogram(
        "mycelium_response_size_tokens_estimate",
        "Federated response token estimate. APPROXIMATION: bytes/4. "
        "Real tokenization is model-specific; this metric tracks payload "
        "shape, not exact billing.",
    )
    _metrics["response_truncated_total"] = Counter(
        "mycelium_response_truncated_total",
        "Federated responses whose snippets were truncated to fit the cap",
    )


def incr(name: str, labels: dict | None = None, value: float = 1.0) -> None:
    """Increment a counter. No-op if prometheus_client not installed.

    `labels` keys must match the metric's label spec.
    """
    _init()
    if not _available or name not in _metrics:
        return
    metric = _metrics[name]
    if labels:
        metric.labels(**labels).inc(value)
    else:
        metric.inc(value)


def observe(name: str, value: float, labels: dict | None = None) -> None:
    """Observe a histogram value. No-op if prometheus_client not installed."""
    _init()
    if not _available or name not in _metrics:
        return
    metric = _metrics[name]
    if labels:
        metric.labels(**labels).observe(value)
    else:
        metric.observe(value)


def set_gauge(name: str, value: float, labels: dict | None = None) -> None:
    """Set a gauge value. No-op if prometheus_client not installed."""
    _init()
    if not _available or name not in _metrics:
        return
    metric = _metrics[name]
    if labels:
        metric.labels(**labels).set(value)
    else:
        metric.set(value)
