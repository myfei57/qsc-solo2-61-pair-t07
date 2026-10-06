"""Append only audit registry, write-through to the durable journal."""

from __future__ import annotations

import threading

from .journal import AuditJournal
from .model import Record
from .report import AuditState
from .summary import count_by_event, summary


class AuditRegistry:
    """Keeps every linkage audit record in the order it was written.

    When a journal is attached the registry is a read cache over it: appends
    are persisted before they become visible, deletions go through
    :meth:`evict` after the journal copy is gone, and the in-memory order is
    rebuilt from the journal on startup so a restart loses nothing.
    """

    def __init__(self, journal: AuditJournal | None = None) -> None:
        self._journal = journal
        self._records: list[Record] = journal.scan() if journal is not None else []
        self._lock = threading.RLock()

    def append(self, record: Record) -> Record:
        with self._lock:
            if self._journal is not None:
                self._journal.append(record)
            self._records.append(record)
        return record

    def evict(self, record_id: str) -> bool:
        """Drop one record from memory after its journal copy was deleted."""

        with self._lock:
            for index, record in enumerate(self._records):
                if record.id == record_id:
                    del self._records[index]
                    return True
        return False

    def records(self) -> list[Record]:
        with self._lock:
            return list(self._records)

    def list(self) -> list[Record]:
        """Records sorted by identifier, for stable console output."""

        return sorted(self.records(), key=lambda record: record.id)

    def count(self) -> int:
        with self._lock:
            return len(self._records)

    def latest_id(self) -> str:
        with self._lock:
            if not self._records:
                return ""
            return self._records[-1].id

    def state(self) -> AuditState:
        return AuditState(entries=[record.as_dict() for record in self.list()], count=self.count())

    def count_by_event(self) -> dict[str, int]:
        return count_by_event(self.records())

    def summary(self) -> dict[str, int]:
        return summary(self.records())

    def describe(self) -> str:
        return f"audit records={self.count()}"
