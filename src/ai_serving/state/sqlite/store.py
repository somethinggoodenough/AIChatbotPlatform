"""Small synchronous transactions; no network or model work inside a transaction."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sqlite3
import time
import uuid

from ai_serving.contracts.messages import Message, WorkerReport


class LeaseLost(RuntimeError):
    """A stale worker must not commit results or effects."""


@dataclass(frozen=True)
class Lease:
    task_id: str
    owner: str
    token: int
    revision: int
    start_step: int


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
            CREATE TABLE IF NOT EXISTS inbox (
                message_id TEXT PRIMARY KEY REFERENCES messages(message_id),
                status TEXT NOT NULL DEFAULT 'pending', decision TEXT, task_id TEXT, decision_input TEXT,
                batch_representative TEXT,
                lease_owner TEXT, lease_until REAL NOT NULL DEFAULT 0,
                fencing_token INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS rounds (
                task_id TEXT NOT NULL REFERENCES tasks(task_id), revision INTEGER NOT NULL,
                start_step INTEGER NOT NULL, status TEXT NOT NULL,
                PRIMARY KEY(task_id, revision)
            );
            CREATE TABLE IF NOT EXISTS steps (
                task_id TEXT NOT NULL REFERENCES tasks(task_id), step_index INTEGER NOT NULL,
                kind TEXT NOT NULL, backend TEXT NOT NULL, input TEXT NOT NULL,
                output TEXT, status TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                compensation TEXT, compensated INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(task_id, step_index)
            );
            CREATE TABLE IF NOT EXISTS tool_receipts (
                idempotency_key TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                fencing_token INTEGER NOT NULL, input TEXT NOT NULL, output TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS effects (
                name TEXT PRIMARY KEY, value REAL NOT NULL
            );
        """)
        # The Phase 1 database is upgraded in place; no task history is discarded.
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(tasks)")}
        for name, definition in {
            "revision": "INTEGER NOT NULL DEFAULT 0",
            "completed_revision": "INTEGER NOT NULL DEFAULT 0",
            "last_progress": "REAL NOT NULL DEFAULT 0",
            "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in columns:
                self.connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
        self.connection.execute(
            "UPDATE tasks SET revision = (SELECT count(*) FROM task_messages WHERE task_id = tasks.task_id) "
            "WHERE revision = 0"
        )
        if "completed_revision" not in columns:
            self.connection.execute("UPDATE tasks SET completed_revision = revision WHERE status = 'completed'")

    def close(self) -> None:
        self.connection.close()

    def save_message(self, message: Message) -> bool:
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?)",
            (message.message_id, message.session_id, json.dumps(asdict(message)), time.time()),
        )
        return cursor.rowcount == 1

    @contextmanager
    def transaction(self):
        if self.connection.in_transaction:
            yield
            return
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def enqueue(self, message: Message) -> bool:
        with self.transaction():
            self.save_message(message)
            cursor = self.connection.execute("INSERT OR IGNORE INTO inbox(message_id) VALUES (?)", (message.message_id,))
            return cursor.rowcount == 1

    def inbox(self, message_id: str) -> dict:
        return dict(self.connection.execute("SELECT * FROM inbox WHERE message_id = ?", (message_id,)).fetchone())

    def inbox_claim(self, message_id: str, owner: str, ttl: float) -> int | None:
        now = time.time()
        with self.transaction():
            row = self.inbox(message_id)
            if row["status"] in {"done", "uncertain", "cancelled"} or row["lease_until"] > now or row["batch_representative"]:
                return None
            token = row["fencing_token"] + 1
            self.connection.execute(
                "UPDATE inbox SET lease_owner = ?, lease_until = ?, fencing_token = ? WHERE message_id = ?",
                (owner, now + ttl, token, message_id),
            )
            return token

    def inbox_update(self, message_id: str, owner: str, token: int, **values) -> None:
        allowed = {"status", "decision", "task_id", "lease_until", "decision_input"}
        if values.keys() - allowed:
            raise ValueError("unknown inbox field")
        assignments = ", ".join(f"{name} = ?" for name in values)
        cursor = self.connection.execute(
            f"UPDATE inbox SET {assignments} WHERE message_id = ? AND lease_owner = ? AND fencing_token = ? AND lease_until > ?",
            (*values.values(), message_id, owner, token, time.time()),
        )
        if cursor.rowcount != 1:
            raise LeaseLost("inbox claim replaced")

    def pending_messages(self) -> list[Message]:
        rows = self.connection.execute(
            "SELECT payload FROM inbox JOIN messages USING(message_id) "
            "WHERE status NOT IN ('done', 'cancelled', 'uncertain') AND batch_representative IS NULL ORDER BY created"
        ).fetchall()
        return [Message(**json.loads(row["payload"])) for row in rows]

    def group_inbox(self, messages: list[Message]) -> None:
        representative = messages[-1].message_id
        with self.transaction():
            for message in messages[:-1]:
                self.connection.execute("UPDATE inbox SET batch_representative = ? WHERE message_id = ? AND status = 'pending'",
                                        (representative, message.message_id))

    def acknowledge_inbox(self, message_id: str, owner: str, token: int) -> None:
        with self.transaction():
            self.inbox_update(message_id, owner, token, status="done")
            self.connection.execute("UPDATE inbox SET status = 'done' WHERE batch_representative = ?", (message_id,))

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

    def reply(self, message_id: str) -> str | None:
        row = self.connection.execute("SELECT text FROM replies WHERE message_id = ?", (message_id,)).fetchone()
        return row["text"] if row else None

    def create_task(self, message: Message, spec: str, acceptance: tuple[str, ...]) -> str:
        # Stable per-message id makes replay of a saved spawn decision idempotent.
        task_id = uuid.uuid5(uuid.NAMESPACE_URL, "ai-serving:" + message.message_id).hex
        with self.transaction():
            self.connection.execute(
                "INSERT OR IGNORE INTO tasks (task_id, session_id, user_id, spec, acceptance, status, created) "
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
        with self.transaction():
            self.save_message(message)
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO task_messages VALUES (?, ?)", (task_id, message.message_id),
            )
            if cursor.rowcount:
                self.connection.execute(
                    "UPDATE tasks SET revision = revision + 1, status = CASE WHEN status = 'completed' "
                    "THEN 'pending' ELSE status END WHERE task_id = ?", (task_id,),
                )

    def task_context(self, task_id: str, revision: int | None = None) -> list[dict]:
        rows = self.connection.execute(
            "SELECT payload FROM task_messages JOIN messages USING(message_id) "
            "WHERE task_id = ? ORDER BY task_messages.rowid LIMIT ?", (task_id, revision or -1),
        ).fetchall()
        return [{"role": "user", "content": json.loads(row["payload"])["text"]} for row in rows]

    def claim(self, task_id: str, owner: str, ttl: float) -> Lease | None:
        now = time.time()
        with self.transaction():
            task = self.task(task_id)
            if task["cancel_requested"] or task["status"] in {"uncertain", "failed"} or task["lease_until"] > now:
                return None
            if task["completed_revision"] >= task["revision"]:
                return None
            pending = self.connection.execute(
                "SELECT * FROM rounds WHERE task_id = ? AND status = 'running'", (task_id,),
            ).fetchone()
            if pending:
                revision, start_step = pending["revision"], pending["start_step"]
            else:
                revision = task["revision"]
                start_step = self.connection.execute(
                    "SELECT coalesce(max(step_index), -1) + 1 FROM steps WHERE task_id = ?", (task_id,),
                ).fetchone()[0]
                self.connection.execute("INSERT INTO rounds VALUES (?, ?, ?, 'running')", (task_id, revision, start_step))
            token = task["fencing_token"] + 1
            self.connection.execute(
                "UPDATE tasks SET lease_owner = ?, lease_until = ?, fencing_token = ?, heartbeat = ?, "
                "last_progress = ?, status = 'running' WHERE task_id = ?",
                (owner, now + ttl, token, now, now, task_id),
            )
            return Lease(task_id, owner, token, revision, start_step)

    def assert_lease(self, lease: Lease) -> None:
        task = self.task(lease.task_id)
        if (task["lease_owner"] != lease.owner or task["fencing_token"] != lease.token
                or task["lease_until"] <= time.time() or task["cancel_requested"]):
            raise LeaseLost("worker lease expired or was replaced")

    def heartbeat(self, lease: Lease, ttl: float) -> None:
        with self.transaction():
            self.assert_lease(lease)
            now = time.time()
            self.connection.execute("UPDATE tasks SET lease_until = ?, heartbeat = ? WHERE task_id = ?",
                                    (now + ttl, now, lease.task_id))

    def release(self, lease: Lease) -> None:
        self.connection.execute(
            "UPDATE tasks SET lease_owner = NULL, lease_until = 0 WHERE task_id = ? AND fencing_token = ? AND lease_owner = ?",
            (lease.task_id, lease.token, lease.owner),
        )

    def complete(self, lease: Lease, report: WorkerReport, *, status: str | None = None) -> None:
        with self.transaction():
            self.assert_lease(lease)
            status = status or ("completed" if report.accepted else "failed")
            self.connection.execute(
                "UPDATE tasks SET status = ?, report = ?, artifacts = ?, completed_revision = ? WHERE task_id = ?",
                (status, report.text, json.dumps(report.artifacts), lease.revision, lease.task_id),
            )
            self.connection.execute("UPDATE rounds SET status = ? WHERE task_id = ? AND revision = ?",
                                    (status, lease.task_id, lease.revision))

    def pause(self, lease: Lease, report: str) -> None:
        with self.transaction():
            self.assert_lease(lease)
            self.connection.execute("UPDATE tasks SET status = 'pending', report = ? WHERE task_id = ?", (report, lease.task_id))

    def stop(self, task_id: str, user_id: str) -> bool:
        with self.transaction():
            task = self.task(task_id)
            if task["user_id"] != user_id:
                return False
            self.connection.execute(
                "UPDATE tasks SET cancel_requested = 1, status = 'cancelled', fencing_token = fencing_token + 1, "
                "lease_owner = NULL, lease_until = 0, report = 'Task stopped.' WHERE task_id = ?", (task_id,),
            )
            return True

    def step(self, task_id: str, index: int) -> dict | None:
        row = self.connection.execute("SELECT * FROM steps WHERE task_id = ? AND step_index = ?", (task_id, index)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for key in ("input", "output", "compensation"):
            result[key] = json.loads(result[key]) if result[key] is not None else None
        return result

    def begin_step(self, lease: Lease, index: int, kind: str, backend: str, value, compensation=None) -> dict:
        with self.transaction():
            self.assert_lease(lease)
            self.connection.execute(
                "INSERT OR IGNORE INTO steps(task_id, step_index, kind, backend, input, status, idempotency_key, compensation) "
                "VALUES (?, ?, ?, ?, ?, 'started', ?, ?)",
                (lease.task_id, index, kind, backend, json.dumps(value, sort_keys=True), f"{lease.task_id}:{index}",
                 json.dumps(compensation) if compensation else None),
            )
            return self.step(lease.task_id, index)

    def finish_step(self, lease: Lease, index: int, output, *, status: str = "completed") -> None:
        with self.transaction():
            self.assert_lease(lease)
            self.connection.execute("UPDATE steps SET output = ?, status = ? WHERE task_id = ? AND step_index = ?",
                                    (json.dumps(output), status, lease.task_id, index))
            self.connection.execute("UPDATE tasks SET progress = progress + 1, last_progress = ? WHERE task_id = ?",
                                    (time.time(), lease.task_id))

    def effect(self, lease: Lease, key: str, name: str, delta: float) -> dict:
        """Toy effect sink: fencing, mutation and receipt share one transaction."""
        value = json.dumps({"name": name, "delta": delta}, sort_keys=True)
        with self.transaction():
            self.assert_lease(lease)
            receipt = self.connection.execute("SELECT input, output FROM tool_receipts WHERE idempotency_key = ?", (key,)).fetchone()
            if receipt:
                if receipt["input"] != value:
                    raise ValueError("idempotency key reused with different arguments")
                return json.loads(receipt["output"])
            self.connection.execute(
                "INSERT INTO effects VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET value = value + excluded.value", (name, delta),
            )
            output = {"name": name, "value": self.connection.execute("SELECT value FROM effects WHERE name = ?", (name,)).fetchone()[0]}
            self.connection.execute("INSERT INTO tool_receipts VALUES (?, ?, ?, ?, ?)",
                                    (key, lease.task_id, lease.token, value, json.dumps(output)))
            return output

    def compensations(self, task_id: str, start_step: int = 0) -> list[dict]:
        rows = self.connection.execute(
            "SELECT step_index FROM steps WHERE task_id = ? AND compensation IS NOT NULL "
            "AND compensated = 0 AND step_index >= ? ORDER BY step_index DESC", (task_id, start_step),
        ).fetchall()
        return [self.step(task_id, row["step_index"]) for row in rows]

    def mark_compensated(self, lease: Lease, index: int) -> None:
        with self.transaction():
            self.assert_lease(lease)
            self.connection.execute("UPDATE steps SET compensated = 1 WHERE task_id = ? AND step_index = ?", (lease.task_id, index))
