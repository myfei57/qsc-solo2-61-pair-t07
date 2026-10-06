"""Immutable daily archive packs and their manifest."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from tunnelnet.drain import utc_now
from tunnelnet.store import Store

from .journal import audit_root
from .model import Record
from .summary import count_by_event

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MANIFEST_NAME = "manifest.json"


@dataclass(frozen=True)
class PackInfo:
    """Manifest entry describing one sealed archive pack."""

    day: str
    file: str
    records: int
    events: dict[str, int]
    sha256: str
    sealed_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "day": self.day,
            "file": self.file,
            "records": self.records,
            "events": dict(self.events),
            "sha256": self.sha256,
            "sealed_at": self.sealed_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "PackInfo":
        events = payload.get("events", {})
        return cls(
            day=str(payload.get("day", "")),
            file=str(payload.get("file", "")),
            records=int(payload.get("records", 0)),  # type: ignore[arg-type]
            events={str(k): int(v) for k, v in dict(events).items()},  # type: ignore[arg-type]
            sha256=str(payload.get("sha256", "")),
            sealed_at=str(payload.get("sealed_at", "")),
        )


def _check_day(day: str) -> None:
    if not _DAY.match(day):
        raise ValueError(f"invalid archive day {day!r}")


class ArchiveStore:
    """Sealed, never rewritten packs of expired audit records.

    Archiving is grouped by UTC day: one ``<day>.jsonl`` pack plus one entry
    in ``manifest.json``.  A day that is already sealed is returned as-is and
    never rewritten, so sealed history is immutable; the manifest records the
    pack checksum so later tampering is detectable.  Both the pack file and
    the manifest are written atomically (temporary file then rename).
    """

    def __init__(self, root: str, clock: Callable[[], str] = utc_now) -> None:
        self._root = os.path.abspath(root)
        self._clock = clock
        self._lock = threading.RLock()
        os.makedirs(self._root, exist_ok=True)

    @classmethod
    def for_store(cls, store: Store, clock: Callable[[], str] = utc_now) -> "ArchiveStore":
        """Open the archive belonging to the given record store."""

        return cls(os.path.join(audit_root(store.root), "archive"), clock=clock)

    @property
    def root(self) -> str:
        return self._root

    def _manifest_path(self) -> str:
        return os.path.join(self._root, MANIFEST_NAME)

    def _pack_path(self, day: str) -> str:
        _check_day(day)
        return os.path.join(self._root, f"{day}.jsonl")

    def _load_manifest(self) -> dict[str, PackInfo]:
        try:
            with open(self._manifest_path(), encoding="utf-8") as handle:
                payload = json.loads(handle.read())
        except FileNotFoundError:
            return {}
        except ValueError as exc:
            raise ValueError("audit archive manifest is corrupt") from exc
        packs: dict[str, PackInfo] = {}
        for entry in payload.get("packs", []):
            info = PackInfo.from_dict(entry)
            packs[info.day] = info
        return packs

    def _save_manifest(self, packs: dict[str, PackInfo]) -> None:
        payload = {"packs": [packs[day].as_dict() for day in sorted(packs)]}
        temporary = f"{self._manifest_path()}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        os.replace(temporary, self._manifest_path())

    def packs(self) -> list[PackInfo]:
        """Every sealed pack, ordered by day."""

        with self._lock:
            packs = self._load_manifest()
        return [packs[day] for day in sorted(packs)]

    def pack(self, day: str) -> PackInfo | None:
        """The sealed pack for one day, or ``None`` when it is not archived."""

        _check_day(day)
        with self._lock:
            return self._load_manifest().get(day)

    def seal(self, day: str, records: Sequence[Record]) -> PackInfo:
        """Seal one day of records into a pack, once.

        Sealing an already sealed day is a no-op that returns the existing
        entry: sealed packs are immutable.  The pack file becomes durable
        before the manifest entry, so a crash in between simply leaves an
        unsealed file that the next run rewrites.
        """

        _check_day(day)
        ordered = sorted(records, key=lambda record: (record.at, record.id))
        with self._lock:
            packs = self._load_manifest()
            existing = packs.get(day)
            if existing is not None:
                return existing
            blob = "".join(
                json.dumps(record.as_dict(), ensure_ascii=False, sort_keys=True) + "\n"
                for record in ordered
            ).encode("utf-8")
            pack_path = self._pack_path(day)
            temporary = f"{pack_path}.tmp"
            with open(temporary, "wb") as handle:
                handle.write(blob)
            os.replace(temporary, pack_path)
            info = PackInfo(
                day=day,
                file=f"{day}.jsonl",
                records=len(ordered),
                events=count_by_event(ordered),
                sha256=hashlib.sha256(blob).hexdigest(),
                sealed_at=self._clock(),
            )
            packs[day] = info
            self._save_manifest(packs)
            return info

    def read(self, day: str) -> list[Record]:
        """The records of one sealed pack, in stored order."""

        records: list[Record] = []
        with open(self._pack_path(day), encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                    records.append(
                        Record(
                            id=str(payload["id"]),
                            event=str(payload["event"]),
                            at=str(payload["at"]),
                        )
                    )
                except (ValueError, KeyError, TypeError) as exc:
                    raise ValueError(f"audit archive pack {day} is corrupt") from exc
        return records

    def ids(self, day: str) -> set[str]:
        """Identifiers stored in one sealed pack, used to gate deletion."""

        return {record.id for record in self.read(day)}

    def query(self, event: str = "", start: str = "", end: str = "") -> list[Record]:
        """Archived records filtered by event and an inclusive time range."""

        matched: list[Record] = []
        for info in self.packs():
            if start and info.day < start[:10]:
                continue
            if end and info.day > end[:10]:
                continue
            for record in self.read(info.day):
                if event and record.event != event:
                    continue
                if start and record.at < start:
                    continue
                if end and record.at > end:
                    continue
                matched.append(record)
        return sorted(matched, key=lambda record: (record.at, record.id))

    def verify(self, day: str = "") -> dict[str, object]:
        """Recompute pack checksums against the manifest."""

        packs = self._load_manifest()
        days = [day] if day else sorted(packs)
        results: list[dict[str, object]] = []
        intact = True
        for name in days:
            info = packs.get(name)
            if info is None:
                results.append({"day": name, "ok": False, "detail": "no such pack"})
                intact = False
                continue
            try:
                with open(self._pack_path(name), "rb") as handle:
                    digest = hashlib.sha256(handle.read()).hexdigest()
            except FileNotFoundError:
                results.append({"day": name, "ok": False, "detail": "pack file missing"})
                intact = False
                continue
            ok = digest == info.sha256
            results.append(
                {
                    "day": name,
                    "ok": ok,
                    "detail": "intact" if ok else "checksum mismatch",
                    "sha256": digest,
                }
            )
            intact = intact and ok
        return {"ok": intact, "packs": results}

    def describe(self) -> str:
        packs = self.packs()
        return f"audit archive packs={len(packs)} records={sum(pack.records for pack in packs)}"
