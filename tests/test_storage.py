from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from receipt_app.storage import Storage


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "nested" / "receipts.sqlite3"
        self.store = Storage(self.path)

    def assert_minimal_schema(self, path):
        with closing(sqlite3.connect(path)) as db:
            columns = db.execute("PRAGMA table_info(receipts)").fetchall()
            self.assertEqual([(row[1], row[2], row[3], row[5]) for row in columns],
                             [("number", "INTEGER", 0, 1), ("created_at", "TEXT", 1, 0)])
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            self.assertEqual({row[0] for row in tables}, {"receipts", "sqlite_sequence"})

    def create_legacy(self, path, *, with_uncertainty=True, nullable_timestamp=False):
        timestamp_constraint = "" if nullable_timestamp else "NOT NULL"
        uncertainty_column = ", outcome_uncertain INTEGER NOT NULL DEFAULT 0" if with_uncertainty else ""
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(f"""CREATE TABLE receipts (
                number INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT {timestamp_constraint},
                with_photo INTEGER NOT NULL, status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT ''
                {uncertainty_column})""")
            db.execute("""INSERT INTO receipts(number, created_at, with_photo, status, attempts, error)
                VALUES (7, ?, 1, 'error', 2, ?)""",
                       (None if nullable_timestamp else "2026-11-07T12:34:56.123+09:00",
                        "legacy-private-error-29b30a64"))
            db.execute("""INSERT INTO receipts(number, created_at, with_photo, status)
                VALUES (41, 'deleted timestamp', 0, 'completed')""")
            db.execute("DELETE FROM receipts WHERE number=41")

    def test_schema_and_returned_ticket_contain_only_number_and_datetime(self):
        ticket = self.store.allocate("2026-11-07T12:00:00.123+09:00")
        self.assert_minimal_schema(self.path)
        self.assertEqual(asdict(ticket),
                         {"number": 1, "created_at": "2026-11-07T12:00:00.123+09:00"})
        self.assertEqual(asdict(Storage(self.path).get(1)), asdict(ticket))

    def test_sequence_persists_across_new_storage_instances(self):
        first = self.store.allocate("2026-11-07T12:00:00.123+09:00")
        restarted = Storage(self.path)
        second = restarted.allocate("2026-11-07T12:01:00.456+09:00")
        self.assertEqual((first.number, second.number), (1, 2))
        self.assertEqual(restarted.get(1), first)
        self.assertEqual(restarted.latest(), second)

    def test_concurrent_allocation_uses_one_shared_sequence(self):
        def allocate(index):
            return Storage(self.path).allocate(f"timestamp-{index}")

        with ThreadPoolExecutor(max_workers=8) as pool:
            tickets = list(pool.map(allocate, range(32)))
        self.assertEqual(sorted(ticket.number for ticket in tickets), list(range(1, 33)))
        self.assertEqual({ticket.created_at for ticket in tickets},
                         {f"timestamp-{index}" for index in range(32)})
        self.assertEqual(self.store.latest().number, 32)

    def test_empty_database_and_unknown_number(self):
        self.assertIsNone(self.store.latest())
        self.assertIsNone(self.store.get(99))

    def test_migrates_both_legacy_schemas_preserving_rows_and_deleted_high_water(self):
        for with_uncertainty in (False, True):
            with self.subTest(with_uncertainty=with_uncertainty):
                path = self.path.parent / f"legacy-{with_uncertainty}.sqlite3"
                self.create_legacy(path, with_uncertainty=with_uncertainty)
                migrated = Storage(path)
                self.assert_minimal_schema(path)
                self.assertEqual(asdict(migrated.get(7)),
                                 {"number": 7, "created_at": "2026-11-07T12:34:56.123+09:00"})
                self.assertIsNone(migrated.get(41))
                self.assertEqual(migrated.allocate("new timestamp").number, 42)
                reopened = Storage(path)
                self.assert_minimal_schema(path)
                self.assertEqual(reopened.get(7), migrated.get(7))
                self.assertEqual(reopened.allocate("another timestamp").number, 43)

    def test_migration_preserves_high_water_when_all_old_rows_were_deleted(self):
        path = self.path.parent / "empty-legacy.sqlite3"
        self.create_legacy(path)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("DELETE FROM receipts")
        migrated = Storage(path)
        self.assert_minimal_schema(path)
        self.assertIsNone(migrated.latest())
        self.assertEqual(migrated.allocate("new timestamp").number, 42)

    def test_migration_removes_legacy_values_without_leaving_backup_files(self):
        directory = self.path.parent / "privacy-check"
        directory.mkdir()
        path = directory / "legacy.sqlite3"
        self.create_legacy(path)
        marker = b"legacy-private-error-29b30a64"
        self.assertIn(marker, path.read_bytes())
        Storage(path)
        self.assert_minimal_schema(path)
        self.assertEqual({entry.name for entry in directory.iterdir()}, {path.name})
        self.assertNotIn(marker, path.read_bytes())

    def test_migration_clears_legacy_values_from_live_wal_files(self):
        path = self.path.parent / "wal-legacy.sqlite3"
        self.create_legacy(path)
        with closing(sqlite3.connect(path)) as other:
            self.assertEqual(other.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            with other:
                other.execute("UPDATE receipts SET error='legacy-private-error-in-wal-c4eef7cb'")
            Storage(path)
            self.assert_minimal_schema(path)
            for suffix in ("", "-wal", "-shm"):
                current = Path(str(path) + suffix)
                if current.exists():
                    self.assertNotIn(b"legacy-private-error-in-wal-c4eef7cb", current.read_bytes())

    def test_cleanup_is_retried_after_vacuum_failure(self):
        path = self.path.parent / "cleanup-retry.sqlite3"
        self.create_legacy(path)
        connect = sqlite3.connect

        class FailingVacuumConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql == "VACUUM":
                    raise sqlite3.OperationalError("simulated vacuum failure")
                return super().execute(sql, *args, **kwargs)

        with patch("receipt_app.storage.sqlite3.connect",
                   side_effect=lambda *args, **kwargs: connect(*args, **kwargs, factory=FailingVacuumConnection)):
            with self.assertRaisesRegex(sqlite3.OperationalError, "simulated vacuum failure"):
                Storage(path)
        self.assert_minimal_schema(path)
        with closing(connect(path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
        reopened = Storage(path)
        self.assertEqual(reopened.get(7).created_at, "2026-11-07T12:34:56.123+09:00")
        self.assertEqual(reopened.allocate("after cleanup").number, 42)
        with closing(connect(path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_failed_migration_rolls_back_without_losing_legacy_rows(self):
        path = self.path.parent / "invalid-legacy.sqlite3"
        self.create_legacy(path, nullable_timestamp=True)
        with self.assertRaises(sqlite3.IntegrityError):
            Storage(path)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("SELECT number, created_at, error FROM receipts").fetchall(),
                             [(7, None, "legacy-private-error-29b30a64")])
            self.assertEqual(db.execute("SELECT seq FROM sqlite_sequence WHERE name='receipts'").fetchone(),
                             (41,))
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            self.assertEqual({row[0] for row in tables}, {"receipts", "sqlite_sequence"})


if __name__ == "__main__":
    unittest.main()
