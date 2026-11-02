"""Small synchronous HTTP/SSE helpers for live probes, not the production backend."""

import argparse
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import http.client
import json
import math
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit, urlunsplit
import uuid

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from ai_serving.config.providers import ProfileConfigError, load_profile, read_environment


class ProbeFailure(Exception):
    pass


class Blocked(Exception):
    pass


class Inconclusive(Exception):
    pass


def require(condition, message):
    if not condition:
        raise ProbeFailure(message)


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be positive and finite")
    return number


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def parser(description, both=False):
    result = argparse.ArgumentParser(description=description)
    result.add_argument("--target", choices=("local", "api", "both") if both else ("local",), default="both" if both else "local")
    result.add_argument("--timeout", type=positive, default=120, help="HTTP read timeout and per-stream total deadline, seconds")
    result.add_argument("--output-dir", type=Path, default=ROOT / "artifacts/spikes")
    return result


def profile_for(target):
    env = read_environment(ROOT / ".env")
    file = "local_sglang.toml" if target == "local" else "deepseek.toml"
    # Allows independent model profiles without making the business code branch.
    selected = env.get("SGLANG_SPIKE_PROFILE" if target == "local" else "API_SPIKE_PROFILE")
    path = ROOT / selected if selected else ROOT / "configs/providers" / file
    try:
        profile = load_profile(path, env)
    except (ProfileConfigError, FileNotFoundError) as exc:
        raise Blocked(f"{target} profile is incomplete; fill its .env variables ({type(exc).__name__})") from None
    return profile, path


@contextmanager
def open_http(profile, *, body=None, url=None, timeout=120):
    address = urlsplit(url or profile.url)
    cls = http.client.HTTPSConnection if address.scheme == "https" else http.client.HTTPConnection
    connection = cls(address.hostname, address.port, timeout=timeout)
    response = None
    try:
        path = urlunsplit(("", "", address.path or "/", address.query, ""))
        headers = {**profile.request_headers(), "Connection": "close"}
        connection.request("POST" if body is not None else "GET", path,
                           body=json.dumps(body).encode() if body is not None else None, headers=headers)
        response = connection.getresponse()
        if not 200 <= response.status < 300:
            # Deliberately do not echo upstream bodies/headers that might contain credentials.
            raise ProbeFailure(f"HTTP {response.status}; no redirects or retries performed")
        yield response
    finally:
        if response is not None:
            response.close()
        connection.close()  # Also closes unfinished streams; never sends an abort endpoint call.


def get_text(profile, url, timeout=10):
    with open_http(profile, url=url, timeout=timeout) as response:
        return response.read().decode("utf-8")


def post_json(profile, body, timeout):
    with open_http(profile, body=body, timeout=timeout) as response:
        result = json.loads(response.read())
    require(isinstance(result, dict) and "error" not in result, "JSON response contains an error or is not an object")
    return result


def server_url(profile, suffix):
    # These probes target a directly exposed SGLang instance, without a path-prefix proxy.
    address = urlsplit(profile.base_url)
    return urlunsplit((address.scheme, address.netloc, suffix, "", ""))


def sse_data(response, deadline=None):
    """SSE framing: comments, CRLF, and multiple data lines; EOF is NOT [DONE]."""
    data = []
    while True:
        if deadline is not None:
            require(time.monotonic() < deadline, "SSE total deadline exceeded")
        raw = response.readline(1_048_577)
        if len(raw) > 1_048_576:
            raise ProbeFailure("SSE line exceeds 1 MiB")
        if not raw:
            if data:
                yield "\n".join(data)
            return
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                yield "\n".join(data)
                data = []
        elif line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)


def stream_events(profile, body, timeout):
    started = time.monotonic()
    with open_http(profile, body=body, timeout=timeout) as response:
        require("text/event-stream" in response.getheader("Content-Type", ""), "Expected SSE Content-Type")
        for data in sse_data(response, started + timeout):
            require(time.monotonic() - started < timeout, "Stream total deadline exceeded")
            if data == "[DONE]":
                yield None
                return
            event = json.loads(data)
            require(isinstance(event, dict) and "error" not in event and event.get("type") != "error", "Error event inside HTTP 200 stream")
            yield event


