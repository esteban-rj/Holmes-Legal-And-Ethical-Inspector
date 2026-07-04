"""Dedup logic (FR-012).

Key = (entity_id, source_agent, signal_type, time_bucket)
  where time_bucket = floor(emitted_at_epoch / window_seconds).
Only the first signal with a given key within the same bucket survives. Subsequent
duplicates are dropped and counted in a metric. A dropped dedup MUST NOT cause a
duplicate Critical Fraud Alert (consensus dedup is shared).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

from .schema import Signal


@dataclass
class DedupStats:
    accepted: int = 0
    dropped: int = 0

    def as_dict(self) -> Dict[str, int]:
        return {"accepted": self.accepted, "dropped": self.dropped}


class Deduper:
    def __init__(self, window_seconds: int) -> None:
        if window_seconds <= 0:
            raise ValueError("dedup_window_seconds must be positive")
        self.window = window_seconds
        self._seen: Dict[Tuple[str, str, str, int], Signal] = {}
        self.stats = DedupStats()

    def _bucket(self, signal: Signal) -> int:
        return int(signal.emitted_at.timestamp()) // self.window

    def accept(self, signal: Signal) -> bool:
        """Return True if the signal is accepted (first in its bucket), False if dropped."""
        key = (signal.entity_id, signal.source_agent, signal.signal_type, self._bucket(signal))
        if key in self._seen:
            self.stats.dropped += 1
            return False
        self._seen[key] = signal
        self.stats.accepted += 1
        return True
