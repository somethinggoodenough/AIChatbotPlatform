"""Provider differences live here; runtime callers use canonical arguments."""

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .errors import ErrorClassifier
from .usage import PriceTable, UsageAccumulator, UsageNormalizer


class UnsupportedCapability(ValueError):
    pass


@dataclass(frozen=True)
class Capabilities:
    streaming: bool = False
    json_schema: bool = False
    strict_json_schema: bool = False
    reasoning: bool = False
    tools: bool = False
    temperature: bool = False


@dataclass(frozen=True)
class RequestMapping:
    max_tokens_field: str = "max_tokens"
    stream_usage: bool = False
    defaults: Mapping[str, Any] = field(default_factory=dict)
    reasoning_field: str = ""
    reasoning_values: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    model: str
    base_url: str
    protocol: str = "chat_completions"
    endpoint: str = "/chat/completions"
    api_key: str = field(default="", repr=False)
    auth_header: str = "Authorization"
    auth_prefix: str = "Bearer "
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    capabilities: Capabilities = field(default_factory=Capabilities)
    request: RequestMapping = field(default_factory=RequestMapping)
    usage_normalizer: UsageNormalizer = field(default_factory=UsageNormalizer)
    error_classifier: ErrorClassifier = field(default_factory=ErrorClassifier)
    prices: PriceTable = field(default_factory=PriceTable)
    usage_stream_mode: str = "cumulative"

    @property
    def url(self) -> str:
        return self.base_url.rstrip("/") + self.endpoint

    def request_headers(self) -> dict[str, str]:
        result = {"Content-Type": "application/json", **self.headers}
        if self.api_key:
            result[self.auth_header] = self.auth_prefix + self.api_key
        return result

    def usage_accumulator(self) -> UsageAccumulator:
        return UsageAccumulator(self.usage_stream_mode)

    def build_request(
        self, messages: Sequence[Mapping[str, Any]], *, stream: bool = False,
        max_output_tokens: int | None = None, json_schema: Mapping[str, Any] | None = None,
        schema_name: str = "response", strict_schema: bool = False,
        reasoning: str | None = None, tools: Sequence[Mapping[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        """Encode a chat-completions request; never silently drop requirements.

        This does not send HTTP or validate returned JSON against the schema.
        Messages/tools use the chat-completions wire shape in this first adapter.
        """
        if self.protocol != "chat_completions":
            raise UnsupportedCapability(f"No request adapter for {self.protocol}")
        required = {
            "streaming": stream, "json_schema": json_schema is not None,
            "strict_json_schema": strict_schema, "reasoning": reasoning is not None,
            "tools": tools is not None, "temperature": temperature is not None,
        }
        for name, needed in required.items():
            if needed and not getattr(self.capabilities, name):
                raise UnsupportedCapability(f"{self.name} does not support {name}")
        if strict_schema and json_schema is None:
            raise ValueError("strict_schema requires json_schema")
        body: dict[str, Any] = deepcopy(dict(self.request.defaults))
        body.update(model=self.model, messages=deepcopy(list(messages)), stream=stream)
        if max_output_tokens is not None:
            if type(max_output_tokens) is not int or max_output_tokens <= 0:
                raise ValueError("max_output_tokens must be a positive integer")
            body[self.request.max_tokens_field] = max_output_tokens
        if stream and self.request.stream_usage:
            body["stream_options"] = {"include_usage": True}
        if json_schema is not None:
            schema: dict[str, Any] = {"name": schema_name, "schema": deepcopy(dict(json_schema))}
            if strict_schema:
                schema["strict"] = True
            body["response_format"] = {"type": "json_schema", "json_schema": schema}
        if reasoning is not None:
            if reasoning not in self.request.reasoning_values:
                raise UnsupportedCapability(f"{self.name} does not support reasoning level {reasoning}")
            target = body
            parts = self.request.reasoning_field.split(".")
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = deepcopy(self.request.reasoning_values[reasoning])
        if tools is not None:
            body["tools"] = deepcopy(list(tools))
        if temperature is not None:
            if type(temperature) not in {int, float} or not 0 <= temperature <= 2:
                raise ValueError("temperature must be a number between 0 and 2")
            body["temperature"] = temperature
        return body
