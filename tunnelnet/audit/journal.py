"""Append only, file backed audit registry for the online retention window.

Records are routed to ``live/<day>/<event>.<gen>.jsonl``: by event type first
as required, and by UTC day so whole days can be handed to the archiver. The
active file rolls to a new generation once it reaches ``max_segment_bytes``,
which gives the size triggered archiving path immutable, sealed units.

Nothing here ever rewrites history: :meth:`clear` refuses because deletion is
the retention subsystem's job and only runs after a record is safely archived.
"""

from __future__ import annotations

import os
import threading

from . import layout, segment
from .model import Record
from .report import AuditState
from .summary import count_by_event, summary
from .timeutil import day_of, utc_now


class ImmutableAuditError(RuntimeError):
    """Raised when a caller tries to wipe durable audit history directly."""


class PersistentAuditRegistry:
    """File backed counterpart of the in memory :class:`AuditRegistry`."""

    def __init__(
        self,
        root: str,
        max_segment_bytes: int = 8 * 1024 * 1024,
        clock=utc_now,
    ) -> None:
        self._root = root
        self._max_bytes = max(1, max_segment_bytes)
        self._clock = clock
        self._lock = threading.RLock()
        layout.ensure_layout(self._root)
        self._active_generation: dict[tuple[str, str], int] = {}
        self._load_active_generations()

    @property
    def root(self) -> str:
        return self._root

    def _load_active_generations(self) -> None:
        for day in segment.list_live_days(self._root):
            for item in segment.list_day_segments(self._root, day):
                key = (day, item.event)
                previous = self._active_generation.get(key, 0)
                self._active_generation[key] = max(previous, item.generation)

    def append(self, record: Record) -> Record:
        """Persist one record into its event segment and return it."""

        with self._lock:
            day = day_of(record.at)
            generation = self._active_generation.get((day, record.event), 0)
            if generation == 0:
                generation = 1
            path = self._segment_path(day, record.event, generation)
            line = segment.encode_record(record)
            try:
                size = os.path.getsize(path)
            except FileNotFoundError:
                size = 0
            if size > 0 and size + len(line.encode("utf-8")) + 1 > self._max_bytes:
                generation += 1
                path = self._segment_path(day, record.event, generation)
            segment.append_line(path, line)
            self._active_generation[(day, record.event)] = generation
        return record

    def _segment_path(self, day: str, event: str, generation: int) -> str:
        directory = layout.day_dir(self._root, day)
        return os.path.join(directory, layout.segment_name(event, generation))

    def live_segments(self, days: list[str] | None = None) -> list[segment.Segment]:
        with self._lock:
            selected = days if days is not None else segment.list_live_days(self._root)
            items: list[segment.Segment] = []
            for day in selected:
                items.extend(segment.list_day_segments(self._root, day))
            return items

    def records(self) -> list[Record]:
        """Every live record in chronological ``(at, id)`` order."""

        items = [item for seg in self.live_segments() for item in seg.records()]
        items.sort(key=lambda record: (record.at, record.id))
        return items

    def list(self) -> list[Record]:
        """Records sorted by identifier, for stable console output."""

        return sorted(self.records(), key=lambda record: record.id)

    def count(self) -> int:
        return len(self.records())

    def clear(self) -> int:
        raise ImmutableAuditError(
            "audit history is append only; expire records through the retention window"
        )

    def latest_id(self) -> str:
        records = self.records()
        return records[-1].id if records else ""

    def state(self) -> AuditState:
        return AuditState(
            entries=[record.as_dict() for record in self.list()], count=self.count()
        )

    def count_by_event(self) -> dict[str, int]:
        return count_by_event(self.records())

    def summary(self) -> dict[str, int]:
        return summary(self.records())

    def day_bytes(self, day: str) -> int:
        return sum(item.size for item in self.live_segments([day]))

    def describe(self) -> str:
        days = segment.list_live_days(self._root)
        files = len(self.live_segments())
        return f"audit records={self.count()} live_days={len(days)} segments={files}"
