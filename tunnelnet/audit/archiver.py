"""Build immutable archive packages out of sealed live segments.

Packaging order (every step crash safe and idempotent):

1. Select candidate segments: whole past days (daily batch) or a day whose
   live bytes pass the size guard (size fallback). Segments already covered
   by a sealed package are skipped so a rerun never packages them twice.
2. Hash the segment files, then build ``archive/<day>.<seq>.tar.gz`` into a
   ``.part`` file while streaming the tar bytes through a SHA-256 hasher.
3. Verify the part: reopen it, compare its checksum and re-extract every
   member, comparing per segment hashes. Only then atomically rename it.
4. Write the sidecar manifest and append the WORM manifest log line.

The live copies are deliberately **kept** after sealing: packaging and
deletion are separate phases. The retention sweeper later removes a live
segment only after the WORM manifest proves an identical sealed copy, which
is exactly the archive-before-delete ordering the system requires. If the
process dies mid packaging the package is simply not sealed: live files stay
in place, no manifest is written, and the ``.part`` file is discarded by
:meth:`recover` so the next run retries from scratch.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import tarfile
import threading
from dataclasses import dataclass, field

from . import layout, manifest as manifest_module
from .journal import PersistentAuditRegistry
from .segment import Segment, list_live_days
from .timeutil import today, utc_now

TAR_MANIFEST_MEMBER = "package-manifest.json"
HASH_CHUNK = 64 * 1024


@dataclass(frozen=True)
class ArchiveResult:
    """Outcome of one packaging pass."""

    sealed: list[str] = field(default_factory=list)
    skipped_segments: list[str] = field(default_factory=list)
    removed_partials: list[str] = field(default_factory=list)
    candidates: int = 0
    error: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "sealed": self.sealed,
            "skipped_segments": self.skipped_segments,
            "removed_partials": self.removed_partials,
            "candidates": self.candidates,
            "error": self.error,
        }


class Archiver:
    """Packages live segments into WORM compressed tar archives."""

    def __init__(
        self,
        registry: PersistentAuditRegistry,
        max_day_bytes: int = 32 * 1024 * 1024,
        clock=utc_now,
    ) -> None:
        self._registry = registry
        self._root = registry.root
        self._max_day_bytes = max(1, max_day_bytes)
        self._clock = clock
        self._lock = threading.RLock()

    @property
    def max_day_bytes(self) -> int:
        return self._max_day_bytes

    def candidate_days(self, force_current: bool = False) -> list[str]:
        """Days that currently hold packageable, not fully archived data."""

        covered = manifest_module.covered_segments(self._root)
        current = today(self._clock)
        result: list[str] = []
        for day in self._live_days():
            segments = self._uncovered(day, covered)
            if not segments:
                continue
            if day < current:
                result.append(day)
            elif force_current or self._registry.day_bytes(day) >= self._max_day_bytes:
                result.append(day)
        return result

    def _live_days(self) -> list[str]:
        return list_live_days(self._root)

    def _uncovered(self, day: str, covered: dict[str, str]) -> list[Segment]:
        return [
            segment
            for segment in self._registry.live_segments([day])
            if segment.relative not in covered
        ]

    def run_once(self, force_current: bool = False) -> ArchiveResult:
        """Package every candidate day, tolerating and reporting failures."""

        with self._lock:
            recovered = self.recover()
            sealed: list[str] = []
            skipped: list[str] = []
            errors: list[str] = []
            days = self.candidate_days(force_current=force_current)
            for day in days:
                try:
                    package_id = self._package_day(day)
                    if package_id:
                        sealed.append(package_id)
                    else:
                        skipped.append(day)
                except Exception as exc:  # noqa: BLE001 - one bad day must not abort the batch
                    errors.append(f"{day}: {exc}")
            return ArchiveResult(
                sealed=sealed,
                skipped_segments=skipped,
                removed_partials=recovered.removed_partials,
                candidates=len(days),
                error="; ".join(errors),
            )

    def _package_day(self, day: str) -> str:
        covered = manifest_module.covered_segments(self._root)
        segments = self._uncovered(day, covered)
        if not segments:
            return ""
        sequence = manifest_module.next_sequence(self._root, day)
        package_id = layout.package_id_for(day, sequence)
        entries = [self._hash_segment(segment) for segment in segments]
        record_count = sum(entry.records for entry in entries)
        package_manifest = manifest_module.PackageManifest(
            package_id=package_id,
            day=day,
            sequence=sequence,
            created_at=self._clock(),
            record_count=record_count,
            tar_sha256="",
            segments=entries,
        )
        tar_path = os.path.join(layout.archive_dir(self._root), layout.package_name(day, sequence))
        part_path = tar_path + layout.PARTIAL_SUFFIX
        tar_hash = self._build_part(part_path, segments, package_manifest)
        package_manifest = manifest_module.PackageManifest(
            package_id=package_id,
            day=day,
            sequence=sequence,
            created_at=package_manifest.created_at,
            record_count=record_count,
            tar_sha256=tar_hash,
            segments=entries,
        )
        self._verify_part(part_path, tar_hash, package_manifest)
        os.replace(part_path, tar_path)
        manifest_module.write_sidecar(self._root, package_manifest)
        manifest_module.append_log(self._root, package_manifest)
        return package_id

    def _hash_segment(self, segment: Segment) -> manifest_module.SegmentEntry:
        digest = hashlib.sha256()
        total = 0
        with open(segment.path, "rb") as handle:
            while True:
                block = handle.read(HASH_CHUNK)
                if not block:
                    break
                digest.update(block)
                total += len(block)
        return manifest_module.SegmentEntry(
            path=segment.relative,
            records=len(segment.records()),
            bytes=total,
            sha256=digest.hexdigest(),
        )

    def _build_part(
        self,
        part_path: str,
        segments: list[Segment],
        package_manifest: manifest_module.PackageManifest,
    ) -> str:
        digest = hashlib.sha256()
        if os.path.exists(part_path):
            os.remove(part_path)
        with open(part_path, "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
                with tarfile.open(fileobj=gz, mode="w") as tar:
                    for segment in segments:
                        tar.add(segment.path, arcname=segment.relative)
                    meta_bytes = self._manifest_bytes(package_manifest)
                    info = tarfile.TarInfo(name=TAR_MANIFEST_MEMBER)
                    info.size = len(meta_bytes)
                    tar.addfile(info, io.BytesIO(meta_bytes))
            raw.flush()
            os.fsync(raw.fileno())
        with open(part_path, "rb") as handle:
            while True:
                block = handle.read(HASH_CHUNK)
                if not block:
                    break
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _manifest_bytes(package_manifest: manifest_module.PackageManifest) -> bytes:
        import json

        return json.dumps(package_manifest.as_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8")

    def _verify_part(
        self,
        part_path: str,
        expected_hash: str,
        package_manifest: manifest_module.PackageManifest,
    ) -> None:
        digest = hashlib.sha256()
        with open(part_path, "rb") as handle:
            while True:
                block = handle.read(HASH_CHUNK)
                if not block:
                    break
                digest.update(block)
        if digest.hexdigest() != expected_hash:
            raise ArchiveError("archive checksum mismatch before sealing")
        expected = {entry.path: entry for entry in package_manifest.segments}
        with tarfile.open(part_path, mode="r:gz") as tar:
            members = {member.name: member for member in tar.getmembers()}
            for relative, entry in expected.items():
                member = members.get(relative)
                if member is None or not member.isfile():
                    raise ArchiveError(f"archive missing segment {relative}")
                extracted = tar.extractfile(member)
                if extracted is None:
                    raise ArchiveError(f"archive unreadable segment {relative}")
                member_hash = hashlib.sha256(extracted.read()).hexdigest()
                if member_hash != entry.sha256:
                    raise ArchiveError(f"segment hash mismatch in archive {relative}")
            if TAR_MANIFEST_MEMBER not in members:
                raise ArchiveError("archive missing embedded manifest")

    def recover(self) -> ArchiveResult:
        """Discard unfinished ``.part`` packages from an interrupted run.

        Live segments are never touched: an unsealed package means its data
        was never declared archived, so the next packaging pass retries it.
        """

        removed_partials: list[str] = []
        archive_directory = layout.archive_dir(self._root)
        for name in sorted(os.listdir(archive_directory)):
            if name.endswith(layout.PARTIAL_SUFFIX):
                os.remove(os.path.join(archive_directory, name))
                removed_partials.append(name)
        return ArchiveResult(removed_partials=removed_partials)

    @staticmethod
    def _same_hash(path: str, expected: str) -> bool:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while True:
                block = handle.read(HASH_CHUNK)
                if not block:
                    break
                digest.update(block)
        return digest.hexdigest() == expected


class ArchiveError(RuntimeError):
    """Raised when a package cannot be built or verified safely."""
