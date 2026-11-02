"""Load strict TOML provider profiles and a small, non-executing .env format."""

from dataclasses import fields
from datetime import date
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import shlex
import tomllib
from typing import Any, Mapping
from urllib.parse import urlsplit

from ai_serving.backends.providers.errors import ErrorClassifier, KINDS
from ai_serving.backends.providers.profile import Capabilities, ProviderProfile, RequestMapping
from ai_serving.backends.providers.usage import PriceTable, UsageNormalizer


class ProfileConfigError(ValueError):
    """Configuration errors report field names, never secret values."""


def read_environment(path: Path, overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """Process env (or explicit overrides) wins; never mutate os.environ.

    Supports KEY=value, quoted values, comments, and optional 'export'.
    No variable interpolation, shell evaluation, multiline values or escapes
    beyond shlex's quoted-string rules. An absent .env is fine for deployments.
    """
    env: dict[str, str] = {}
    if path.exists():
        for number, raw in enumerate(path.read_text().splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, sep, value = line.partition("=")
            key = key.strip()
            if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or key in env:
                raise ProfileConfigError(f"Invalid or duplicate .env key at line {number}")
            try:
                parts = shlex.split(value, comments=True, posix=True)
            except ValueError:
                raise ProfileConfigError(f"Invalid .env quoting at line {number}") from None
            if len(parts) > 1:
                raise ProfileConfigError(f"Quote whitespace in .env value at line {number}")
            env[key] = parts[0] if parts else ""
    env.update(os.environ if overrides is None else overrides)
    return env


def _table(value: Any, allowed: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProfileConfigError(f"{label} must be a table")
    if value.keys() - allowed:
        raise ProfileConfigError(f"Unknown fields in {label}")
    return dict(value)


def _text(value: Any, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not empty):
        raise ProfileConfigError(f"{label} must be a {'possibly empty ' if empty else 'nonempty '}string")
    return value


def _bool(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ProfileConfigError(f"{label} must be a boolean")
    return value


def load_profile(path: str | Path, env: Mapping[str, str] | None = None) -> ProviderProfile:
    environment = os.environ if env is None else env
    try:
        with Path(path).open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError:
        raise ProfileConfigError("Invalid provider TOML") from None
    _table(data, {"profile", "capabilities", "request", "usage", "errors", "pricing"}, "root")
    p = _table(data.get("profile", {}), {
        "name", "protocol", "endpoint", "base_url", "base_url_env", "model", "model_env",
        "api_key_env", "api_key_required", "auth_header", "auth_prefix", "headers",
    }, "profile")

    def resolve(name: str) -> str:
        if name in p and name + "_env" in p:
            raise ProfileConfigError(f"Choose {name} or {name}_env, not both")
        env_name = p.get(name + "_env")
        if env_name is not None:
            _text(env_name, name + "_env")
            return _text(environment.get(env_name), name)
        return _text(p.get(name), name)

    name = _text(p.get("name"), "profile.name")
    base_url, model = resolve("base_url"), resolve("model")
    try:
        url = urlsplit(base_url)
        valid_url = (url.scheme in {"http", "https"} and url.hostname and not url.username
                     and not url.password and not url.query and not url.fragment
                     and not any(c.isspace() for c in base_url))
        url.port  # Reject invalid ports during loading, before a request is built.
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ProfileConfigError("base_url must be an HTTP(S) URL without credentials, query or fragment")
    protocol = p.get("protocol", "chat_completions")
    if protocol != "chat_completions":
        raise ProfileConfigError("Only the chat_completions request adapter is implemented")
    endpoint = _text(p.get("endpoint", "/chat/completions"), "endpoint")
    if not endpoint.startswith("/") or endpoint.startswith("//") or any(c in endpoint for c in "?#\r\n"):
        raise ProfileConfigError("endpoint must be a relative absolute-path without query or fragment")
    api_key_env = _text(p.get("api_key_env", ""), "api_key_env", empty=True)
    api_key = _text(environment.get(api_key_env, ""), "api_key", empty=True)
    if _bool(p.get("api_key_required", False), "api_key_required") and not api_key.strip():
        raise ProfileConfigError("Required provider API key is missing")
    auth_header = _text(p.get("auth_header", "Authorization"), "auth_header")
    auth_prefix = _text(p.get("auth_prefix", "Bearer "), "auth_prefix", empty=True)
    headers = p.get("headers", {})
    if not isinstance(headers, dict):
        raise ProfileConfigError("headers must be a table")
    for key, value in {**headers, auth_header: auth_prefix + api_key}.items():
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key):
            raise ProfileConfigError("Invalid HTTP header name")
        if not isinstance(value, str) or "\r" in value or "\n" in value:
            raise ProfileConfigError("Invalid HTTP header value")
    if any(key.lower() in {auth_header.lower(), "authorization", "proxy-authorization", "x-api-key", "cookie"} for key in headers):
        raise ProfileConfigError("Authentication headers must come from api_key_env")

    cap_data = _table(data.get("capabilities", {}), {f.name for f in fields(Capabilities)}, "capabilities")
    caps = Capabilities(**{k: _bool(v, "capabilities." + k) for k, v in cap_data.items()})
    if caps.strict_json_schema and not caps.json_schema:
        raise ProfileConfigError("strict_json_schema requires json_schema")
    req_data = _table(data.get("request", {}), {f.name for f in fields(RequestMapping)}, "request")
    req = RequestMapping(**req_data)
    if _text(req.max_tokens_field, "request.max_tokens_field") not in {"max_tokens", "max_completion_tokens"}:
        raise ProfileConfigError("Unsupported max_tokens_field")
    _bool(req.stream_usage, "request.stream_usage")
    if req.stream_usage and not caps.streaming:
        raise ProfileConfigError("stream_usage requires streaming")
    _text(req.reasoning_field, "request.reasoning_field", empty=True)
    if not isinstance(req.defaults, dict) or not isinstance(req.reasoning_values, dict):
        raise ProfileConfigError("request defaults and reasoning_values must be tables")
    try:
        json.dumps(req.defaults, allow_nan=False)
        json.dumps(req.reasoning_values, allow_nan=False)
    except (ValueError, TypeError):
        raise ProfileConfigError("request mappings must contain valid JSON values") from None
    reserved = {"model", "messages", "stream", "stream_options", "response_format", "tools", "temperature", "max_tokens", "max_completion_tokens"}
    if req.defaults.keys() & reserved:
        raise ProfileConfigError("request.defaults cannot override canonical request fields")
    if caps.reasoning:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*", req.reasoning_field) or not req.reasoning_values:
            raise ProfileConfigError("reasoning requires a field path and explicit value mapping")
        if req.reasoning_field.split(".")[0] in reserved:
            raise ProfileConfigError("reasoning_field conflicts with a canonical request field")
    elif req.reasoning_field or req.reasoning_values:
        raise ProfileConfigError("Reasoning mappings require the reasoning capability")

    usage_data = _table(data.get("usage", {}), {f.name for f in fields(UsageNormalizer)} | {"stream_mode"}, "usage")
    stream_mode = usage_data.pop("stream_mode", "cumulative")
    if _text(stream_mode, "usage.stream_mode") not in {"cumulative", "delta"}:
        raise ProfileConfigError("usage.stream_mode must be cumulative or delta")
    for key, value in usage_data.items():
        if key == "input_includes_cache":
            _bool(value, "usage." + key)
        else:
            _text(value, "usage." + key, empty=key not in {"input_tokens", "output_tokens"})
    normalizer = UsageNormalizer(**usage_data)

    err_data = _table(data.get("errors", {}), {f.name for f in fields(ErrorClassifier)}, "errors")
    classifier = ErrorClassifier(**err_data)
    if not isinstance(classifier.code_paths, (tuple, list)) or not all(isinstance(v, str) and v for v in classifier.code_paths):
        raise ProfileConfigError("errors.code_paths must be a list of paths")
    for mapping in (classifier.codes, classifier.statuses):
        if not isinstance(mapping, dict) or not all(isinstance(v, str) and v in KINDS for v in mapping.values()):
            raise ProfileConfigError("Invalid error classification mapping")
    if not all(re.fullmatch(r"[1-5][0-9]{2}", key) for key in classifier.statuses):
        raise ProfileConfigError("errors.statuses keys must be HTTP status codes")
    classifier = ErrorClassifier(
        code_paths=tuple(classifier.code_paths), codes=classifier.codes,
        statuses={**ErrorClassifier().statuses, **classifier.statuses},
    )

    price_data = _table(data.get("pricing", {}), {f.name for f in fields(PriceTable)}, "pricing")
    for key, value in price_data.items():
        _text(value, "pricing." + key, empty=key in {"source", "effective_date"})
        if key.endswith("_per_million"):
            try:
                rate = Decimal(value)
            except InvalidOperation:
                raise ProfileConfigError("Invalid decimal price") from None
            if not rate.is_finite() or rate < 0:
                raise ProfileConfigError("Prices must be finite and non-negative")
            price_data[key] = rate
    if price_data.get("currency", "USD") not in {"USD", "CNY", "EUR"}:
        raise ProfileConfigError("Unsupported pricing currency")
    if any(key.endswith("_per_million") for key in price_data):
        if not price_data.get("source") or not price_data.get("effective_date"):
            raise ProfileConfigError("Configured prices require source and effective_date")
    if price_data.get("effective_date"):
        try:
            date.fromisoformat(price_data["effective_date"])
        except ValueError:
            raise ProfileConfigError("pricing.effective_date must use YYYY-MM-DD") from None
    prices = PriceTable(**price_data)
    return ProviderProfile(
        name=name, model=model, base_url=base_url, protocol=protocol, endpoint=endpoint,
        api_key=api_key, auth_header=auth_header, auth_prefix=auth_prefix, headers=headers,
        capabilities=caps, request=req, usage_normalizer=normalizer,
        error_classifier=classifier, prices=prices, usage_stream_mode=stream_mode,
    )


def load_active_profile(root: str | Path, overrides: Mapping[str, str] | None = None) -> ProviderProfile:
    """Single-profile entry point; future per-call routing reuses load_profile."""
    root = Path(root)
    env = read_environment(root / ".env", overrides)
    selected = _text(env.get("PROVIDER_PROFILE"), "PROVIDER_PROFILE")
    return load_profile(root / selected, env)
