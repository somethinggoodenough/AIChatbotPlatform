"""Container entrypoint. Experimental 8 GiB settings, not a tuned benchmark."""

import os
import sys


def command(env):
    args = [
        sys.executable, "-m", "sglang.launch_server",
        "--model-path", env.get("SGLANG_MODEL") or "Qwen/Qwen2.5-0.5B-Instruct",
        "--host", "0.0.0.0", "--port", "30000", "--dtype", "float16",
        "--context-length", env.get("SGLANG_CONTEXT_LENGTH") or "4096",
        "--mem-fraction-static", env.get("SGLANG_MEM_FRACTION") or "0.65",
        "--max-running-requests", "4", "--disable-cuda-graph",
        "--enable-cache-report", "--enable-metrics",
        "--decode-log-interval", "1", "--stream-interval", "1",
    ]
    if env.get("SGLANG_API_KEY"):
        args.extend(["--api-key", env["SGLANG_API_KEY"]])
    return args


if __name__ == "__main__":
    os.execv(sys.executable, command(os.environ))
