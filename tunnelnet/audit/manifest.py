"""Append only registry of sealed archive packages.

Every finalized package is described twice:

* a sidecar ``<id>.manifest.json`` beside the tar, holding per segment hashes
  and the hash of the tar itself;
* one line appended to ``manifest.log``, the durable index consulted by
  retrieval and by the retention gate ("a record is archived" means a sealed
  package covers its exact segment file).

The log is WORM: entries are only ever appended, never rewritten, so archived
history stays independently verifiable even if the tars are copied elsewhere.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from . import layout


@dataclass(frozen=True)
class SegmentEntry:
    """One segment captured by a package, with its content hash."""

    path: str  # relative path: "<day>/<event>.<gen>.jsonl"
    records: int
    bytes: int
    sha256: str

    def as_dict(self) -> dict[str, object]:
        return {"path": self.path, "records": self.records, "bytes": self.bytes, "sha256": self.sha256}

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "SegmentEntry":
        return cls(
            path=str(payload["path"]),
            records=int(payload["records"]),
            bytes=int(payload["bytes"]),
            sha256=str(payload["sha256"]),
        )


@dataclass(frozen=True)
class PackageManifest:
    """Sealed description of one archive package."""

    package_id: str
    day: str
    sequence: int
    created_at: str
    record_count: int
    tar_sha256: str
    segments: list[SegmentEntry] = field(default_factory=list)

    @property
    def segment_paths(self) -> frozenset[str]:
        return frozenset(item.path for item in self.segments)

    @property
    def package_file(self) -> str:
        return layout.package_name(self.day, self.sequence)

    @property
    def manifest_file(self) -> str:
        return layout.manifest_name(self.day, self.sequence)

    def as_dict(self) -> dict[str, object]:
        return {
            "package_id": self.package_id,
            "day": self.day,
            "sequence": self.sequence,
            "created_at": self.created_at,
            "record_count": self.record_count,
            "tar_sha256": self.tar_sha256,
            "segments": [item.as_dict() for item in self.segments],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "PackageManifest":
        raw_segments = payload.get("segments", [])
        segments = [
            SegmentEntry.from_dict(item)
            for item in raw_segments
            if isinstance(item, dict)
        ]
        return cls(
            package_id=str(payload["package_id"]),
            day=str(payload["day"]),
            sequence=int(payload["sequence"]),
            created_at=str(payload["created_at"]),
            record_count=int(payload["record_count"]),
            tar_sha256=str(payload["tar_sha256"]),
            segments=segments,
        )


def encode_log_line(manifest: PackageManifest) -> str:
    return json.dumps(manifest.as_dict(), ensure_ascii=False, sort_keys=True)


def write_sidecar(root: str, manifest: PackageManifest) -> str:
    path = os.path.join(layout.archive_dir(root), manifest.manifest_file)
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(manifest.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return path


def append_log(root: str, manifest: PackageManifest) -> None:
    """Append one sealed package to the WORM index (idempotent by id)."""

    existing = {item.package_id for item in read_log(root)}
    if manifest.package_id in existing:
        return
    path = layout.manifest_log_path(root)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(encode_log_line(manifest) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_log(root: str) -> list[PackageManifest]:
    path = layout.manifest_log_path(root)
    manifests: list[PackageManifest] = []
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except FileNotFoundError:
        return []
    seen: set[str] = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        manifest = PackageManifest.from_dict(payload)
        if manifest.package_id in seen:
            continue
        seen.add(manifest.package_id)
        manifests.append(manifest)
    manifests.sort(key=lambda item: (item.day, item.sequence))
    return manifests


def next_sequence(root: str, day: str) -> int:
    sequences = [item.sequence for item in read_log(root) if item.day == day]
    return (max(sequences) + 1) if sequences else 1


def covered_segments(root: str) -> dict[str, str]:
    """Map archived relative segment path -> package id."""

    coverage: dict[str, str] = {}
    for manifest in read_log(root):
        for entry in manifest.segments:
            coverage.setdefault(entry.path, manifest.package_id)
    return coverage
