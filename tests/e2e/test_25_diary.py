"""Diary write -> read round-trip.

Diary entries append to a per-day file and (team mode) re-index the whole day
via the worker. Because they can't be individually deleted, this is gated on
MYCELIUM_E2E_ALLOW_DIARY (on in the ephemeral compose harness, off against a
live deployment you don't want to pollute).
"""
from __future__ import annotations

import pytest

from .conftest import commit_timeout_for, poll_until

pytestmark = pytest.mark.blackbox


def test_diary_roundtrip(client, settings, nonce):
    if not settings.allow_diary:
        pytest.skip("diary writes pollute the day file; set MYCELIUM_E2E_ALLOW_DIARY=1 to run")

    conf = client.call_raw(
        "diary_write",
        content=f"diary probe {nonce}: wallaby checkpoint reconciliation notes.",
        session_id=f"e2e-{nonce}",
    )
    assert "diary" in conf.lower()

    # diary_read returns markdown text (not JSON). The nonce must appear once
    # the day file is (re)indexed / read back.
    found = poll_until(
        lambda: nonce in client.call_raw("diary_read"),
        timeout=commit_timeout_for(settings),
    )
    assert found, "diary entry never became readable via diary_read"
