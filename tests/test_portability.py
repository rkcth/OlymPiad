import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from olympiad.cli import main
from olympiad.assets.scripts import runtime, prerequisites, process_control, configuration
from olympiad.assets.scripts import server_http


def ok(stdout="ok", returncode=0):
    return SimpleNamespace(stdout=stdout, returncode=returncode)


class DiagnosticTests(unittest.TestCase):
    def inspect(self, docker=None, docker_return=0, missing=None):
        def which(name):
            return None if name in (missing or []) else "/tools/" + name

        def probe(command, **kwargs):
            if command[0].endswith("docker"):
                return ok(docker if docker is not None else json.dumps({"OSType": "linux", "ServerVersion": "fixture", "MemTotal": 16 * 1024**3}), docker_return)
            return ok("fixture")

        with tempfile.TemporaryDirectory() as tmp, patch.object(prerequisites.shutil, "which", side_effect=which), patch.object(prerequisites, "probe", side_effect=probe), patch.object(prerequisites, "evaluator_python", return_value=sys.executable), patch.object(prerequisites, "bash_command", return_value="/tools/bash"):
            return prerequisites.inspect_environment(Path(tmp), need_pi=False)

    def test_missing_docker_is_actionable_not_a_traceback(self):
        result = self.inspect(missing=["docker"])
        self.assertFalse(result["ready"])
        message = next(c["message"] for c in result["checks"] if c["name"] == "Docker")
        self.assertIn("not installed", message)
        self.assertIn("https://docs.docker.com", message)

    def test_stopped_docker_and_socket_permissions_have_different_guidance(self):
        for message, expected in [("Cannot connect", "Start Docker"), ("permission denied", "socket")]:
            result = self.inspect(docker=message, docker_return=1)
            self.assertFalse(result["ready"])
            self.assertIn(expected, next(c["message"] for c in result["checks"] if c["name"] == "Docker"))

    def test_windows_container_mode_is_rejected(self):
        result = self.inspect(docker=json.dumps({"OSType": "windows"}))
        self.assertFalse(result["ready"])
        self.assertIn("Linux containers", next(c["message"] for c in result["checks"] if c["name"] == "Docker"))

    def test_healthy_engine_does_not_require_pull_or_a_model(self):
        self.assertTrue(self.inspect()["ready"])

    def test_doctor_returns_all_failures_before_any_benchmark(self):
        diagnostic = {"ready": False, "checks": [{"name": "Docker", "status": "error", "message": "Install Docker"}, {"name": "Pi", "status": "error", "message": "Install Pi"}]}
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch("olympiad.cli.inspect_environment", return_value=diagnostic):
            self.assertEqual(main(["doctor", "--format", "ndjson"]), 2)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(events), 3)
        self.assertFalse(events[-1]["ready"])

    def test_run_with_missing_docker_stops_before_tasks_or_server_requests(self):
        from test_benchmark import controller
        import signal
        signals = [signal.SIGINT, signal.SIGTERM] + ([signal.SIGBREAK] if hasattr(signal, "SIGBREAK") else [])
        saved = {sig: signal.getsignal(sig) for sig in signals}
        diagnostic = {"ready": False, "checks": [{"name": "Docker", "status": "error", "message": "Docker is not installed; install Docker Desktop or Engine"}]}
        try:
            with contextlib.redirect_stdout(io.StringIO()), patch.object(sys, "argv", ["controller", "--endpoint-model", "fixture"]), patch.object(controller, "inspect_environment", return_value=diagnostic), patch.object(controller, "docker_preflight") as docker, patch.object(controller, "parallel_stage") as tasks, patch.object(controller, "check_server") as server:
                self.assertEqual(controller.main(), 2)
                docker.assert_not_called()
                tasks.assert_not_called()
                server.assert_not_called()
        finally:
            for sig, handler in saved.items():
                signal.signal(sig, handler)

    def test_readiness_error_does_not_start_a_task_or_touch_output_directory(self):
        from test_benchmark import CommandLineTests
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "not-created"
            env = {**os.environ, "PATH": "", "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
            result = subprocess.run([sys.executable, "-m", "olympiad", "run", "--endpoint-model", "fixture", "--output-root", str(output)], env=env, text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("Docker is not installed", result.stdout)
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse(output.exists())


class RuntimeTests(unittest.TestCase):
    def test_python_locks_are_complete_and_match_packaged_provenance(self):
        paths = runtime.python_lock_files()
        metadata = json.loads((runtime.ASSETS / "python-runtime/sources.json").read_text(encoding="utf-8"))
        lines = [line for path in paths for line in path.read_text(encoding="utf-8").splitlines() if line and not line.startswith("#")]
        self.assertEqual(len(lines), len(metadata["packages"]))
        for name, package in metadata["packages"].items():
            self.assertIn(f"{name}=={package['version']} --hash=sha256:{package['sha256']}", lines)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "python-runtime"
            root.mkdir()
            for path in paths:
                (root / path.name).write_bytes(path.read_bytes())
            (root / "sources.json").write_text(json.dumps(metadata), encoding="utf-8")
            with patch.object(runtime, "ASSETS", Path(tmp)):
                runtime.python_lock_files()
                (root / "requirements.txt").write_text("unlocked-package\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "missing or changed"):
                    runtime.python_lock_files()

    def test_evaluator_requires_the_locked_python_minor_version(self):
        for version in ([3, 9], [3, 12], [3, 14]):
            with patch.object(runtime, "probe", return_value=ok(json.dumps(version))):
                with self.assertRaisesRegex(ValueError, "requires Python 3.11"):
                    runtime.evaluator_python("fixture-python")
        with patch.object(runtime, "probe", return_value=ok("[3, 11]")):
            self.assertEqual(runtime.evaluator_python("fixture-python"), "fixture-python")

    def test_user_data_paths_are_platform_specific_and_overrideable(self):
        with patch.dict(os.environ, {"OLYMPIAD_HOME": ""}), patch.object(runtime.sys, "platform", "win32"), patch.dict(os.environ, {"LOCALAPPDATA": "/fixture/appdata"}):
            self.assertEqual(runtime.data_directory(), Path("/fixture/appdata/OlymPiad"))
        with patch.dict(os.environ, {"OLYMPIAD_HOME": ""}), patch.object(runtime.sys, "platform", "darwin"):
            self.assertEqual(runtime.data_directory(), Path.home() / "Library/Application Support/OlymPiad")
        with patch.dict(os.environ, {"OLYMPIAD_HOME": "/fixture/override"}):
            self.assertEqual(runtime.data_directory(), Path("/fixture/override").resolve())

    def test_old_node_is_rejected_before_install(self):
        with patch.object(runtime.shutil, "which", return_value="node"), patch.object(runtime, "probe", return_value=ok("v20.1.0")):
            with self.assertRaisesRegex(ValueError, "22.19"):
                runtime.node_command()

    def test_wrong_managed_pi_version_cannot_silently_run(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(runtime, "data_directory", return_value=Path(tmp)), patch.object(runtime, "node_command", return_value="node"):
            package = Path(tmp) / "pi/node_modules" / runtime.PI_PACKAGE
            package.mkdir(parents=True)
            (package / "package.json").write_text(json.dumps({"name": runtime.PI_PACKAGE, "version": "0.0.0"}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "setup --install-pi"):
                runtime.pi_command()

    def test_locked_runtime_has_exact_pi_version_and_integrity(self):
        lock = json.loads((runtime.ASSETS / "pi-runtime/package-lock.json").read_text(encoding="utf-8"))
        pi = lock["packages"]["node_modules/" + runtime.PI_PACKAGE]
        self.assertEqual(pi["version"], runtime.PI_VERSION)
        self.assertTrue(pi["integrity"].startswith("sha512-"))
        for name, value in lock["packages"].items():
            if name:
                self.assertIn("integrity", value)

    def test_windows_process_group_uses_windows_flags(self):
        with patch.object(process_control.os, "name", "nt"), patch.object(process_control.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True):
            self.assertEqual(process_control.group_options(), {"creationflags": 512})

    def test_windows_timeout_kills_only_own_process_tree(self):
        process = SimpleNamespace(pid=12345, poll=lambda: None)
        with patch.object(process_control.os, "name", "nt"), patch.object(process_control.subprocess, "run", return_value=ok()) as run:
            process_control.kill_tree(process)
        self.assertEqual(run.call_args.args[0], ["taskkill", "/PID", "12345", "/T", "/F"])


class ConfigurationTests(unittest.TestCase):
    def test_configuration_file_never_accepts_api_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"base_url": "https://example.invalid/v1", "api_key": "not-allowed"}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "API keys"):
                configuration.read_config(path)

    def test_cli_can_save_config_then_run_offline_with_an_override(self):
        from test_benchmark import CommandLineTests
        cli = CommandLineTests()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server settings.json"
            created = cli.command("configure", "--base-url", "https://example.invalid/v1", "--model", "fixture", "--agents", "4", "--output", str(path))
            self.assertEqual(created.returncode, 0, created.stderr)
            run = cli.command("run", "--config", str(path), "--plan-only", "--agents", "2", "--format", "ndjson")
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout)["concurrency"], 2)
            self.assertNotIn("API_KEY", path.read_text(encoding="utf-8"))

    def test_utf8_report_and_config_work_with_unicode_paths(self):
        from test_benchmark import CommandLineTests, fixture
        cli = CommandLineTests()
        with tempfile.TemporaryDirectory(prefix="benchmark 雪 ") as tmp:
            root = Path(tmp)
            run = fixture(root / "run", "Modèle 雪")
            output = root / "résultat.txt"
            result = cli.command("report", str(run), "--output", str(output))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Modèle 雪", output.read_text(encoding="utf-8"))


class ProcessTests(unittest.TestCase):
    def test_actual_pi_timeout_stops_a_spawned_child_process(self):
        from test_benchmark import task_runner
        with tempfile.TemporaryDirectory(prefix="benchmark process ") as tmp:
            marker = Path(tmp) / "unexpected-child-survived.txt"
            child = "import time;from pathlib import Path;time.sleep(2);Path(" + repr(str(marker)) + ").write_text('survived')"
            parent = "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c'," + repr(child) + "]);print('spawned',flush=True);time.sleep(30)"
            result = task_runner.run_pi([sys.executable, "-c", parent], Path(tmp), os.environ.copy(), timeout=1)
            self.assertTrue(result["timed_out"])
            self.assertIn("spawned", result["stdout"])
            time.sleep(2)
            self.assertFalse(marker.exists(), "A timed-out agent must not leave its own child running")

    def test_native_windows_virtualenv_executable_is_scripts_python(self):
        from test_benchmark import task_runner
        commands = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def run(command, **kwargs):
                commands.append(command)
                return {"command": command, "returncode": 0, "output": "", "wall_seconds": 0}
            with patch.object(task_runner.os, "name", "nt"), patch.object(task_runner, "run", side_effect=run):
                result = task_runner.make_venv("python", root / "venv", root)
            self.assertEqual(result, root / "venv/Scripts/python.exe")
            self.assertEqual(commands[1][0], str(result))
            self.assertTrue(all("--require-hashes" in command and "--only-binary=:all:" in command for command in commands[1:3]))
            self.assertTrue(all(flag in commands[3] for flag in ("--no-build-isolation", "--no-deps", "--no-index")))
            self.assertEqual(commands[4][-1], "check")


class ServerTests(unittest.TestCase):
    def test_http_auth_denial_is_actionable_without_showing_response_or_retrying(self):
        denied = urllib.error.HTTPError("https://example.invalid/v1/models", 401, "denied", {}, None)
        with patch.object(server_http, "read_models", side_effect=denied) as read:
            result = server_http.check_server("https://example.invalid/v1", "model")
        self.assertEqual(result["status"], "error")
        self.assertIn("OLYMPIAD_API_KEY", result["message"])
        self.assertEqual(read.call_count, 1)

    def test_gateway_without_models_requires_an_explicit_model(self):
        missing = urllib.error.HTTPError("https://example.invalid/v1/models", 404, "not found", {}, None)
        with patch.object(server_http, "read_models", side_effect=missing):
            self.assertEqual(server_http.check_server("https://example.invalid/v1", "model")["status"], "warning")
            self.assertEqual(server_http.check_server("https://example.invalid/v1")["status"], "error")

    def test_cross_origin_redirect_is_blocked_before_auth_can_be_forwarded(self):
        request = urllib.request.Request("https://example.invalid/v1/models", headers={"Authorization": "Bearer synthetic-key"})
        handler = server_http.SameOriginRedirect()
        with self.assertRaisesRegex(ValueError, "different origin"):
            handler.redirect_request(request, None, 302, "redirect", {}, "https://other.invalid/models")

    def test_multiple_models_need_an_explicit_selection(self):
        with patch.object(server_http, "read_models", return_value={"data": [{"id": "one"}, {"id": "two"}]}):
            self.assertEqual(server_http.check_server("https://example.invalid/v1")["status"], "error")
            self.assertEqual(server_http.check_server("https://example.invalid/v1", "two")["model"], "two")

    def test_provider_errors_are_not_counted_as_code_quality_failures(self):
        from test_benchmark import task_runner
        for message in ("401 unauthorized", "403 permission denied", "invalid request", "TLS failure"):
            kind, _ = task_runner.classify_pi_failure({"timed_out": False, "returncode": 0}, "error", message)
            self.assertEqual(kind, "infra")

    def test_missing_provider_usage_is_unknown_not_zero(self):
        from test_benchmark import task_runner
        event = {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}}
        usage, *_ = task_runner.parse_pi_metrics(json.dumps(event))
        self.assertIsNone(usage["input"])
        self.assertIsNone(usage["totalTokens"])

    def test_completion_report_does_not_invent_usage_or_crash_when_it_is_missing(self):
        from test_benchmark import controller
        manifest, _ = controller.load_and_validate_manifest(controller.MANIFEST)
        rows = {task["instance_id"]: {"score": {"passed": True}} for task in manifest["tasks"]}
        summary = controller.summarize(manifest, rows, "synthetic-report-contract", "fixture", "fixture", "off", 0, "fixture", time.monotonic())
        self.assertIsNone(summary["token_totals"]["totalTokens"])
        self.assertEqual(summary["token_measured_tasks"]["totalTokens"], 0)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.md"
            controller.write_report(path, summary, rows, manifest)
            self.assertIn("null", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
