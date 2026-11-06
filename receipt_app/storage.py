from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Ticket:
    number: int
    created_at: str
    with_photo: bool
    status: str
    attempts: int
    error: str
    outcome_uncertain: bool = False


class Storage:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS receipts (
                number INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                with_photo INTEGER NOT NULL CHECK(with_photo IN (0,1)),
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                outcome_uncertain INTEGER NOT NULL DEFAULT 0 CHECK(outcome_uncertain IN (0,1))
            )""")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(receipts)")}
            if "outcome_uncertain" not in columns:
                db.execute("ALTER TABLE receipts ADD COLUMN outcome_uncertain INTEGER NOT NULL DEFAULT 0")
                db.execute("""UPDATE receipts SET outcome_uncertain=1
                    WHERE status IN ('printing','interrupted') OR error LIKE '%受信・印刷結果は不明%'""")

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
        return Ticket(row["number"], row["created_at"], bool(row["with_photo"]),
                      row["status"], row["attempts"], row["error"], bool(row["outcome_uncertain"]))

    def allocate(self, with_photo: bool, created_at: str) -> Ticket:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                "INSERT INTO receipts(created_at, with_photo, status) VALUES (?, ?, 'rendering')",
                (created_at, int(with_photo)),
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

    def update(self, number: int, status: str, error: str = "", increment_attempt: bool = False,
               outcome_uncertain: bool = False):
        with self._connect() as db:
            result = db.execute(
                """UPDATE receipts SET status=?, error=?, attempts=attempts+?,
                    outcome_uncertain=MAX(outcome_uncertain, ?) WHERE number=?""",
                (status, error, int(increment_attempt), int(outcome_uncertain), number),
            )
            if result.rowcount != 1:
                raise ValueError(f"受付番号 {number} がありません")

    def recover_interrupted(self) -> list[Ticket]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM receipts WHERE status IN ('capturing','rendering','printing')"
            ).fetchall()
            db.execute("""UPDATE receipts SET status='interrupted',
                outcome_uncertain=CASE WHEN status='printing' THEN 1 ELSE outcome_uncertain END,
                error='前回処理が中断しました。印刷済みの可能性があります。画像は復元できません。'
                WHERE status IN ('capturing','rendering','printing')""")
            return [self._ticket(row) for row in rows]
