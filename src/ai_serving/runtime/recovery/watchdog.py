"""Detect stale ownership, stalled progress and repeated actions."""

import time

from ai_serving.state.sqlite.store import StateStore


class RepeatedAction(ValueError):
    pass


class Watchdog:
    def __init__(self, store: StateStore, *, progress_timeout: float = 30.0, repeat_limit: int = 3):
        self.store = store
        self.progress_timeout = progress_timeout
        self.repeat_limit = repeat_limit
        self._last_action = None
        self._repeats = 0

    def stalled(self, task_id: str) -> bool:
        return time.time() - self.store.task(task_id)["last_progress"] > self.progress_timeout

    def observe_action(self, signature: str) -> None:
        self._repeats = self._repeats + 1 if signature == self._last_action else 1
        self._last_action = signature
        if self._repeats >= self.repeat_limit:
            raise RepeatedAction("Worker repeated the same action without a new plan")

    def expired_tasks(self) -> list[str]:
        now = time.time()
        return [task["task_id"] for task in self.store.tasks(status="running") if task["lease_until"] <= now]
