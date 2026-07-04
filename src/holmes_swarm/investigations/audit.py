"""Audit log (FR-030)."""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, List, Optional

from ..blackboard.schema import AuditLogEntry


class AuditLog:
    def __init__(self) -> None:
        self._entries: List[AuditLogEntry] = []

    def append(self, entry: AuditLogEntry) -> None:
        self._entries.append(entry)

    def query(
        self,
        *,
        actor: Optional[str] = None,
        action: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> List[AuditLogEntry]:
        out: List[AuditLogEntry] = []
        for e in self._entries:
            if actor is not None and e.actor != actor:
                continue
            if action is not None and e.action != action:
                continue
            if since is not None and e.at < since:
                continue
            if until is not None and e.at > until:
                continue
            out.append(e)
        return out

    def all(self) -> List[AuditLogEntry]:
        return list(self._entries)
