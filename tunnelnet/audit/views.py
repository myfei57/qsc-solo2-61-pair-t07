"""Read views that span the online journal and the sealed archive."""

from __future__ import annotations

from .archive import ArchiveStore
from .model import Record
from .registry import AuditRegistry

SOURCES = ("online", "archive", "all")


def source_records(registry: AuditRegistry, archive: ArchiveStore, source: str) -> list[Record]:
    """One consistent record set behind exports and reports.

    The online window and the archive packs hold the same records at
    different life-cycle stages, keyed by identifier; a merged view de-
    duplicates by identifier so a record archived but not yet deleted
    appears exactly once.
    """

    if source == "online":
        return sorted(registry.records(), key=lambda record: (record.at, record.id))
    if source == "archive":
        return archive.query()
    if source == "all":
        merged = {record.id: record for record in archive.query()}
        for record in registry.records():
            merged[record.id] = record
        return sorted(merged.values(), key=lambda record: (record.at, record.id))
    raise ValueError(f"unknown audit source {source!r}")
