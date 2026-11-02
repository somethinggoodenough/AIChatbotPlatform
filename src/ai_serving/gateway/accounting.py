"""Durable cost records contain identifiers and token counts, never content."""

from decimal import Decimal
import sqlite3

from ai_serving.backends.providers.usage import TokenUsage


class CostLedger:
    def __init__(self, path: str = ":memory:") -> None:
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS model_costs (
            call_id TEXT, request_id TEXT, attempt INTEGER, backend TEXT, user_id TEXT, task_id TEXT,
            status TEXT, currency TEXT, cost TEXT, prompt_tokens INTEGER,
            completion_tokens INTEGER, cached_tokens INTEGER,
            PRIMARY KEY(call_id, attempt))""")

    def record(self, call_id: str, attempt: int, backend, context,
               status: str, usage: dict | None) -> None:
        token_usage = TokenUsage() if usage is None else TokenUsage(
            input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
            cache_read_tokens=usage.get("cached_tokens"), cache_write_tokens=usage.get("cache_write_tokens"),
            reasoning_tokens=usage.get("reasoning_tokens"),
        )
        cost = backend.profile.prices.estimate(token_usage)
        with self.db:
            self.db.execute("INSERT INTO model_costs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                call_id, context.request_id or call_id, attempt, backend.name, context.user_id, context.task_id, status,
                backend.profile.prices.currency, str(cost) if cost is not None else None,
                token_usage.input_tokens, token_usage.output_tokens, token_usage.cache_read_tokens,
            ))

    def totals(self, *, user_id: str | None = None, task_id: str | None = None,
               request_id: str | None = None) -> dict:
        clauses, values = [], []
        for name, value in (("user_id", user_id), ("task_id", task_id), ("request_id", request_id)):
            if value is not None:
                clauses.append(name + " = ?")
                values.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.db.execute("SELECT currency, cost FROM model_costs" + where, values).fetchall()
        totals: dict[str, Decimal] = {}
        unknown = 0
        for currency, cost in rows:
            if cost is None:
                unknown += 1
            else:
                totals[currency] = totals.get(currency, Decimal(0)) + Decimal(cost)
        return {"known_cost": {key: str(value) for key, value in totals.items()},
                "unknown_attempts": unknown, "attempts": len(rows)}

    def request_cost(self, request_id: str, currency: str = "USD") -> Decimal | None:
        result = self.totals(request_id=request_id)
        if not result["attempts"] or result["unknown_attempts"]:
            return None
        if set(result["known_cost"]) != {currency}:
            return None
        return Decimal(result["known_cost"][currency])

    def close(self) -> None:
        self.db.close()
