"""Online retention window with an archive first deletion gate.

Expiry policy (the chosen design): records are **never deleted directly** at
the end of the online window. A live segment may be removed only when the WORM
manifest shows an identical segment (same relative path and SHA-256) inside a
sealed, verified package. The gate is evaluated per segment, so a batch clean
that fails partway simply leaves the blocked files behind and the next sweep
retries them -- recovery is idempotent and archived history never changes.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field

from . import layout, manifest as manifest_module
from .archiver import Archiver
from .journal import PersistentAuditRegistry
from .segment import list_live_days
from .timeutil import shift_day, today, utc_now


@dataclass(frozen=True)
class RetentionPolicy:
    """Days kept in the online window before archival expiry."""

    online_days: int = 7

    def cutoff_day(self, current_day: str) -> str:
        """First day that is still inside the online window."""

        return shift_day(current_day, -self.online_days)

    def expired_days(self, day: str, current_day: str) -> bool:
        return day < self.cutoff_day(current_day)


@dataclass(frozen=True)
class PruneResult:
    """Partial failure aware outcome of one retention sweep."""

    cutoff_day: str
    deleted_segments: list[str] = field(default_factory=list)
    deleted_records: int = 0
    blocked: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    archived_packages: list[str] = field(default_factory=list)
    archive_error: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "cutoff_day": self.cutoff_day,
            "deleted_segments": self.deleted_segments,
            "deleted_records": self.deleted_records,
            "blocked": self.blocked,
            "failed": self.failed,
            "archived_packages": self.archived_packages,
            "archive_error": self.archive_error,
        }


class RetentionSweeper:
    """Enforces the online window after the archiver has sealed history."""

    def __init__(
        self,
        registry: PersistentAuditRegistry,
        archiver: Archiver,
        policy: RetentionPolicy | None = None,
        clock=utc_now,
    ) -> None:
        self._registry = registry
        self._archiver = archiver
        self._root = registry.root
        self.policy = policy or RetentionPolicy()
        self._clock = clock
        self._lock = threading.RLock()

    def expired_days(self) -> list[str]:
        current = today(self._clock)
        return [
            day for day in list_live_days(self._root) if self.policy.expired_days(day, current)
        ]

    def run_once(self, dry_run: bool = False) -> PruneResult:
        """Archive everything due first, then delete only gated segments."""

        with self._lock:
            self._archiver.recover()
            current = today(self._clock)
            cutoff = self.policy.cutoff_day(current)
            archive_result = self._archiver.run_once()
            coverage = manifest_module.covered_segments(self._root)

            deleted: list[str] = []
            blocked: list[str] = []
            failed: list[str] = []
            deleted_records = 0
            for day in list_live_days(self._root):
                if not self.policy.expired_days(day, current):
                    continue
                for segment in self._registry.live_segments([day]):
                    package_id = coverage.get(segment.relative)
                    if package_id is None:
                        # Archive did not succeed: deletion is forbidden.
                        blocked.append(segment.relative)
                        continue
                    live_path = os.path.join(layout.live_dir(self._root), segment.relative)
                    if not self._sealed_bytes_match(live_path, segment.relative, package_id):
                        blocked.append(segment.relative)
                        continue
                    if dry_run:
                        deleted.append(segment.relative)
                        deleted_records += len(segment.records())
                        continue
                    try:
                        count = len(segment.records())
                        os.remove(live_path)
                    except OSError as exc:
                        failed.append(f"{segment.relative}: {exc}")
                        continue
                    deleted.append(segment.relative)
                    deleted_records += count
                _remove_empty_day_dir(self._root, day)
            return PruneResult(
                cutoff_day=cutoff,
                deleted_segments=deleted,
                deleted_records=deleted_records,
                blocked=blocked,
                failed=failed,
                archived_packages=archive_result.sealed,
                archive_error=archive_result.error,
            )

    def _sealed_bytes_match(self, live_path: str, relative: str, package_id: str) -> bool:
        for sealed in manifest_module.read_log(self._root):
            if sealed.package_id != package_id:
                continue
            for entry in sealed.segments:
                if entry.path == relative:
                    return self._archiver._same_hash(  # noqa: SLF001 - internal gate
                        live_path, entry.sha256
                    )
        return False


def _remove_empty_day_dir(root: str, day: str) -> None:
    directory = layout.day_dir(root, day)
    try:
        if not os.listdir(directory):
            os.rmdir(directory)
    except FileNotFoundError:
        pass
