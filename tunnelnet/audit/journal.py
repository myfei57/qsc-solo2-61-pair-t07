"""File backed journal that keeps the online audit records durable."""

from __future__ import annotations

import os
import re

from tunnelnet.store import Store

from .model import Record

_SLUG_SAFE = re.compile(r"[^a-z0-9_-]+")


def audit_root(store_root: str) -> str:
    """Directory that holds every audit file inside the record store.

    The directory is hidden (dot prefixed) so the domain record store does
    not list audit files among its kinds, and a factory reset of the store
    leaves the audit trail untouched.
    """

    return os.path.join(store_root, ".audit")


def event_slug(event: str) -> str:
    """Directory safe form of an event name.

    Records keep their original event name inside the document; the slug is
    only a storage shard, so collisions between similar names are harmless.
    """

    slug = _SLUG_SAFE.sub("_", event.strip().lower()).strip("_")
    return slug or "event"


class AuditJournal:
    """Append-only online records, one directory per event type.

    Every write goes through the atomic record store, so a record is either
    fully on disk or absent.  The journal is the durable source behind the
    in-memory registry; a restart rebuilds the registry from these files.
    """

    def __init__(self, root: str) -> None:
        self._store = Store.open(root)

    @classmethod
    def for_store(cls, store: Store) -> "AuditJournal":
        """Open the online journal belonging to the given record store."""

        return cls(os.path.join(audit_root(store.root), "online"))

    @property
    def root(self) -> str:
        return self._store.root

    def append(self, record: Record) -> Record:
        """Persist one record before it becomes visible in memory."""

        self._store.save(event_slug(record.event), record.id, record.as_dict())
        return record

    def delete(self, record: Record) -> bool:
        """Remove one persisted record, reporting whether it was present."""

        return self._store.delete(event_slug(record.event), record.id)

    def scan(self) -> list[Record]:
        """Every online record, ordered by timestamp then identifier."""

        records: list[Record] = []
        for kind in self._store.kinds():
            for record_id in self._store.list(kind):
                payload = self._store.load(kind, record_id)
                if payload is None:
                    continue
                records.append(
                    Record(
                        id=str(payload.get("id", record_id)),
                        event=str(payload.get("event", "")),
                        at=str(payload.get("at", "")),
                    )
                )
        return sorted(records, key=lambda record: (record.at, record.id))

    def count(self) -> int:
        return self._store.file_count()

    def describe(self) -> str:
        return f"audit journal records={self.count()}"
