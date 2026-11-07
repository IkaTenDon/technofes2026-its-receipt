from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Ticket:
    number: int
    created_at: str


class Storage:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA secure_delete = ON")
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS receipts (
                number INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL
            )""")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(receipts)")}
            migrated = columns != {"number", "created_at"}
            if migrated:
                self._migrate(db)
                # VACUUM runs after commit; retry unfinished cleanup on the next startup.
                db.execute("PRAGMA user_version = 1")
            cleanup_pending = db.execute("PRAGMA user_version").fetchone()[0] == 1
        if cleanup_pending:
            with self._connect() as db:
                db.execute("VACUUM")
                checkpoint = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint[0]:
                    raise sqlite3.OperationalError("旧データの整理が完了しませんでした。DBを使用中の他のアプリを終了してください")
                db.execute("PRAGMA user_version = 2")

    @staticmethod
    def _migrate(db):
        sequence = db.execute("SELECT seq FROM sqlite_sequence WHERE name='receipts'").fetchone()
        highest = db.execute("SELECT COALESCE(MAX(number), 0) FROM receipts").fetchone()[0]
        highest = max(highest, sequence[0] if sequence else 0)
        db.execute("""CREATE TABLE receipts_minimal (
            number INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL)""")
        db.execute("INSERT INTO receipts_minimal SELECT number, created_at FROM receipts")
        db.execute("DROP TABLE receipts")
        db.execute("ALTER TABLE receipts_minimal RENAME TO receipts")
        db.execute("DELETE FROM sqlite_sequence WHERE name='receipts'")
        db.execute("INSERT INTO sqlite_sequence(name, seq) VALUES ('receipts', ?)", (highest,))

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous = FULL")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _ticket(row) -> Ticket | None:
        if row is None:
            return None
        return Ticket(row["number"], row["created_at"])

    def allocate(self, created_at: str) -> Ticket:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                "INSERT INTO receipts(created_at) VALUES (?)",
                (created_at,),
            )
            number = cursor.lastrowid
            row = db.execute("SELECT * FROM receipts WHERE number=?", (number,)).fetchone()
        return self._ticket(row)

    def get(self, number: int) -> Ticket | None:
        with self._connect() as db:
            return self._ticket(db.execute("SELECT * FROM receipts WHERE number=?", (number,)).fetchone())

    def latest(self) -> Ticket | None:
        with self._connect() as db:
            return self._ticket(db.execute("SELECT * FROM receipts ORDER BY number DESC LIMIT 1").fetchone())