def collect_stream(profile, body, timeout):
    started = time.monotonic()
    evidence = {"content": "", "content_chunks": 0, "usage_frames": [], "finish_reasons": [], "done": False}
    acc = profile.usage_accumulator()
    first = last = None
    events = stream_events(profile, body, timeout)
    try:
        for event in events:
            if event is None:
                evidence["done"] = True
                break
            if event.get("usage") is not None:
                acc.update(event["usage"])
                evidence["usage_frames"].append(event["usage"])
            for choice in event.get("choices", []):
                delta = choice.get("delta") or {}
                require(not delta.get("refusal"), "Model refused the probe")
                text = delta.get("content")
                if text:
                    now = time.monotonic()
                    first = now if first is None else first
                    last = now
                    evidence["content"] += text
                    evidence["content_chunks"] += 1
                if choice.get("finish_reason"):
                    evidence["finish_reasons"].append(choice["finish_reason"])
    finally:
        events.close()
    evidence.update(elapsed_seconds=time.monotonic() - started,
                    ttft_seconds=None if first is None else first - started,
                    decode_seconds=0 if first is None else last - first,
                    normalized_usage=asdict(acc.normalized(profile.usage_normalizer)))
    return evidence


def check_completed_stream(evidence, min_chunks=2):
    require(evidence["done"], "Stream ended without [DONE]")
    require(evidence["content_chunks"] >= min_chunks, "Insufficient incremental content chunks")
    require(bool(evidence["finish_reasons"]) and set(evidence["finish_reasons"]) <= {"stop", "length"}, "Missing or unexpected finish reason")
    require(bool(evidence["usage_frames"]), "include_usage was requested but no usage frame arrived")
    usage = evidence["normalized_usage"]
    require(usage["input_tokens"] is not None and usage["input_tokens"] > 0, "No positive input token usage")
    require(usage["output_tokens"] is not None and usage["output_tokens"] > 0, "No positive output token usage")


def metric_samples(text, name):
    pattern = re.compile(r"^" + re.escape(name) + r'(?:\{([^\n]*)\})?\s+([^\s]+)(?:\s+[^\s]+)?$')
    result = []
    for line in text.splitlines():
        match = pattern.match(line)
        if match:
            value = float(match[2])
            require(math.isfinite(value) and value >= 0, "Non-finite or negative request gauge")
            result.append({"labels": match[1] or "", "value": value})
    return result


def running_snapshot(profile, timeout):
    text = get_text(profile, server_url(profile, "/metrics"), timeout)
    running = metric_samples(text, "sglang:num_running_reqs")
    queued = metric_samples(text, "sglang:num_queue_reqs")
    if not running or not queued:
        raise Inconclusive("Missing running/queue gauges; launch with --enable-metrics and warm up once")
    # Dedicated single-model server: aggregate all scheduler series conservatively.
    return {"running": sum(row["value"] for row in running), "queued": sum(row["value"] for row in queued),
            "samples": running, "queue_samples": queued}


def run(name, args, probe):
    report = {"spike": name, "started_at": datetime.now(timezone.utc).isoformat(),
              "client": {"python": platform.python_version(), "os": platform.system()},
              "results": [], "execution": "live_http", "timeout_seconds": args.timeout}
    try:
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=3)
        report["revision"] = revision.stdout.strip() if revision.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        report["revision"] = None
    report["probe_sha256"] = hashlib.sha256(Path(probe.__code__.co_filename).read_bytes()).hexdigest()
    report["helper_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    for target in (("local", "api") if args.target == "both" else (args.target,)):
        result = {"target": target, "status": "running", "evidence": {}}
        report["results"].append(result)
        try:
            profile, path = profile_for(target)
            result.update(profile=profile.name, model=profile.model,
                          profile_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            if target == "local":
                info = json.loads(get_text(profile, server_url(profile, "/get_server_info"), min(args.timeout, 10)))
                result["server_version"] = info.get("version")
                result["server_flags"] = {k: info.get(k) for k in ("enable_cache_report", "enable_metrics", "disable_radix_cache")}
            probe(profile, args, result["evidence"])
            result["status"] = "passed"
        except (Blocked, ProfileConfigError) as exc:
            result.update(status="blocked", reason=str(exc))
        except Inconclusive as exc:
            result.update(status="inconclusive", reason=str(exc))
        except ProbeFailure as exc:
            result.update(status="failed", reason=str(exc))
        except (TimeoutError, http.client.HTTPException) as exc:
            result.update(status="failed", reason=f"Transport interrupted or timed out: {type(exc).__name__}")
        except OSError as exc:
            result.update(status="blocked", reason=f"Connection/transport unavailable: {type(exc).__name__}")
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            result.update(status="failed", reason=f"Malformed response: {type(exc).__name__}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    path = args.output_dir / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{name}-{uuid.uuid4().hex[:8]}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"spike": name, "results": [{k: row[k] for k in ("target", "status", "reason") if k in row} for row in report["results"]], "report": str(path)}, ensure_ascii=False, indent=2))
    statuses = {row["status"] for row in report["results"]}
    return 0 if statuses == {"passed"} else (1 if "failed" in statuses else 2)
