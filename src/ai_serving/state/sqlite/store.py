"""Small synchronous transactions; no network or model work inside a transaction."""

from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import time
import uuid

from ai_serving.contracts.messages import Message, WorkerReport


class StateStore:
    def __init__(self, path: str | Path = ":memory:"):
        self.connection = sqlite3.connect(str(path), isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                message_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                payload TEXT NOT NULL, created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS replies (
                message_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, text TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, user_id TEXT NOT NULL,
                spec TEXT NOT NULL, acceptance TEXT NOT NULL, status TEXT NOT NULL,
                report TEXT, artifacts TEXT NOT NULL DEFAULT '[]', created REAL NOT NULL,
                lease_owner TEXT, lease_until REAL NOT NULL DEFAULT 0,
                fencing_token INTEGER NOT NULL DEFAULT 0, heartbeat REAL NOT NULL DEFAULT 0,
                progress INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS subscriptions (
                channel_id TEXT NOT NULL, thread_id TEXT NOT NULL,
                task_id TEXT NOT NULL REFERENCES tasks(task_id),
                PRIMARY KEY (channel_id, thread_id)
            );
            CREATE TABLE IF NOT EXISTS task_messages (
                task_id TEXT NOT NULL REFERENCES tasks(task_id),
                message_id TEXT NOT NULL REFERENCES messages(message_id),
                PRIMARY KEY (task_id, message_id)
            );
        """)

    def close(self) -> None:
        self.connection.close()

    def save_message(self, message: Message) -> bool:
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?)",
            (message.message_id, message.session_id, json.dumps(asdict(message)), time.time()),
        )
        return cursor.rowcount == 1

    def session_context(self, session_id: str, limit: int = 20) -> list[dict]:
        rows = self.connection.execute(
            "SELECT payload, text FROM messages LEFT JOIN replies USING(message_id, session_id) "
            "WHERE messages.session_id = ? ORDER BY created DESC LIMIT ?", (session_id, limit),
        ).fetchall()
        result = []
        for row in reversed(rows):
            result.append({"role": "user", "content": json.loads(row["payload"])["text"]})
            if row["text"] is not None:
                result.append({"role": "assistant", "content": row["text"]})
        return result

    def save_reply(self, message: Message, text: str) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO replies VALUES (?, ?, ?)",
            (message.message_id, message.session_id, text),
        )

    def create_task(self, message: Message, spec: str, acceptance: tuple[str, ...]) -> str:
        task_id = uuid.uuid4().hex
        self.connection.execute(
            "INSERT INTO tasks (task_id, session_id, user_id, spec, acceptance, status, created) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (task_id, message.session_id, message.user_id, spec, json.dumps(acceptance), time.time()),
        )
        self.append_task_message(task_id, message)
        if message.thread_id:
            self.subscribe(message.channel_id, message.thread_id, task_id)
        return task_id

    def task(self, task_id: str) -> dict:
        row = self.connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        result = dict(row)
        result["acceptance"] = json.loads(result["acceptance"])
        result["artifacts"] = json.loads(result["artifacts"])
        return result

    def tasks(self, *, status: str | None = None) -> list[dict]:
        rows = self.connection.execute(
            "SELECT task_id FROM tasks WHERE (? IS NULL OR status = ?) ORDER BY created", (status, status),
        ).fetchall()
        return [self.task(row["task_id"]) for row in rows]

    def set_status(self, task_id: str, status: str) -> None:
        self.connection.execute("UPDATE tasks SET status = ? WHERE task_id = ?", (status, task_id))

    def save_report(self, report: WorkerReport) -> None:
        self.connection.execute(
            "UPDATE tasks SET status = ?, report = ?, artifacts = ? WHERE task_id = ?",
            ("completed" if report.accepted else "failed", report.text, json.dumps(report.artifacts), report.task_id),
        )

    def subscribe(self, channel_id: str, thread_id: str, task_id: str) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO subscriptions VALUES (?, ?, ?)", (channel_id, thread_id, task_id),
        )

    def subscription(self, channel_id: str, thread_id: str | None) -> str | None:
        row = self.connection.execute(
            "SELECT task_id FROM subscriptions WHERE channel_id = ? AND thread_id = ?",
            (channel_id, thread_id),
        ).fetchone()
        return row["task_id"] if row else None

    def append_task_message(self, task_id: str, message: Message) -> None:
        self.save_message(message)
        self.connection.execute(
            "INSERT OR IGNORE INTO task_messages VALUES (?, ?)", (task_id, message.message_id),
        )

    def task_context(self, task_id: str) -> list[dict]:
        rows = self.connection.execute(
            "SELECT payload FROM task_messages JOIN messages USING(message_id) "
            "WHERE task_id = ? ORDER BY created", (task_id,),
        ).fetchall()
        return [{"role": "user", "content": json.loads(row["payload"])["text"]} for row in rows]
