import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from olympiad.cli import ASSETS
from olympiad.reports import compare_runs, load_run, write_reports

SCRIPTS = ASSETS / "scripts"
sys.path.insert(0, str(SCRIPTS))
from report_stream import ReportStream


def module(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


controller = module("pi-coding-suite-bench")
task_runner = module("swe-agent-bench")


def fixture(root, label="fixture-model"):
    root.mkdir()
    manifest = {"suite_id": "fixture-v1", "tasks": [
        {"instance_id": "pass-task", "language": "Python"},
        {"instance_id": "fail-task", "language": "TypeScript"},
        {"instance_id": "timeout-task", "language": "Rust"},
        {"instance_id": "infra-task", "language": "C++"},
        {"instance_id": "missing-task", "language": "Python"}]}
    (root / "suite-manifest.json").write_text(json.dumps(manifest))
    pin = hashlib.sha256((root / "suite-manifest.json").read_bytes()).hexdigest()
    (root / "suite-result.json").write_text(json.dumps({"run_id": root.name, "model_label": label, "suite_manifest_sha256": pin, "batch_wall_seconds": 10, "status": "complete"}))
    for ident, result in {
        "pass-task": {"score": {"passed": True}, "task_wall_seconds": 2.5, "agent": {"usage": {"input": 8, "output": 2}, "requests": [{"time_to_first_generated_content_seconds": 0.5}]}},
        "fail-task": {"score": {"passed": False}},
        "timeout-task": {"agent": {"timed_out": True}},
        "infra-task": {"infra_error": "fixture unavailable"},
    }.items():
        path = root / "tasks" / ident
        path.mkdir(parents=True)
        (path / "result.json").write_text(json.dumps(result))
    return root


class StreamingTests(unittest.TestCase):
    def test_text_reaches_stdout_before_completion(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            report = ReportStream()
            report.emit("task_finished", "1/15 fixture: pass")
            self.assertIn("1/15 fixture: pass\n", output.getvalue())
            self.assertNotIn("complete:", output.getvalue())

    def test_file_is_readable_after_each_flushed_event_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.txt"
            report = ReportStream(path)
            try:
                report.emit("started", "fixture")
                first = path.read_text()
                report.emit("task_finished", "fixture pass")
                self.assertTrue(path.read_text().startswith(first))
                self.assertEqual(len(path.read_text().splitlines()), 2)
                with self.assertRaises(FileExistsError):
                    ReportStream(path)
            finally:
                report.close()

    def test_each_ndjson_event_is_complete_without_closing_document(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            report = ReportStream(format="ndjson")
            report.emit("task_finished", "quoted \"task\"\nname", task="fixture", status="pass")
            self.assertEqual(json.loads(output.getvalue())["status"], "pass")
            report.emit("complete", "done")
        self.assertEqual([json.loads(line)["event"] for line in output.getvalue().splitlines()], ["task_finished", "complete"])

    def test_live_controller_emits_each_completed_task(self):
        from types import SimpleNamespace
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(controller, "execute_task", return_value={"score": {"passed": True}}), patch.object(controller, "REPORT", ReportStream()):
            # Build the sink inside redirect_stdout; ReportStream captures stdout.
            controller.REPORT = ReportStream()
            rows = controller.parallel_stage(SimpleNamespace(), [{"instance_id": "a"}, {"instance_id": "b"}], Path("unused"), "tasks")
        self.assertEqual(len(rows), 2)
        self.assertEqual(out.getvalue().count("task_finished:"), 2)

    def test_actual_worker_pool_obeys_limit_and_refills_all_task_slots(self):
        from types import SimpleNamespace
        for limit in (1, 2, 4):
            with self.subTest(limit=limit):
                lock = threading.Lock()
                first_wave = threading.Event()
                counters = {"active": 0, "peak": 0, "finished": 0}

                def execute(*args):
                    with lock:
                        counters["active"] += 1
                        counters["peak"] = max(counters["peak"], counters["active"])
                        if counters["active"] == limit:
                            first_wave.set()
                    self.assertTrue(first_wave.wait(2), "Configured agent slots must actually run concurrently")
                    time.sleep(0.01)
                    with lock:
                        counters["active"] -= 1
                        counters["finished"] += 1
                    return {"score": {"passed": True}}

                with contextlib.redirect_stdout(io.StringIO()), patch.object(controller, "WORKERS", limit), patch.object(controller, "execute_task", side_effect=execute), patch.object(controller, "REPORT", ReportStream()):
                    controller.REPORT = ReportStream()
                    rows = controller.parallel_stage(SimpleNamespace(), [{"instance_id": str(i)} for i in range(8)], Path("unused"), "tasks")
                self.assertEqual(counters, {"active": 0, "peak": limit, "finished": 8})
                self.assertEqual(len(rows), 8)


class FixtureAndAuthTests(unittest.TestCase):
    def test_all_15_original_task_hashes_and_reference_tests_match(self):
        manifest, counts = controller.load_and_validate_manifest(controller.MANIFEST)
        self.assertEqual(len(manifest["tasks"]), 15)
        self.assertEqual(counts, {"Python": 6, "TypeScript": 6, "C++": 2, "Rust": 1})

    def test_api_secret_is_referenced_not_serialized(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"OLYMPIAD_API_KEY": "fixture-do-not-persist"}):
            path = Path(tmp) / "config"
            task_runner.write_pi_config(path, "https://example.invalid/v1", "fixture", "fixture")
            value = (path / "models.json").read_text()
            self.assertNotIn("fixture-do-not-persist", value)
            self.assertIn("${OLYMPIAD_API_KEY}", value)
            request = controller.endpoint_request("https://example.invalid/v1/models")
            self.assertEqual(request.get_header("Authorization"), "Bearer fixture-do-not-persist")

    def test_explicit_model_does_not_require_gateway_discovery(self):
        with patch.object(controller.urllib.request, "urlopen", side_effect=AssertionError("No network")):
            self.assertEqual(controller.check_endpoint("https://example.invalid/v1", "explicit-model"), "explicit-model")

    def test_model_configuration_does_not_force_reasoning_on_plain_servers(self):
        for enabled in ("0", "1"):
            with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"OLYMPIAD_REASONING": enabled}):
                path = Path(tmp) / "config"
                task_runner.write_pi_config(path, "http://localhost:8000/v1", "fixture", "fixture")
                config = json.loads((path / "models.json").read_text(encoding="utf-8"))
                self.assertEqual(config["providers"]["benchmark"]["models"][0]["reasoning"], enabled == "1")

    def test_reference_patch_is_not_allowed_to_modify_hidden_tests(self):
        self.assertTrue(task_runner.is_test_path("tests/test_example.py"))
        self.assertFalse(task_runner.is_test_path("src/implementation.py"))

    def test_pinned_licenses_ship_with_distribution(self):
        directory = ASSETS / "licenses"
        sources = json.loads((directory / "sources.json").read_text())
        self.assertEqual(len(sources), 7)
        for name, source in sources.items():
            self.assertEqual(hashlib.sha256((directory / (name + ".txt")).read_bytes()).hexdigest(), source["sha256"])


