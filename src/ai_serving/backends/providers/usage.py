"""Normalize provider usage without turning missing billing data into zero."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping


def field_at(data: Mapping[str, Any], path: str) -> Any:
    value: Any = data
    for part in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


@dataclass(frozen=True)
class TokenUsage:
    # Input includes cache reads/writes. Reasoning is a subset of output.
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None

    def __post_init__(self) -> None:
        for value in vars(self).values():
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("Token counts must be non-negative integers or unknown")
        cached = (self.cache_read_tokens or 0) + (self.cache_write_tokens or 0)
        if self.input_tokens is not None and cached > self.input_tokens:
            raise ValueError("Cache tokens exceed total input tokens")
        if (self.reasoning_tokens is not None and self.output_tokens is not None
                and self.reasoning_tokens > self.output_tokens):
            raise ValueError("Reasoning tokens exceed output tokens")


@dataclass(frozen=True)
class UsageNormalizer:
    """Paths are relative to a usage object, NOT the entire response/SSE event.

    A missing configured field is unknown. An empty cache path explicitly means
    that bucket is inapplicable. Normalize only after assembling stream usage.
    """

    input_tokens: str = "prompt_tokens"
    output_tokens: str = "completion_tokens"
    cache_read_tokens: str = "prompt_tokens_details.cached_tokens"
    cache_write_tokens: str = ""
    reasoning_tokens: str = "completion_tokens_details.reasoning_tokens"
    input_includes_cache: bool = True

    def normalize(self, usage: Mapping[str, Any] | None) -> TokenUsage:
        if usage is None:
            return TokenUsage()
        values: dict[str, int | None] = {}
        for name in TokenUsage.__dataclass_fields__:
            path = getattr(self, name)
            value = field_at(usage, path) if path else 0
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"Invalid provider usage field: {name}")
            values[name] = value
        if not self.input_includes_cache:
            parts = [values[k] for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens")]
            values["input_tokens"] = None if None in parts else sum(parts)
        return TokenUsage(**values)


class UsageAccumulator:
    """Per-response accumulator; a transport extracts only usage objects.

    cumulative: merge partial snapshots, replace counts, never sum snapshots.
    delta: sum explicit increments. Transport must not replay delta events.
    """

    def __init__(self, mode: str = "cumulative") -> None:
        if mode not in {"cumulative", "delta"}:
            raise ValueError("Unknown usage stream mode")
        self.mode = mode
        self._data: dict[str, Any] = {}
        self._seen = False

    def update(self, usage: Mapping[str, Any] | None) -> None:
        if usage is None:
            return
        self._seen = True

        def merge(target: dict[str, Any], patch: Mapping[str, Any]) -> None:
            for key, value in patch.items():
                if value is None:
                    continue
                if isinstance(value, Mapping):
                    if not isinstance(target.get(key), dict):
                        target[key] = {}
                    merge(target[key], value)
                elif self.mode == "delta" and type(value) is int:
                    target[key] = target.get(key, 0) + value
                else:
                    target[key] = value

        merge(self._data, usage)

    def normalized(self, normalizer: UsageNormalizer) -> TokenUsage:
        return normalizer.normalize(self._data if self._seen else None)


@dataclass(frozen=True)
class PriceTable:
    """Rates per million tokens for ONE model/version/tier, using Decimal."""

    currency: str = "USD"
    input_per_million: Decimal | None = None
    output_per_million: Decimal | None = None
    cache_read_per_million: Decimal | None = None
    cache_write_per_million: Decimal | None = None
    effective_date: str = ""
    source: str = ""

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if name.endswith("_per_million") and value is not None:
                if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                    raise ValueError("Prices must be finite non-negative Decimals")

    def estimate(self, usage: TokenUsage) -> Decimal | None:
        if usage.input_tokens is None or usage.output_tokens is None:
            return None
        # Unknown cache splits can still be priced if all input rates are equal.
        if usage.cache_read_tokens is None or usage.cache_write_tokens is None:
            if not (self.input_per_million is not None
                    and self.input_per_million == self.cache_read_per_million
                    == self.cache_write_per_million):
                return None
            reads = writes = 0
        else:
            reads, writes = usage.cache_read_tokens, usage.cache_write_tokens
        buckets = (
            (usage.input_tokens - reads - writes, self.input_per_million),
            (reads, self.cache_read_per_million),
            (writes, self.cache_write_per_million),
            (usage.output_tokens, self.output_per_million),
        )
        total = Decimal(0)
        for count, rate in buckets:
            if count and rate is None:
                return None
            if rate is not None:
                total += count * rate
        return total / Decimal(1_000_000)
