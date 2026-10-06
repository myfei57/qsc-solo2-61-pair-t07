"""Facade tying the journal, archiver, retention gate and reader together.

The service owns the lifecycle the operators drive from the console:

* :meth:`append` -- durable write into the event typed online journal;
* :meth:`archive_due` -- daily batch packaging with a size fallback;
* :meth:`run_retention` -- archive first, delete only verified archived data;
* :meth:`search` / :meth:`packages` -- one read path for live and archived
  records, reused by online queries, archive inspection and export reports.

Every maintenance call runs crash recovery first, so interrupted batch work
resumes safely and repeated calls are idempotent.
"""

from __future__ import annotations

import threading

from .archiver import Archiver
from .journal import PersistentAuditRegistry
from .model import Record
from .reader import AuditReader
from .report import AuditState
from .retention import RetentionSweeper
from .settings import AuditSettings, load_settings, save_settings
from .summary import count_by_event as _count_by_event
from .summary import summary as _summary
from .timeutil import utc_now


class AuditService:
    """Durable linkage audit: persist, archive, retain and retrieve."""

    def __init__(self, root: str, settings: AuditSettings | None = None, clock=utc_now) -> None:
        self._root = root
        self._clock = clock
        self.settings = settings or load_settings(root)
        self.registry = PersistentAuditRegistry(
            root, max_segment_bytes=self.settings.max_segment_bytes, clock=clock
        )
        self.archiver = Archiver(self.registry, max_day_bytes=self.settings.max_day_bytes, clock=clock)
        self.sweeper = RetentionSweeper(
            self.registry, self.archiver, policy=self.settings.policy(), clock=clock
        )
        self.reader = AuditReader(self.registry)
        self._lock = threading.RLock()

    @property
    def root_path(self) -> str:
        return self._root

    def append(self, record: Record) -> Record:
        with self._lock:
            return self.registry.append(record)

    def search(
        self,
        scope: str = "all",
        event: str = "",
        start: str = "",
        end: str = "",
        verify: bool = False,
    ) -> list[Record]:
        with self._lock:
            return self.reader.search(scope=scope, event=event, start=start, end=end, verify=verify)

    def live_records(self) -> list[Record]:
        return self.search(scope="live")

    def packages(self) -> list[dict[str, object]]:
        with self._lock:
            return self.reader.packages()

    def counts(self) -> dict[str, int]:
        with self._lock:
            return self.reader.counts()

    def archive_due(self, force_current: bool = False) -> dict[str, object]:
        """Seal past days (and oversized current days) into WORM packages."""

        with self._lock:
            result = self.archiver.run_once(force_current=force_current)
            return result.as_dict()

    def recover(self) -> dict[str, object]:
        """Resolve interrupted packaging from an earlier process."""

        with self._lock:
            result = self.archiver.recover()
            return result.as_dict()

    def run_retention(self, dry_run: bool = False) -> dict[str, object]:
        """Archive due history, then prune the online window behind the gate."""

        with self._lock:
            result = self.sweeper.run_once(dry_run=dry_run)
            return result.as_dict()

    def update_settings(self, online_days: int | None = None, max_day_bytes: int | None = None) -> AuditSettings:
        with self._lock:
            current = self.settings
            updated = AuditSettings(
                online_days=online_days if online_days is not None else current.online_days,
                max_segment_bytes=current.max_segment_bytes,
                max_day_bytes=max_day_bytes if max_day_bytes is not None else current.max_day_bytes,
            )
            save_settings(self._root, updated)
            self.settings = updated
            self.sweeper.policy = updated.policy()
            self.archiver._max_day_bytes = updated.max_day_bytes  # noqa: SLF001
            return updated

    def describe(self) -> str:
        counts = self.counts()
        return (
            f"audit live={counts['live']} archived={counts['archived']} "
            f"packages={len(self.packages())} online_days={self.settings.online_days}"
        )

    # -- compatibility surface with the in memory AuditRegistry -------------

    def records(self) -> list[Record]:
        """Online window records (the old registry's default read)."""

        return self.search(scope="live")

    def list(self) -> list[Record]:
        return sorted(self.records(), key=lambda record: record.id)

    def count(self) -> int:
        return self.registry.count()

    def latest_id(self) -> str:
        records = self.records()
        return records[-1].id if records else ""

    def state(self) -> AuditState:
        return AuditState(
            entries=[record.as_dict() for record in self.list()], count=self.count()
        )

    def count_by_event(self) -> dict[str, int]:
        return _count_by_event(self.records())

    def summary(self) -> dict[str, int]:
        return _summary(self.records())

    def clear(self) -> int:
        return self.registry.clear()
