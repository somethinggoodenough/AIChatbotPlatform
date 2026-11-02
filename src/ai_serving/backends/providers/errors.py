"""Error normalization is separate from the gateway's retry policy."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
from typing import Any, Mapping

from .usage import field_at


KINDS = frozenset({
    "authentication", "permission", "quota", "rate_limit", "invalid_request",
    "context_length", "unsupported", "not_found", "transient", "timeout",
    "connection", "cancelled", "unknown",
})
TRANSIENT = frozenset({"rate_limit", "transient", "timeout", "connection"})


@dataclass(frozen=True)
class ClassifiedError:
    kind: str
    retry_candidate: bool
    retry_after_seconds: float | None = None


@dataclass(frozen=True)
class ErrorClassifier:
    code_paths: tuple[str, ...] = ("error.code", "error.type")
    codes: Mapping[str, str] = field(default_factory=dict)
    statuses: Mapping[str, str] = field(default_factory=lambda: {
        "400": "invalid_request", "401": "authentication", "402": "quota",
        "403": "permission", "404": "not_found", "408": "timeout",
        "413": "invalid_request", "422": "invalid_request", "429": "rate_limit",
    })

    def classify(
        self, *, status: int | None = None, body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None, transport_error: str | None = None,
        now: datetime | None = None,
    ) -> ClassifiedError:
        if transport_error not in {None, "timeout", "connection", "cancelled"}:
            raise ValueError("Unknown transport error")
        kind = transport_error
        if kind is None:
            # A provider code (e.g. insufficient_quota in a 429) wins over status.
            for path in self.code_paths:
                code = field_at(body or {}, path)
                if isinstance(code, str) and code in self.codes:
                    kind = self.codes[code]
                    break
        if kind is None:
            kind = self.statuses.get(str(status))
        if kind is None:
            kind = "transient" if status is not None and 500 <= status < 600 else "unknown"
        retry_after = None
        raw = next((v for k, v in (headers or {}).items() if k.lower() == "retry-after"), None)
        if raw is not None:
            try:
                delay = float(raw)
            except (ValueError, TypeError):
                try:
                    when = parsedate_to_datetime(raw)
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                    delay = (when - (now or datetime.now(timezone.utc))).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    delay = float("nan")
            if math.isfinite(delay):
                retry_after = max(0.0, delay)
        return ClassifiedError(kind, kind in TRANSIENT, retry_after)
