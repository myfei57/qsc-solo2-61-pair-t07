"""Retention engine: archive expired days, then delete what was archived."""

from __future__ import annotations

import datetime
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from tunnelnet.drain import utc_now

from .archive import ArchiveStore
from .journal import AuditJournal
from .model import Record
from .registry import AuditRegistry

DEFAULT_ONLINE_DAYS = 7


@dataclass(frozen=True)
class RetentionPolicy:
    """How long records stay online before they must be archived.

    The window counts whole UTC days, today included: with ``online_days``
    of 7 a record becomes eligible for archiving once its day is more than
    six days behind today.  Expired records are archived day by day and only
    deleted once their day is sealed, never before.
    """

    online_days: int = DEFAULT_ONLINE_DAYS

    def cutoff_day(self, today: str) -> str:
        """First online day; records whose day is older are expired."""

        base = datetime.date.fromisoformat(today)
        keep = max(int(self.online_days), 1) - 1
        return (base - datetime.timedelta(days=keep)).isoformat()

    def as_dict(self) -> dict[str, int]:
        return {"online_days": int(self.online_days)}


def _record_day(record: Record) -> str:
    """The UTC day of a record, or ``""`` when its timestamp is undatable."""

    day = record.at[:10]
    try:
        datetime.date.fromisoformat(day)
    except ValueError:
        return ""
    return day


@dataclass(frozen=True)
class RetentionReport:
    """Outcome of one archive-then-delete pass."""

    today: str
    cutoff: str
    archived: list[dict[str, object]]
    deleted: int
    deleted_by_day: dict[str, int]
    kept: int
    failures: list[dict[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, object]:
        return {
            "today": self.today,
            "cutoff": self.cutoff,
            "archived": list(self.archived),
            "deleted": self.deleted,
            "deleted_by_day": dict(self.deleted_by_day),
            "kept": self.kept,
            "failures": [dict(failure) for failure in self.failures],
            "ok": self.ok,
        }


class Retention:
    """Moves expired online records into the immutable archive.

    One pass archives every expired day that is not sealed yet and then
    deletes only the records whose day is sealed *and* whose identifier is
    present in the sealed pack — a record is never deleted without a
    successful archive.  Each day is processed independently, so one failing
    day does not block the others, and every step is idempotent: re-running
    a pass after a crash or a partial failure converges to the same state
    without rewriting sealed history.
    """

    def __init__(
        self,
        registry: AuditRegistry,
        journal: AuditJournal,
        archive: ArchiveStore,
        policy: RetentionPolicy | None = None,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self._registry = registry
        self._journal = journal
        self._archive = archive
        self._policy = policy or RetentionPolicy()
        self._clock = clock
        self._run_lock = threading.Lock()
        self._last: RetentionReport | None = None

    @property
    def policy(self) -> RetentionPolicy:
        return self._policy

    def _expired_by_day(self, cutoff: str) -> dict[str, list[Record]]:
        expired: dict[str, list[Record]] = {}
        for record in self._registry.records():
            day = _record_day(record)
            if not day or day >= cutoff:
                continue
            expired.setdefault(day, []).append(record)
        return expired

    def run(self) -> RetentionReport:
        """Archive expired days, then delete the records that were archived."""

        with self._run_lock:
            today = self._clock()[:10]
            cutoff = self._policy.cutoff_day(today)
            expired = self._expired_by_day(cutoff)
            archived: list[dict[str, object]] = []
            failures: list[dict[str, str]] = []
            for day in sorted(expired):
                if self._archive.pack(day) is not None:
                    continue
                try:
                    info = self._archive.seal(day, expired[day])
                except Exception as exc:  # noqa: BLE001 - one bad day must not stop the rest
                    failures.append({"day": day, "stage": "archive", "error": str(exc)})
                else:
                    archived.append(info.as_dict())
            deleted = 0
            deleted_by_day: dict[str, int] = {}
            kept = 0
            for day in sorted(expired):
                records = expired[day]
                if self._archive.pack(day) is None:
                    kept += len(records)  # not archived, so not deletable
                    continue
                try:
                    archived_ids = self._archive.ids(day)
                except Exception as exc:  # noqa: BLE001 - fail closed: keep the records
                    failures.append({"day": day, "stage": "verify", "error": str(exc)})
                    kept += len(records)
                    continue
                for record in records:
                    if record.id not in archived_ids:
                        kept += 1  # not inside the sealed pack, so not deletable
                        continue
                    try:
                        self._journal.delete(record)
                    except Exception as exc:  # noqa: BLE001 - retry on the next pass
                        failures.append(
                            {"day": day, "stage": "delete", "id": record.id, "error": str(exc)}
                        )
                        kept += 1
                        continue
                    self._registry.evict(record.id)
                    deleted += 1
                    deleted_by_day[day] = deleted_by_day.get(day, 0) + 1
            report = RetentionReport(
                today=today,
                cutoff=cutoff,
                archived=archived,
                deleted=deleted,
                deleted_by_day=deleted_by_day,
                kept=kept,
                failures=failures,
            )
            self._last = report
            return report

    def clear_archived(self) -> dict[str, int]:
        """Delete online records that are safely sealed in the archive.

        This is the guarded form of the old "clear everything" operation:
        records that have not been archived are kept, so the archive-first
        rule cannot be bypassed.
        """

        cleared = 0
        for record in self._registry.records():
            day = _record_day(record)
            if not day:
                continue
            pack = self._archive.pack(day)
            if pack is None:
                continue
            try:
                archived_ids = self._archive.ids(day)
            except Exception:  # noqa: BLE001 - fail closed: keep the record
                continue
            if record.id not in archived_ids:
                continue
            self._journal.delete(record)
            self._registry.evict(record.id)
            cleared += 1
        return {"cleared": cleared, "count": self._registry.count()}

    def status(self) -> dict[str, object]:
        """Policy, online window and archive state, for the console."""

        today = self._clock()[:10]
        cutoff = self._policy.cutoff_day(today)
        expired = self._expired_by_day(cutoff)
        packs = self._archive.packs()
        sealed_days = {pack.day for pack in packs}
        return {
            "policy": self._policy.as_dict(),
            "today": today,
            "cutoff": cutoff,
            "online": {
                "records": self._registry.count(),
                "expired": sum(len(records) for records in expired.values()),
                "expired_by_day": {day: len(expired[day]) for day in sorted(expired)},
            },
            "archive": {
                "packs": len(packs),
                "records": sum(pack.records for pack in packs),
                "days": [pack.day for pack in packs],
            },
            "pending_days": [day for day in sorted(expired) if day not in sealed_days],
            "last_run": self._last.as_dict() if self._last is not None else None,
        }

    def describe(self) -> str:
        return (
            f"retention online_days={self._policy.online_days} "
            f"online={self._registry.count()} packs={len(self._archive.packs())}"
        )
