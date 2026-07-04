"""Staleness filter (FR-013)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, List

from .schema import Signal


def is_stale(signal: Signal, now: datetime, window_seconds: int) -> bool:
    if signal.emitted_at.tzinfo is None:
        # treat naive as UTC
        emitted = signal.emitted_at.replace(tzinfo=timezone.utc)
    else:
        emitted = signal.emitted_at
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    delta = (now - emitted).total_seconds()
    return delta > window_seconds


def filter_eligible(signals: Iterable[Signal], now: datetime, window_seconds: int) -> List[Signal]:
    return [s for s in signals if not is_stale(s, now, window_seconds)]