class CommandLineTests(unittest.TestCase):
    def command(self, *args):
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        return subprocess.run([sys.executable, "-m", "olympiad", *args], env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)

    def test_actual_stdout_and_file_modes_are_independently_parseable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "live.ndjson"
            stdout = self.command("run", "--plan-only", "--format", "ndjson")
            file = self.command("run", "--plan-only", "--format", "ndjson", "--output", str(path))
            self.assertEqual(stdout.returncode, 0, stdout.stderr)
            self.assertEqual(file.returncode, 0, file.stderr)
            self.assertEqual(file.stdout, "")
            a, b = json.loads(stdout.stdout), json.loads(path.read_text())
            a.pop("timestamp"); b.pop("timestamp")
            self.assertEqual(a, b)
            self.assertEqual(a["tasks"], 15)
            self.assertNotEqual(self.command("run", "--plan-only", "--output", str(path)).returncode, 0)

    def test_agent_limit_switch_and_alias_are_recorded_by_real_cli(self):
        for option, count in [("--agents", 1), ("--agents", 4), ("--concurrency", 2)]:
            with self.subTest(option=option, count=count):
                result = self.command("run", "--plan-only", "--format", "ndjson", option, str(count))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["concurrency"], count)

    def test_nonpositive_or_noninteger_agent_limits_are_rejected(self):
        for count in ["0", "-1", "1.5", "invalid"]:
            result = self.command("run", "--plan-only", "--agents", count)
            self.assertNotEqual(result.returncode, 0)

    def test_saved_report_can_stream_plain_text_and_ndjson_without_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = fixture(Path(tmp) / "run")
            plain = self.command("report", str(run))
            self.assertEqual(plain.returncode, 0, plain.stderr)
            self.assertIn("Accepted 1/5", plain.stdout)
            path = Path(tmp) / "saved.ndjson"
            file = self.command("report", str(run), "--format", "ndjson", "--output", str(path))
            self.assertEqual(file.returncode, 0, file.stderr)
            events = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(events[-1]["event"], "complete")
            self.assertEqual(events[-1]["counts"]["timeout"], 1)

    def test_private_credentials_in_endpoint_urls_are_rejected_in_both_argument_forms(self):
        for options in [["--base-url", "https://user:secret@example.invalid/v1"], ["--base-url=https://example.invalid/v1?api_key=secret"]]:
            run = self.command("run", "--plan-only", *options)
            self.assertNotEqual(run.returncode, 0)
            self.assertNotIn("user:secret", run.stderr)

    def test_report_exports_use_real_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = fixture(Path(tmp) / "run")
            output = Path(tmp) / "exports"
            result = self.command("export", str(run), "--output", str(output))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((output / "results.json").read_text())["counts"]["pass"], 1)


