"""Behavioural tests for durable audit persistence, archiving and retention."""

from __future__ import annotations

import os
import tarfile
import tempfile
import unittest

from tunnelnet.audit import (
    ArchiveIntegrityError,
    AuditService,
    AuditSettings,
    ImmutableAuditError,
    Record,
)
from tunnelnet.audit import archive_reader, manifest as manifest_module
from tunnelnet.audit.layout import (
    PARTIAL_SUFFIX,
    archive_dir,
    day_dir,
    live_dir,
    manifest_log_path,
)
from tunnelnet.audit.timeutil import shift_day


class Clock:
    """Fixed UTC day clock that tests can advance."""

    def __init__(self, day: str = "2026-10-05", hour: int = 12) -> None:
        self.day = day
        self.hour = hour

    def __call__(self) -> str:
        return f"{self.day}T{self.hour:02d}:00:00Z"


def make_service(root: str, online_days: int = 2, **kwargs) -> AuditService:
    settings = AuditSettings(
        online_days=online_days,
        max_segment_bytes=kwargs.pop("max_segment_bytes", 4096),
        max_day_bytes=kwargs.pop("max_day_bytes", 4096),
    )
    return AuditService(root, settings=settings, clock=kwargs.pop("clock", Clock()))


class JournalCase(unittest.TestCase):
    def test_records_persist_by_event_and_day_and_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            service = make_service(root)
            service.append(Record(id="a1", event="access_granted", at="2026-10-04T08:00:00Z"))
            service.append(Record(id="f1", event="fire_detect", at="2026-10-04T09:00:00Z"))
            self.assertTrue(
                os.path.exists(os.path.join(day_dir(root, "2026-10-04"), "access_granted.0001.jsonl"))
            )
            self.assertTrue(
                os.path.exists(os.path.join(day_dir(root, "2026-10-04"), "fire_detect.0001.jsonl"))
            )

            restarted = make_service(root)
            self.assertEqual({r.id for r in restarted.records()}, {"a1", "f1"})
            self.assertEqual(restarted.latest_id(), "f1")

    def test_journal_is_append_only(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            service = make_service(root)
            service.append(Record(id="a1", event="access_granted", at="2026-10-05T08:00:00Z"))
            with self.assertRaises(ImmutableAuditError):
                service.clear()
            self.assertEqual(service.count(), 1)

    def test_size_guard_rolls_generations(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            service = make_service(root, max_segment_bytes=200)
            for index in range(20):
                service.append(
                    Record(
                        id=f"r{index}",
                        event="access_granted",
                        at=f"2026-10-05T08:00:{index:02d}Z",
                        detail={"note": "x" * 40},
                    )
                )
            names = os.listdir(day_dir(root, "2026-10-05"))
            self.assertGreater(len(names), 1)
            self.assertEqual(len(service.records()), 20)


class ArchiveCase(unittest.TestCase):
    def test_daily_batch_packages_past_days_and_keeps_live_window(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            clock = Clock()
            service = make_service(root, clock=clock)
            old_day = shift_day(clock.day, -3)
            in_window = shift_day(clock.day, -1)
            service.append(Record(id="old", event="access_granted", at=f"{old_day}T08:00:00Z"))
            service.append(Record(id="win", event="cycle", at=f"{in_window}T08:00:00Z"))

            result = service.archive_due()
            self.assertEqual(set(result["sealed"]), {f"{old_day}.0001", f"{in_window}.0001"})
            # Packaging never removes the online copies itself.
            self.assertTrue(os.path.isdir(day_dir(root, old_day)))
            self.assertTrue(os.path.exists(os.path.join(archive_dir(root), f"{old_day}.0001.tar.gz")))
            self.assertTrue(os.path.exists(os.path.join(archive_dir(root), f"{old_day}.0001.manifest.json")))

            archived = service.search(scope="archive")
            self.assertEqual({r.id for r in archived}, {"old", "win"})
            self.assertEqual({r.id for r in service.search()}, {"old", "win"})

    def test_archiving_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            clock = Clock()
            service = make_service(root, clock=clock)
            old_day = shift_day(clock.day, -2)
            service.append(Record(id="old", event="access_granted", at=f"{old_day}T08:00:00Z"))
            first = service.archive_due()
            second = service.archive_due()
            self.assertEqual(first["sealed"], [f"{old_day}.0001"])
            self.assertEqual(second["sealed"], [])
            self.assertEqual(len(manifest_module.read_log(root)), 1)

    def test_size_fallback_packages_current_day(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            clock = Clock()
            service = make_service(root, online_days=7, max_day_bytes=500, clock=clock)
            for index in range(20):
                service.append(
                    Record(
                        id=f"r{index}",
                        event="access_granted",
                        at=f"{clock.day}T08:{index:02d}:00Z",
                        detail={"note": "y" * 40},
                    )
                )
            result = service.archive_due()
            self.assertEqual(result["sealed"], [f"{clock.day}.0001"])
            # New same day records land in fresh generations and archive later.
            service.append(Record(id="late", event="access_granted", at=f"{clock.day}T09:00:00Z"))
            self.assertEqual(len(service.live_records()), 21)

    def test_recover_discards_partial_package_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            clock = Clock()
            service = make_service(root, clock=clock)
            old_day = shift_day(clock.day, -2)
            service.append(Record(id="old", event="access_granted", at=f"{old_day}T08:00:00Z"))
            partial = os.path.join(archive_dir(root), f"{old_day}.9999.tar.gz{PARTIAL_SUFFIX}")
            with open(partial, "wb") as handle:
                handle.write(b"not a package")
            result = service.archive_due()
            self.assertEqual(result["sealed"], [f"{old_day}.0001"])
            self.assertEqual(result["removed_partials"], [os.path.basename(partial)])
            self.assertFalse(os.path.exists(partial))

    def test_tampered_archive_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            clock = Clock()
            service = make_service(root, clock=clock)
            old_day = shift_day(clock.day, -2)
            service.append(Record(id="old", event="access_granted", at=f"{old_day}T08:00:00Z"))
            service.archive_due()
            package = os.path.join(archive_dir(root), f"{old_day}.0001.tar.gz")
            with open(package, "r+b") as handle:
                handle.seek(40)
                handle.write(b"\x00" * 8)
            with self.assertRaises(ArchiveIntegrityError):
                service.search(scope="archive", verify=True)

    def test_archives_are_worm_and_verifiable(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            clock = Clock()
            service = make_service(root, clock=clock)
            old_day = shift_day(clock.day, -2)
            service.append(Record(id="old", event="access_granted", at=f"{old_day}T08:00:00Z"))
            service.archive_due()
            manifests = manifest_module.read_log(root)
            self.assertTrue(archive_reader.verify_package(root, manifests[0]))
            package = archive_reader.read_package(root, manifests[0], verify=True)
            self.assertEqual([r.id for r in package], ["old"])
            with tarfile.open(os.path.join(archive_dir(root), manifests[0].package_file)) as tar:
                self.assertIn("package-manifest.json", tar.getnames())


class RetentionCase(unittest.TestCase):
    def test_archive_then_delete_gate(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            clock = Clock()
            service = make_service(root, online_days=2, clock=clock)
            expired = shift_day(clock.day, -3)
            in_window = shift_day(clock.day, -1)
            service.append(Record(id="old", event="access_granted", at=f"{expired}T08:00:00Z"))
            service.append(Record(id="win", event="cycle", at=f"{in_window}T08:00:00Z"))

            result = service.run_retention()
            self.assertIn(f"{expired}.0001", result["archived_packages"])
            self.assertEqual(result["deleted_segments"], [f"{expired}/access_granted.0001.jsonl"])
            self.assertEqual(result["deleted_records"], 1)
            self.assertEqual(result["blocked"], [])
            self.assertFalse(os.path.exists(day_dir(root, expired)))
            self.assertTrue(os.path.exists(day_dir(root, in_window)))
            # The deleted record is still retrievable from its archive.
            self.assertIn("old", [r.id for r in service.search(scope="archive")])
            self.assertEqual({r.id for r in service.search()}, {"old", "win"})

    def test_no_delete_when_archive_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            clock = Clock()
            service = make_service(root, online_days=1, clock=clock)
            expired = shift_day(clock.day, -2)
            service.append(Record(id="old", event="access_granted", at=f"{expired}T08:00:00Z"))

            def fail_build(self, part_path, segments, package_manifest):  # noqa: ANN001
                raise RuntimeError("disk full while packaging")

            from tunnelnet.audit.archiver import Archiver

            original = Archiver._build_part
            Archiver._build_part = fail_build
            try:
                result = service.run_retention()
            finally:
                Archiver._build_part = original
            self.assertEqual(result["archived_packages"], [])
            self.assertIn("disk full", result["archive_error"])
            self.assertEqual(result["blocked"], [f"{expired}/access_granted.0001.jsonl"])
            self.assertEqual(result["deleted_segments"], [])
            self.assertTrue(os.path.exists(day_dir(root, expired)))

            # Retry after recovery archives and then deletes -- idempotent resume.
            recovered = service.run_retention()
            self.assertEqual(recovered["deleted_segments"], [f"{expired}/access_granted.0001.jsonl"])

    def test_partial_failure_is_idempotent_on_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            clock = Clock()
            service = make_service(root, online_days=1, clock=clock)
            day_a = shift_day(clock.day, -2)
            day_b = shift_day(clock.day, -3)
            service.append(Record(id="a", event="access_granted", at=f"{day_a}T08:00:00Z"))
            service.append(Record(id="b", event="cycle", at=f"{day_b}T08:00:00Z"))
            result = service.run_retention()
            self.assertEqual(len(result["deleted_segments"]), 2)
            again = service.run_retention()
            self.assertEqual(again["deleted_segments"], [])
            self.assertEqual(again["blocked"], [])
            self.assertEqual(again["failed"], [])

    def test_hash_mismatch_blocks_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            clock = Clock()
            service = make_service(root, online_days=1, clock=clock)
            expired = shift_day(clock.day, -2)
            service.append(Record(id="old", event="fire_detect", at=f"{expired}T08:00:00Z"))
            service.archive_due()
            live_file = os.path.join(day_dir(root, expired), "fire_detect.0001.jsonl")
            with open(live_file, "a", encoding="utf-8") as handle:
                handle.write('{"id":"x","event":"fire_detect","at":"%sT09:00:00Z"}\n' % expired)
            result = service.run_retention()
            self.assertEqual(result["blocked"], [f"{expired}/fire_detect.0001.jsonl"])
            self.assertTrue(os.path.exists(live_file))

    def test_dry_run_reports_without_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            clock = Clock()
            service = make_service(root, online_days=1, clock=clock)
            expired = shift_day(clock.day, -2)
            service.append(Record(id="old", event="cycle", at=f"{expired}T08:00:00Z"))
            result = service.run_retention(dry_run=True)
            self.assertEqual(result["deleted_segments"], [f"{expired}/cycle.0001.jsonl"])
            self.assertTrue(os.path.exists(day_dir(root, expired)))


class RetrievalCase(unittest.TestCase):
    def test_search_filters_time_event_and_scope(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            clock = Clock()
            service = make_service(root, online_days=1, clock=clock)
            old_day = shift_day(clock.day, -2)
            service.append(Record(id="old-a", event="access_granted", at=f"{old_day}T08:00:00Z"))
            service.append(Record(id="old-f", event="fire_detect", at=f"{old_day}T09:00:00Z"))
            service.append(Record(id="new", event="access_granted", at=f"{clock.day}T08:00:00Z"))
            service.run_retention()

            self.assertEqual(
                {r.id for r in service.search(event="access_granted")},
                {"old-a", "new"},
            )
            self.assertEqual(
                [r.id for r in service.search(scope="archive", event="fire_detect")],
                ["old-f"],
            )
            windowed = service.search(start=f"{old_day}T08:30:00Z")
            self.assertEqual({r.id for r in windowed}, {"old-f", "new"})
            only = service.search(scope="live")
            self.assertEqual([r.id for r in only], ["new"])

    def test_counts_dedupe_records_in_both_window_and_archive(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            clock = Clock()
            service = make_service(root, online_days=5, clock=clock)
            old_day = shift_day(clock.day, -1)
            service.append(Record(id="old", event="cycle", at=f"{old_day}T08:00:00Z"))
            service.archive_due()
            counts = service.counts()
            self.assertEqual(counts["live"], 1)
            self.assertEqual(counts["archived"], 1)
            self.assertEqual(counts["total"], 1)

    def test_settings_persist_across_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            service = make_service(root)
            service.update_settings(online_days=14, max_day_bytes=102400)
            restarted = AuditService(root)
            self.assertEqual(restarted.settings.online_days, 14)
            self.assertEqual(restarted.settings.max_day_bytes, 102400)


if __name__ == "__main__":
    unittest.main()
