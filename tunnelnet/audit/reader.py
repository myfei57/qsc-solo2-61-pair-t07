"""Single read path over the live window and the sealed archive packages.

Online retrieval, archive lookups and CSV/export reports all call this reader,
so every consumer references the same canonical records. Records are de
duplicated by id (a record can transiently exist both live and sealed right
after packaging) and returned in chronological ``(at, id)`` order.
"""

from __future__ import annotations

from collections.abc import Sequence

from . import archive_reader, manifest as manifest_module
from .journal import PersistentAuditRegistry
from .model import Record
from .query import between as filter_between
from .query import filter_records


class AuditReader:
    """Searches live segments and WORM packages behind one interface."""

    def __init__(self, registry: PersistentAuditRegistry) -> None:
        self._registry = registry
        self._root = registry.root

    def search(
        self,
        scope: str = "all",
        event: str = "",
        start: str = "",
        end: str = "",
        verify: bool = False,
    ) -> list[Record]:
        """Return records by scope (``live``/``archive``/``all``).

        Time bounds compare on the canonical ``YYYY-MM-DDTHH:MM:SSZ`` stamp,
        which sorts lexicographically in chronological order.
        """

        records: list[Record] = []
        if scope in ("live", "all"):
            records.extend(self._registry.records())
        if scope in ("archive", "all"):
            records.extend(self._archive_records(start=start, end=end, verify=verify))
        if event:
            records = filter_records(records, event)
        if start and end:
            records = filter_between(records, start, end)
        elif start:
            records = [record for record in records if record.at >= start]
        elif end:
            records = [record for record in records if record.at <= end]
        return self._dedupe_sort(records)

    def _archive_records(
        self,
        start: str = "",
        end: str = "",
        verify: bool = False,
    ) -> list[Record]:
        records: list[Record] = []
        days = _days_for_window(start, end)
        for package_manifest in manifest_module.read_log(self._root):
            if days is not None and not (days[0] <= package_manifest.day <= days[1]):
                continue
            if not archive_reader.package_exists(self._root, package_manifest):
                continue
            records.extend(
                archive_reader.read_package(self._root, package_manifest, verify=verify)
            )
        return records

    @staticmethod
    def _dedupe_sort(records: Sequence[Record]) -> list[Record]:
        unique: dict[str, Record] = {}
        for record in records:
            unique.setdefault(record.id, record)
        return sorted(unique.values(), key=lambda item: (item.at, item.id))

    def packages(self) -> list[dict[str, object]]:
        """Manifest summary of every sealed package for the console."""

        result: list[dict[str, object]] = []
        for package_manifest in manifest_module.read_log(self._root):
            intact = archive_reader.package_exists(self._root, package_manifest)
            result.append(
                {
                    "package_id": package_manifest.package_id,
                    "day": package_manifest.day,
                    "sequence": package_manifest.sequence,
                    "created_at": package_manifest.created_at,
                    "records": package_manifest.record_count,
                    "segments": len(package_manifest.segments),
                    "present": intact,
                }
            )
        return result

    def counts(self) -> dict[str, int]:
        """Live vs archived record counts from one source of truth.

        A record sealed into a package but still inside the online window
        exists in both places; de-duplicate by id for the total so the number
        matches :meth:`search`.
        """

        live = self._registry.records()
        archived = self._archive_records()
        live_ids = {record.id for record in live}
        archive_only = sum(1 for record in archived if record.id not in live_ids)
        return {
            "live": len(live),
            "archived": len(archived),
            "archive_only": archive_only,
            "total": len(live) + archive_only,
        }


def _days_for_window(start: str, end: str) -> tuple[str, str] | None:
    """Narrow package candidates to days intersecting a time window."""

    if not start and not end:
        return None
    start_day = start[:10] if start else "0000-00-00"
    end_day = end[:10] if end else "9999-12-31"
    return start_day, end_day