class ReportTests(unittest.TestCase):
    def test_reports_distinguish_failure_types_and_preserve_unknown_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = load_run(fixture(Path(tmp) / "run"))
            self.assertEqual(run["counts"], {"fail": 1, "infrastructure_error": 1, "not_run": 1, "pass": 1, "timeout": 1})
            self.assertEqual(run["pass_rate_all_tasks"], 0.2)
            self.assertEqual(run["pass_rate_evaluated_tasks"], 0.5)
            self.assertIsNone(run["tasks"][0]["total_tokens"])
            output = write_reports(run, Path(tmp) / "reports")
            self.assertEqual({p.name for p in output.iterdir()}, {"results.json", "tasks.csv", "report.md", "report.html", "junit.xml"})
            suite = ET.parse(output / "junit.xml").getroot()
            self.assertEqual(suite.attrib["failures"], "1")
            self.assertEqual(suite.attrib["errors"], "2")
            self.assertEqual(suite.attrib["skipped"], "1")
            with self.assertRaises(FileExistsError):
                write_reports(run, output)

    def test_html_csv_and_xml_treat_labels_as_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = load_run(fixture(Path(tmp) / "run", "<script>alert(1)</script>"))
            run["run_id"] = "fixture\x01run"
            run["tasks"][0]["task"] = "=HYPERLINK(\"bad\")"
            out = write_reports(run, Path(tmp) / "out")
            self.assertNotIn("<script>", (out / "report.html").read_text())
            self.assertIn("'=HYPERLINK", (out / "tasks.csv").read_text())
            ET.parse(out / "junit.xml")

    def test_comparison_does_not_claim_missing_configuration_is_comparable(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = load_run(fixture(Path(tmp) / "a"))
            b = load_run(fixture(Path(tmp) / "b"))
            result = compare_runs([a, b], Path(tmp) / "comparison")
            self.assertTrue(result["same_suite"])
            self.assertFalse(result["same_configuration"])

    def test_comparison_flags_a_changed_recorded_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = load_run(fixture(Path(tmp) / "a"))
            b = load_run(fixture(Path(tmp) / "b"))
            for run in (a, b):
                run["configuration"] = {key: "recorded-fixture" for key in run["configuration"]}
            self.assertTrue(compare_runs([a, b], Path(tmp) / "same")["same_configuration"])
            b["configuration"]["host"] = "different-host"
            self.assertFalse(compare_runs([a, b], Path(tmp) / "different")["same_configuration"])

    def test_changed_frozen_manifest_cannot_keep_old_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = fixture(Path(tmp) / "run")
            p = path / "suite-manifest.json"
            data = json.loads(p.read_text()); data["suite_id"] = "changed"
            p.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "hash"):
                load_run(path)

    def test_unsafe_task_ids_cannot_read_outside_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = fixture(Path(tmp) / "run")
            (path / "suite-manifest.json").write_text(json.dumps({"tasks": [{"instance_id": "../../escape"}]}))
            with self.assertRaisesRegex(ValueError, "Unsafe"):
                load_run(path)

    def test_original_evaluator_outcome_parser_stays_strict(self):
        task = {"FAIL_TO_PASS": ["test_fix (module.Case)"], "PASS_TO_PASS": ["test_old (module.Case)"]}
        outcomes = task_runner.parse_test_outcomes("test_fix (module.Case) ... ok\ntest_old (module.Case) ... skipped\n", task["FAIL_TO_PASS"] + task["PASS_TO_PASS"])
        self.assertFalse(task_runner.all_tests_pass(task, outcomes, 0))


if __name__ == "__main__":
    unittest.main()
