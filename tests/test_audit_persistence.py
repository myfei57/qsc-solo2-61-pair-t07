"""Persistence, archiving and retention tests for the linkage audit trail."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from tunnelnet.audit import (
    ArchiveStore,
    AuditJournal,
    AuditRegistry,
    Record,
    Retention,
    RetentionPolicy,
    event_slug,
    source_records,
)
from tunnelnet.console.server import Server
from tunnelnet.store import Store

FIXED_NOW = "2026-10-06T12:00:00Z"


def fixed_clock() -> str:
    return FIXED_NOW


def make_stack(tmp: str, online_days: int = 3, fail_days=()):
    store = Store.open(os.path.join(tmp, "data"))
    journal = AuditJournal.for_store(store)
    registry = AuditRegistry(journal)
    archive = FlakyArchive.for_store(store, clock=fixed_clock)
    archive.fail_days = set(fail_days)
    retention = Retention(
        registry, journal, archive, RetentionPolicy(online_days=online_days), clock=fixed_clock
    )
    return store, journal, registry, archive, retention


class FlakyArchive(ArchiveStore):
    """Archive that fails to seal chosen days, to exercise partial failure."""

    def __init__(self, *args, fail_days=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_days = set(fail_days)

    def seal(self, day, records):
        if day in self.fail_days:
            raise OSError(f"injected archive failure for {day}")
        return super().seal(day, records)


class JournalCase(unittest.TestCase):
    def test_records_are_persisted_by_event_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, journal, registry, _, _ = make_stack(tmp)
            registry.append(Record(id="r1", event="access_approved", at="2026-10-05T01:00:00Z"))
            registry.append(Record(id="r2", event="fire_alarm", at="2026-10-05T02:00:00Z"))
            online = os.path.join(store.root, ".audit", "online")
            self.assertTrue(os.path.exists(os.path.join(online, "access_approved", "r1.json")))
            self.assertTrue(os.path.exists(os.path.join(online, "fire_alarm", "r2.json")))
            self.assertEqual(journal.count(), 2)
            with open(os.path.join(online, "fire_alarm", "r2.json"), encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["event"], "fire_alarm")

    def test_event_slugs_are_directory_safe(self) -> None:
        self.assertEqual(event_slug("Fire Alarm!"), "fire_alarm")
        self.assertEqual(event_slug("***"), "event")
        self.assertEqual(event_slug("../../escape"), "escape")
        with tempfile.TemporaryDirectory() as tmp:
            store, _, registry, _, _ = make_stack(tmp)
            registry.append(Record(id="r9", event="../../escape", at="2026-10-05T03:00:00Z"))
            self.assertTrue(
                os.path.exists(os.path.join(store.root, ".audit", "online", "escape", "r9.json"))
            )

    def test_restart_recovers_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, journal, registry, _, _ = make_stack(tmp)
            registry.append(Record(id="r2", event="fire", at="2026-10-05T02:00:00Z"))
            registry.append(Record(id="r1", event="access", at="2026-10-05T01:00:00Z"))
            registry.append(Record(id="r3", event="access", at="2026-10-05T03:00:00Z"))

            restarted = AuditRegistry(AuditJournal.for_store(store))
            self.assertEqual(restarted.count(), 3)
            self.assertEqual([record.id for record in restarted.records()], ["r1", "r2", "r3"])
            self.assertEqual(restarted.latest_id(), "r3")
            self.assertEqual(restarted.count_by_event(), {"access": 2, "fire": 1})


class RetentionCase(unittest.TestCase):
    def test_expired_records_are_archived_then_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, journal, registry, archive, retention = make_stack(tmp, online_days=3)
            registry.append(Record(id="old-1", event="access", at="2026-10-01T08:00:00Z"))
            registry.append(Record(id="old-2", event="access", at="2026-10-02T09:00:00Z"))
            registry.append(Record(id="old-3", event="fire", at="2026-10-02T10:00:00Z"))
            registry.append(Record(id="keep-1", event="access", at="2026-10-05T08:00:00Z"))
            registry.append(Record(id="keep-2", event="fire", at="2026-10-06T08:00:00Z"))

            report = retention.run()
            self.assertTrue(report.ok)
            self.assertEqual(report.cutoff, "2026-10-04")
            self.assertEqual([pack["day"] for pack in report.archived], ["2026-10-01", "2026-10-02"])
            self.assertEqual(report.deleted, 3)
            self.assertEqual(report.deleted_by_day, {"2026-10-01": 1, "2026-10-02": 2})
            self.assertEqual(report.kept, 0)

            self.assertEqual([record.id for record in registry.records()], ["keep-1", "keep-2"])
            self.assertFalse(journal.delete(Record(id="old-1", event="access", at="2026-10-01T08:00:00Z")))

            packs = archive.packs()
            self.assertEqual([pack.day for pack in packs], ["2026-10-01", "2026-10-02"])
            self.assertEqual(packs[1].events, {"access": 1, "fire": 1})
            archived_ids = {record.id for record in archive.query()}
            self.assertEqual(archived_ids, {"old-1", "old-2", "old-3"})

            status = retention.status()
            self.assertEqual(status["online"]["records"], 2)
            self.assertEqual(status["online"]["expired"], 0)
            self.assertEqual(status["archive"]["records"], 3)
            self.assertEqual(status["pending_days"], [])

    def test_rerun_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, registry, archive, retention = make_stack(tmp)
            registry.append(Record(id="old-1", event="access", at="2026-10-01T08:00:00Z"))
            first = retention.run()
            self.assertEqual(first.deleted, 1)
            pack_path = os.path.join(archive.root, "2026-10-01.jsonl")
            with open(pack_path, "rb") as handle:
                sealed_bytes = handle.read()
            with open(os.path.join(archive.root, "manifest.json"), encoding="utf-8") as handle:
                manifest_text = handle.read()

            second = retention.run()
            self.assertTrue(second.ok)
            self.assertEqual(second.archived, [])
            self.assertEqual(second.deleted, 0)
            self.assertEqual(second.kept, 0)
            with open(pack_path, "rb") as handle:
                self.assertEqual(handle.read(), sealed_bytes)
            with open(os.path.join(archive.root, "manifest.json"), encoding="utf-8") as handle:
                self.assertEqual(handle.read(), manifest_text)

    def test_delete_is_blocked_until_archive_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, journal, registry, archive, retention = make_stack(tmp, fail_days={"2026-10-01"})
            registry.append(Record(id="old-1", event="access", at="2026-10-01T08:00:00Z"))

            report = retention.run()
            self.assertFalse(report.ok)
            self.assertEqual(report.failures[0]["stage"], "archive")
            self.assertEqual(report.deleted, 0)
            self.assertEqual(report.kept, 1)
            self.assertEqual(registry.count(), 1)  # archive failed, so nothing was deleted
            self.assertEqual(archive.packs(), [])

            archive.fail_days.clear()
            recovered = retention.run()
            self.assertTrue(recovered.ok)
            self.assertEqual(recovered.deleted, 1)
            self.assertEqual(registry.count(), 0)
            self.assertEqual(journal.count(), 0)

    def test_partial_failure_is_confined_to_one_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, registry, archive, retention = make_stack(tmp, fail_days={"2026-10-02"})
            registry.append(Record(id="day-1", event="access", at="2026-10-01T08:00:00Z"))
            registry.append(Record(id="day-2", event="fire", at="2026-10-02T08:00:00Z"))

            report = retention.run()
            self.assertFalse(report.ok)
            self.assertEqual(report.deleted_by_day, {"2026-10-01": 1})
            self.assertEqual([record.id for record in registry.records()], ["day-2"])
            self.assertEqual([pack.day for pack in archive.packs()], ["2026-10-01"])

            archive.fail_days.clear()
            recovered = retention.run()
            self.assertTrue(recovered.ok)
            self.assertEqual(recovered.deleted, 1)
            self.assertEqual(registry.count(), 0)
            self.assertEqual([pack.day for pack in archive.packs()], ["2026-10-01", "2026-10-02"])

    def test_record_missing_from_sealed_pack_is_never_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, registry, archive, retention = make_stack(tmp)
            registry.append(Record(id="old-1", event="access", at="2026-10-01T08:00:00Z"))
            self.assertEqual(retention.run().deleted, 1)

            # A backdated record for an already sealed day cannot be archived
            # (the pack is immutable) and therefore must not be deleted.
            registry.append(Record(id="late-1", event="access", at="2026-10-01T09:00:00Z"))
            report = retention.run()
            self.assertTrue(report.ok)
            self.assertEqual(report.deleted, 0)
            self.assertEqual(report.kept, 1)
            self.assertEqual([record.id for record in registry.records()], ["late-1"])
            self.assertEqual(archive.ids("2026-10-01"), {"old-1"})

    def test_undatable_records_are_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, registry, archive, retention = make_stack(tmp)
            registry.append(Record(id="odd", event="manual", at="not-a-date"))
            report = retention.run()
            self.assertTrue(report.ok)
            self.assertEqual(report.deleted, 0)
            self.assertEqual(registry.count(), 1)
            self.assertEqual(archive.packs(), [])

    def test_clear_only_removes_archived_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, registry, archive, retention = make_stack(tmp)
            old = registry.append(Record(id="old-1", event="access", at="2026-10-01T08:00:00Z"))
            registry.append(Record(id="keep-1", event="access", at="2026-10-06T08:00:00Z"))
            archive.seal("2026-10-01", [old])

            outcome = retention.clear_archived()
            self.assertEqual(outcome, {"cleared": 1, "count": 1})
            self.assertEqual([record.id for record in registry.records()], ["keep-1"])


class ArchiveCase(unittest.TestCase):
    def test_sealed_pack_is_immutable_and_verifiable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, _, archive, _ = make_stack(tmp)
            records = [Record(id="a", event="access", at="2026-10-01T08:00:00Z")]
            info = archive.seal("2026-10-01", records)
            again = archive.seal(
                "2026-10-01", [Record(id="b", event="fire", at="2026-10-01T09:00:00Z")]
            )
            self.assertEqual(again, info)  # resealing is a no-op
            self.assertEqual(archive.ids("2026-10-01"), {"a"})
            self.assertEqual(archive.verify()["ok"], True)

            pack_path = os.path.join(archive.root, "2026-10-01.jsonl")
            with open(pack_path, "a", encoding="utf-8") as handle:
                handle.write('{"id": "forged", "event": "access", "at": "2026-10-01T10:00:00Z"}\n')
            outcome = archive.verify()
            self.assertFalse(outcome["ok"])
            self.assertEqual(outcome["packs"][0]["detail"], "checksum mismatch")

    def test_archive_query_filters_by_event_and_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, _, archive, _ = make_stack(tmp)
            archive.seal(
                "2026-10-01",
                [
                    Record(id="a1", event="access", at="2026-10-01T08:00:00Z"),
                    Record(id="f1", event="fire", at="2026-10-01T09:00:00Z"),
                ],
            )
            archive.seal("2026-10-02", [Record(id="a2", event="access", at="2026-10-02T08:00:00Z")])

            self.assertEqual(
                [record.id for record in archive.query(event="access")], ["a1", "a2"]
            )
            self.assertEqual(
                [record.id for record in archive.query(start="2026-10-02T00:00:00Z")], ["a2"]
            )
            self.assertEqual(
                [record.id for record in archive.query(end="2026-10-01T08:30:00Z")], ["a1"]
            )
            self.assertEqual(archive.query(event="vent"), [])

    def test_views_merge_online_and_archive_by_identifier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, registry, archive, retention = make_stack(tmp)
            registry.append(Record(id="old-1", event="access", at="2026-10-01T08:00:00Z"))
            registry.append(Record(id="keep-1", event="fire", at="2026-10-06T08:00:00Z"))
            retention.run()

            self.assertEqual(
                [record.id for record in source_records(registry, archive, "online")], ["keep-1"]
            )
            self.assertEqual(
                [record.id for record in source_records(registry, archive, "archive")], ["old-1"]
            )
            self.assertEqual(
                [record.id for record in source_records(registry, archive, "all")],
                ["old-1", "keep-1"],
            )
            with self.assertRaises(ValueError):
                source_records(registry, archive, "sideways")


class AuditHttpCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/data")
        self.server = Server(self.store, web_dir="web", audit_online_days=3)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def call(self, method: str, path: str, payload: dict | None = None, query: dict | None = None):
        response = self.server.dispatch(method, path, payload, query)
        body = response.body.decode("utf-8")
        parsed = json.loads(body) if response.content_type.startswith("application/json") else body
        return response.status, parsed

    def test_records_survive_a_restart(self) -> None:
        self.call("POST", "/api/audit/append", {"event": "manual"})
        status, before = self.call("GET", "/api/audit")
        self.assertEqual(before["count"], 1)

        restarted = Server(self.store, web_dir="web", audit_online_days=3)
        response = restarted.dispatch("GET", "/api/audit")
        after = json.loads(response.body.decode("utf-8"))
        self.assertEqual(after["count"], 1)
        self.assertEqual(after["items"][0]["event"], "manual")

    def test_retention_archive_and_export_endpoints(self) -> None:
        self.server.runtime.audit.append(
            Record(id="old-1", event="manual", at="2020-01-01T00:00:00Z")
        )
        self.server.runtime.audit.append(
            Record(id="keep-1", event="manual", at="2099-01-01T00:00:00Z")
        )

        status, retention = self.call("GET", "/api/audit/retention")
        self.assertEqual(status, 200)
        self.assertEqual(retention["policy"], {"online_days": 3})
        self.assertEqual(retention["online"]["expired"], 1)
        self.assertEqual(retention["pending_days"], ["2020-01-01"])

        status, report = self.call("POST", "/api/audit/retention/run")
        self.assertEqual(status, 200)
        self.assertTrue(report["ok"])
        self.assertEqual(report["deleted"], 1)
        self.assertEqual([pack["day"] for pack in report["archived"]], ["2020-01-01"])

        status, audit = self.call("GET", "/api/audit")
        self.assertEqual([item["id"] for item in audit["items"]], ["keep-1"])

        status, archive = self.call("GET", "/api/audit/archive")
        self.assertEqual(archive["count"], 1)
        self.assertEqual(archive["records"], 1)
        self.assertEqual(archive["packs"][0]["events"], {"manual": 1})

        status, items = self.call("GET", "/api/audit/archive/query", query={"event": ["manual"]})
        self.assertEqual([item["id"] for item in items["items"]], ["old-1"])
        status, items = self.call(
            "GET", "/api/audit/archive/query", query={"start": ["2021-01-01T00:00:00Z"]}
        )
        self.assertEqual(items["count"], 0)

        status, verify = self.call("GET", "/api/audit/archive/verify")
        self.assertTrue(verify["ok"])

        # The export references the same records across online and archive.
        status, csv_all = self.call("GET", "/api/export/audit", query={"source": ["all"]})
        self.assertIn("old-1", csv_all)
        self.assertIn("keep-1", csv_all)
        status, csv_online = self.call("GET", "/api/export/audit")
        self.assertNotIn("old-1", csv_online)
        self.assertIn("keep-1", csv_online)
        status, _ = self.call("GET", "/api/export/audit", query={"source": ["sideways"]})
        self.assertEqual(status, 400)

        status, cleared = self.call("POST", "/api/audit/clear")
        self.assertEqual(cleared, {"cleared": 0, "count": 1})


if __name__ == "__main__":
    unittest.main()
