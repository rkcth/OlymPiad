"""Opt-in real pinned Pi against a local synthetic HTTP server; no paid API."""

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch

from olympiad.assets.scripts.runtime import pi_command


@unittest.skipUnless(os.environ.get("OLYMPIAD_PI_SMOKE") == "1", "Opt in after setup --install-pi; uses only a synthetic loopback server")
class RealPiTests(unittest.TestCase):
    def test_real_pinned_pi_calls_synthetic_openai_endpoint_and_parses_completion(self):
        from test_benchmark import task_runner
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append({"path": self.path, "authorization": self.headers.get("Authorization"), "body": body})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream" if body.get("stream") else "application/json")
                self.end_headers()
                base = {"id": "private-smoke", "created": 1, "model": "synthetic-benchmark-model"}
                has_tools = any(message.get("role") == "tool" for message in body.get("messages", []))
                delta = {"role": "assistant", "content": "BENCHMARK_SMOKE_OK"} if has_tools else {"role": "assistant", "tool_calls": [
                    {"index": 0, "id": "synthetic-read", "type": "function", "function": {"name": "read", "arguments": json.dumps({"path": "smoke.txt"})}},
                    {"index": 1, "id": "synthetic-bash", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": "printf BENCHMARK_BASH_OK"})}},
                ]}
                finish = "stop" if has_tools else "tool_calls"
                if body.get("stream"):
                    events = [
                        {**base, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                        {**base, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}], "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12}},
                    ]
                    for event in events:
                        self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode("utf-8"))
                        self.wfile.flush()
                    self.wfile.write(b"data: [DONE]\n\n")
                else:
                    self.wfile.write(json.dumps({**base, "object": "chat.completion", "choices": [{"index": 0, "message": {"role": "assistant", "content": "BENCHMARK_SMOKE_OK"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12}}).encode("utf-8"))
                self.close_connection = True

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="olympiad ü ") as tmp:
                root = Path(tmp)
                with patch.dict(os.environ, {"OLYMPIAD_API_KEY": "synthetic-loopback-key-only"}):
                    task_runner.write_pi_config(root / "config", f"http://127.0.0.1:{server.server_port}/v1", "synthetic-benchmark-model", "Synthetic integration test")
                env = {k: v for k, v in os.environ.items() if k in {"PATH", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP", "COMSPEC", "PATHEXT", "ProgramFiles", "ProgramFiles(x86)", "HOME", "USERPROFILE", "LOCALAPPDATA"}}
                env.update({"PI_CODING_AGENT_DIR": str(root / "config"), "PI_OFFLINE": "1", "PI_SKIP_VERSION_CHECK": "1", "NO_COLOR": "1", "OLYMPIAD_API_KEY": "synthetic-loopback-key-only"})
                (root / "smoke.txt").write_text("BENCHMARK_READ_OK 雪", encoding="utf-8")
                prompt = "Read smoke.txt, run printf BENCHMARK_BASH_OK, then reply BENCHMARK_SMOKE_OK. " + "Long prompt 雪 " * 4000
                result = task_runner.run_pi(pi_command() + ["--model", "benchmark/synthetic-benchmark-model", "--thinking", "off", "--mode", "json", "--no-session", "--no-approve", "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-themes", "--no-mcp", "--tools", "read,bash,edit,write", "-p"], root, env, 40, input_text=prompt)
                self.assertEqual(result["returncode"], 0, result["stderr"] + result["stdout"][-4000:])
                self.assertFalse(result["timed_out"])
                self.assertEqual(len(calls), 2, [{"path": call["path"]} for call in calls])
                self.assertEqual(calls[0]["path"], "/v1/chat/completions")
                self.assertEqual(calls[0]["authorization"], "Bearer synthetic-loopback-key-only")
                self.assertEqual(calls[0]["body"]["model"], "synthetic-benchmark-model")
                user_messages = [m for m in calls[0]["body"]["messages"] if m["role"] == "user"]
                content = user_messages[-1]["content"]
                received = content if isinstance(content, str) else "\n".join(item.get("text", "") for item in content)
                # Pi trims outer stdin whitespace. Compare the entire remaining
                # payload, not a prefix: this detects command/pipe truncation.
                import hashlib
                self.assertEqual(hashlib.sha256(received.strip().encode("utf-8")).hexdigest(), hashlib.sha256(prompt.strip().encode("utf-8")).hexdigest())
                self.assertIn("BENCHMARK_SMOKE_OK", result["stdout"])
                usage, tools, stop, error, requests = task_runner.parse_pi_metrics(result["stdout"], result["line_elapsed_seconds"])
                self.assertEqual(stop, "stop", error)
                self.assertEqual(tools, 2)
                ends = [json.loads(line) for line in result["stdout"].splitlines() if line.startswith("{") and json.loads(line).get("type") == "tool_execution_end"]
                self.assertEqual(len(ends), 2)
                self.assertTrue(all(not event.get("isError") for event in ends))
                outputs = json.dumps([message for message in calls[1]["body"]["messages"] if message.get("role") == "tool"], ensure_ascii=False)
                self.assertIn("BENCHMARK_READ_OK 雪", outputs)
                self.assertIn("BENCHMARK_BASH_OK", outputs)
                self.assertEqual(usage["totalTokens"], 24)
                self.assertEqual(len(requests), 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
