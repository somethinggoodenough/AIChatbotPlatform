"""Probe logic tested against a LOCAL FAKE HTTP server; not SGLang acceptance."""

import importlib.util
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPIKES = ROOT / "experiments/phase_0_serving_baseline/spikes"
sys.path.insert(0, str(SPIKES))
import _common as common
from ai_serving.config.providers import load_profile


def module(filename):
    spec = importlib.util.spec_from_file_location("spike_" + filename[:2], SPIKES / filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


streaming = module("01_streaming.py")
structured = module("02_structured_output.py")
prefix = module("03_prefix_cache.py")
disconnect = module("04_disconnect.py")


class State:
    def __init__(self):
        self.running = 0
        self.calls = []
        self.missing_usage = False
        self.no_cache = False
        self.ignore_disconnect = False
        self.disconnected = threading.Event()


def handler_for(state):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def send_json(self, body):
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/metrics":
                text = f'sglang:num_running_reqs{{model_name="fake"}} {state.running}\nsglang:num_queue_reqs{{model_name="fake"}} 0\n'
                data = text.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_json({"version": "FAKE-NOT-SGLANG"})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.calls.append(body)
            schema = "response_format" in body
            text = json.dumps({"action": "reply", "reason": "greeting", "priority": 1}) if schema else "One two three four five six seven eight nine ten."
            cached = 0 if state.no_cache or len(state.calls) == 1 else 900
            usage = {"prompt_tokens": 1000, "completion_tokens": 12,
                     "prompt_tokens_details": {"cached_tokens": cached}}
            if not body.get("stream"):
                self.send_json({"id": "fake", "usage": usage, "choices": [{"finish_reason": "stop", "message": {"content": text}}]})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            forced = body.get("ignore_eos", False)
            chunks = ["x "] * body["max_tokens"] if forced else [text[:10], text[10:]]
            state.running += 1

            def emit(event):
                data = event if isinstance(event, str) else json.dumps(event)
                self.wfile.write(("data: " + data + "\n\n").encode())
                self.wfile.flush()

            try:
                for index, chunk in enumerate(chunks):
                    try:
                        emit({"id": "fake-stream", "choices": [{"delta": {"content": chunk}, "finish_reason": None}]})
                    except (BrokenPipeError, ConnectionResetError):
                        state.disconnected.set()
                        if state.ignore_disconnect:
                            time.sleep(0.01 * (len(chunks) - index))
                        return
                    time.sleep(0.01)
                emit({"choices": [{"delta": {}, "finish_reason": "length" if forced else "stop"}]})
                if not state.missing_usage:
                    usage["completion_tokens"] = len(chunks) if forced else 12
                    emit({"choices": [], "usage": usage})
                emit("[DONE]")
            except (BrokenPipeError, ConnectionResetError):
                state.disconnected.set()
            finally:
                state.running -= 1
    return Handler


class ProbeUnitTests(unittest.TestCase):
    def setUp(self):
        self.state = State()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.state))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.profile = load_profile(ROOT / "configs/providers/local_sglang.toml", {
            "SGLANG_MODEL": "fake", "SGLANG_BASE_URL": f"http://127.0.0.1:{self.server.server_port}/v1",
        })
        self.args = SimpleNamespace(timeout=5, max_output_tokens=80, prefix_lines=64, requests=3,
                                    min_prefix_tokens=512, abort_deadline=0.25, stable_window=0.03,
                                    poll_interval=0.01, close_after_chunks=3)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_stream_reads_usage_after_empty_choices(self):
        evidence = {}
        streaming.probe(self.profile, self.args, evidence)
        self.assertEqual(self.state.calls[0]["stream_options"], {"include_usage": True})
        self.assertEqual(evidence["normalized_usage"]["output_tokens"], 12)
        self.assertTrue(evidence["done"])

    def test_missing_usage_is_failure(self):
        self.state.missing_usage = True
        with self.assertRaises(common.ProbeFailure):
            streaming.probe(self.profile, self.args, {})

    def test_structured_nonstream_and_stream_use_the_schema(self):
        evidence = {}
        structured.probe(self.profile, self.args, evidence)
        self.assertEqual(len(evidence["trials"]), 2)
        self.assertTrue(all(row["parsed"]["action"] == "reply" for row in evidence["trials"]))
        self.assertTrue(all("response_format" in body for body in self.state.calls))

    def test_prefix_cache_requires_substantial_hits(self):
        evidence = {}
        prefix.probe(self.profile, self.args, evidence)
        self.assertEqual([row["cached_tokens"] for row in evidence["trials"]], [0, 900, 900])
        self.assertEqual(self.state.calls[0]["messages"][0], self.state.calls[1]["messages"][0])
        self.assertNotEqual(self.state.calls[0]["messages"][1], self.state.calls[1]["messages"][1])

    def test_prefix_no_hits_fails(self):
        self.state.no_cache = True
        with self.assertRaises(common.ProbeFailure):
            prefix.probe(self.profile, self.args, {})

    def test_disconnect_closes_tcp_and_waits_for_server_idle(self):
        evidence = {}
        disconnect.probe(self.profile, self.args, evidence)
        self.assertTrue(self.state.disconnected.is_set())
        self.assertTrue(evidence["server_request_disappeared"])
        self.assertEqual(self.state.running, 0)

    def test_client_close_without_server_cleanup_does_not_pass(self):
        self.state.ignore_disconnect = True
        with self.assertRaises(common.ProbeFailure):
            disconnect.probe(self.profile, self.args, {})
        self.assertTrue(self.state.disconnected.is_set())


class ParserTests(unittest.TestCase):
    def test_sse_comments_crlf_and_multiline_data(self):
        data = b': heartbeat\r\ndata: {"choices":\r\ndata: []}\r\n\r\ndata: [DONE]\r\n\r\n'
        self.assertEqual(list(common.sse_data(io.BytesIO(data))), ['{"choices":\n[]}', '[DONE]'])

    def test_eof_is_not_done(self):
        self.assertEqual(list(common.sse_data(io.BytesIO(b'data: {"choices":[]}\n\n'))), ['{"choices":[]}'])

    def test_heartbeat_cannot_bypass_total_deadline(self):
        with self.assertRaises(common.ProbeFailure):
            list(common.sse_data(io.BytesIO(b': heartbeat\n\n'), deadline=time.monotonic() - 1))

    def test_fixed_schema_rejects_repairable_text_and_wrong_types(self):
        for text in ('```json\n{}\n```', '{"action":"other","reason":"x","priority":1}',
                     '{"action":"reply","reason":"x","priority":true}',
                     '{"action":"reply","reason":"x","priority":1,"extra":0}'):
            with self.subTest(text=text), self.assertRaises((ValueError, common.ProbeFailure)):
                structured.parse_decision(text)

    def test_metric_parser_ignores_help_and_accepts_scientific_notation(self):
        rows = common.metric_samples('# HELP sglang:num_running_reqs Running\nsglang:num_running_reqs{model_name="a"} 1e0\n', 'sglang:num_running_reqs')
        self.assertEqual(rows[0]["value"], 1)


if __name__ == "__main__":
    unittest.main()
