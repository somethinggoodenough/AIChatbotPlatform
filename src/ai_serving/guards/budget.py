"""SQLite admission reservations; unknown spend stays reserved for reconciliation."""

from decimal import Decimal
import sqlite3
import secrets


class BudgetExceeded(ValueError):
    pass


class BudgetStore:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS guard_sessions (id TEXT PRIMARY KEY, turns INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS guard_turns (session_id TEXT, turn_id TEXT, PRIMARY KEY(session_id,turn_id));
            CREATE TABLE IF NOT EXISTS guard_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS guard_blocked_users (user_id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS guard_spend (
                request_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, amount TEXT NOT NULL,
                status TEXT NOT NULL
            );
        """)

    def canary(self) -> str:
        self.db.execute("INSERT OR IGNORE INTO guard_settings VALUES ('canary', ?)",
                        ("CANARY_" + secrets.token_hex(12),))
        return self.db.execute("SELECT value FROM guard_settings WHERE key='canary'").fetchone()[0]

    def reserve(self, request_id: str, user_id: str, session_id: str, *,
                turn_limit: int, spend_limit: Decimal, amount: Decimal, turn_id: str = "") -> None:
        if amount < 0 or spend_limit < 0:
            raise ValueError("Budget amounts must be nonnegative")
        turn_id = turn_id or request_id
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.db.execute("SELECT 1 FROM guard_blocked_users WHERE user_id=?", (user_id,)).fetchone():
                raise BudgetExceeded("Cost bound was exceeded; reconciliation required")
            if self.db.execute("SELECT 1 FROM guard_spend WHERE request_id=?", (request_id,)).fetchone():
                raise BudgetExceeded("Request already admitted")
            row = self.db.execute("SELECT turns FROM guard_sessions WHERE id=?", (session_id,)).fetchone()
            turns = row[0] if row else 0
            new_turn = self.db.execute("SELECT 1 FROM guard_turns WHERE session_id=? AND turn_id=?",
                                       (session_id, turn_id)).fetchone() is None
            spent = sum((Decimal(row[0]) for row in self.db.execute(
                "SELECT amount FROM guard_spend WHERE user_id=?", (user_id,))), Decimal(0))
            if (new_turn and turns >= turn_limit) or spent + amount > spend_limit:
                raise BudgetExceeded("Session or spending limit reached")
            self.db.execute("INSERT INTO guard_sessions VALUES (?,?) ON CONFLICT(id) DO UPDATE SET turns=excluded.turns",
                            (session_id, turns + int(new_turn)))
            self.db.execute("INSERT OR IGNORE INTO guard_turns VALUES (?,?)", (session_id, turn_id))
            self.db.execute("INSERT INTO guard_spend VALUES (?,?,?,'reserved')", (request_id, user_id, str(amount)))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def settle(self, request_id: str, actual: Decimal | None) -> None:
        if actual is not None:
            row = self.db.execute("SELECT user_id, amount FROM guard_spend WHERE request_id=?", (request_id,)).fetchone()
            if row and actual > Decimal(row[1]):
                self.db.execute("INSERT OR IGNORE INTO guard_blocked_users VALUES (?)", (row[0],))
            self.db.execute("UPDATE guard_spend SET amount=?,status='settled' WHERE request_id=?",
                            (str(actual), request_id))

    def close(self):
        self.db.close()
