from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from receipt_app.storage import Storage


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "nested" / "receipts.sqlite3"
        self.store = Storage(self.path)

    def test_sequence_persists_across_new_storage_instances(self):
        first = self.store.allocate(True, "2026-11-07T12:00:00+09:00")
        self.store.update(first.number, "completed", increment_attempt=True)
        restarted = Storage(self.path)
        second = restarted.allocate(False, "2026-11-07T12:01:00+09:00")
        self.assertEqual((first.number, second.number), (1, 2))
        self.assertEqual(restarted.get(1).created_at, first.created_at)
        self.assertTrue(restarted.get(1).with_photo)
        self.assertFalse(second.with_photo)
        self.assertEqual(restarted.latest().number, 2)

    def test_concurrent_allocation_uses_one_shared_sequence(self):
        def allocate(index):
            return Storage(self.path).allocate(bool(index % 2), f"timestamp-{index}")

        with ThreadPoolExecutor(max_workers=8) as pool:
            tickets = list(pool.map(allocate, range(32)))
        self.assertEqual(sorted(ticket.number for ticket in tickets), list(range(1, 33)))
        self.assertEqual(self.store.latest().number, 32)
        self.assertEqual(sum(ticket.with_photo for ticket in tickets), 16)

    def test_state_and_attempt_count_survive_reopen(self):
        ticket = self.store.allocate(False, "now")
        self.store.update(ticket.number, "printing", increment_attempt=True)
        self.store.update(ticket.number, "error", "paper out")
        self.store.update(ticket.number, "printing", increment_attempt=True)
        self.store.update(ticket.number, "completed")
        saved = Storage(self.path).get(ticket.number)
        self.assertEqual(saved.attempts, 2)
        self.assertEqual(saved.status, "completed")
        self.assertEqual(saved.error, "")

    def test_recover_only_in_progress_rows_and_is_idempotent(self):
        active = []
        for status in ("capturing", "rendering", "printing"):
            ticket = self.store.allocate(False, "now")
            self.store.update(ticket.number, status)
            active.append(ticket.number)
        completed = self.store.allocate(False, "now")
        self.store.update(completed.number, "completed")
        error = self.store.allocate(False, "now")
        self.store.update(error.number, "error", "failed")
        recovered = self.store.recover_interrupted()
        self.assertEqual(sorted(ticket.number for ticket in recovered), active)
        for number in active:
            self.assertEqual(self.store.get(number).status, "interrupted")
            self.assertIn("印刷済みの可能性", self.store.get(number).error)
        self.assertEqual(self.store.get(completed.number).status, "completed")
        self.assertEqual(self.store.get(error.number).status, "error")
        self.assertEqual(self.store.recover_interrupted(), [])

    def test_empty_database_and_unknown_number(self):
        self.assertIsNone(self.store.latest())
        self.assertIsNone(self.store.get(99))
        with self.assertRaisesRegex(ValueError, "受付番号 99"):
            self.store.update(99, "completed")

    def test_uncertainty_is_sticky_across_retries_and_reopen(self):
        ticket = self.store.allocate(False, "now")
        self.store.update(ticket.number, "error", "transfer interrupted", outcome_uncertain=True)
        self.store.update(ticket.number, "error", "connection refused", outcome_uncertain=False)
        self.assertTrue(Storage(self.path).get(ticket.number).outcome_uncertain)
        self.store.update(ticket.number, "completed")
        self.assertTrue(self.store.get(ticket.number).outcome_uncertain)
        self.assertFalse(self.store.allocate(False, "next").outcome_uncertain)

    def test_migrates_legacy_database_without_resetting_sequence(self):
        legacy_path = self.path.parent / "legacy.sqlite3"
        with closing(sqlite3.connect(legacy_path)) as db, db:
            db.execute("""CREATE TABLE receipts (
                number INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                with_photo INTEGER NOT NULL, status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '')""")
            db.execute("INSERT INTO receipts VALUES (7, 'old', 1, 'error', 2, '受信・印刷結果は不明です。')")
        migrated = Storage(legacy_path)
        self.assertTrue(migrated.get(7).outcome_uncertain)
        self.assertEqual(migrated.get(7).attempts, 2)
        self.assertEqual(migrated.allocate(False, "new").number, 8)
        self.assertTrue(Storage(legacy_path).get(7).outcome_uncertain)

    def test_recovery_marks_in_progress_printing_as_uncertain(self):
        ticket = self.store.allocate(False, "now")
        self.store.update(ticket.number, "printing")
        self.store.recover_interrupted()
        self.assertTrue(self.store.get(ticket.number).outcome_uncertain)


if __name__ == "__main__":
    unittest.main()
