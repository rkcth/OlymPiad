"""Normalize retained run artifacts; never invent missing usage or timing."""

import csv
import hashlib
from html import escape
import json
import math
from pathlib import Path
import re
import statistics
import xml.etree.ElementTree as ET

SCHEMA_VERSION = "1.0"
FIELDS = ("task", "language", "status", "task_seconds", "agent_seconds", "input_tokens",
          "output_tokens", "cache_read_tokens", "reasoning_tokens", "total_tokens",
          "median_ttft_seconds", "client_estimated_decode_tokens_per_second")
VALID_STATUSES = {"pass", "fail", "timeout", "infrastructure_error", "not_run"}


def read_json(path, default=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else default


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def median(values):
    present = [n for n in map(number, values) if n is not None]
    return statistics.median(present) if present else None


def classify(result, fallback=None):
    if result.get("infra_error"):
        return "infrastructure_error"
    if (result.get("agent") or {}).get("timed_out"):
        return "timeout"
    passed = (result.get("score") or {}).get("passed")
    if passed is True:
        return "pass"
    if passed is False:
        return "fail"
    return fallback if fallback in VALID_STATUSES else "not_run"


def load_run(directory):
    directory = Path(directory).resolve()
    summary = read_json(directory / "suite-result.json", {})
    meta = read_json(directory / "run-metadata.json", {})
    manifest = read_json(directory / "suite-manifest.json")
    if not manifest or not isinstance(manifest.get("tasks"), list):
        raise ValueError(f"Missing suite-manifest.json with tasks in {directory}")
    scores = summary.get("scores") or summary.get("batch_statuses") or {}
    rows, seen = [], set()
    for task in manifest["tasks"]:
        ident = task["instance_id"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", ident) or ident in seen:
            raise ValueError("Unsafe or duplicate task identifier")
        seen.add(ident)
        result_path = directory / "tasks" / ident / "result.json"
        if not result_path.resolve().is_relative_to(directory):
            raise ValueError("Result path escapes run directory")
        result = read_json(result_path, {})
        agent = result.get("agent") or {}
        usage = agent.get("usage") or {}
        requests = agent.get("requests") or []
        row = {"task": ident, "language": task.get("language", "unknown"),
               "status": classify(result, scores.get(ident)),
               "task_seconds": number(result.get("task_wall_seconds")),
               "agent_seconds": number(agent.get("wall_seconds")),
               "median_ttft_seconds": median(r.get("time_to_first_generated_content_seconds") for r in requests),
               "client_estimated_decode_tokens_per_second": median(r.get("client_estimated_decode_tokens_per_second") for r in requests)}
        for field, key in (("input_tokens", "input"), ("output_tokens", "output"),
                           ("cache_read_tokens", "cacheRead"), ("reasoning_tokens", "reasoning"),
                           ("total_tokens", "totalTokens")):
            row[field] = number(usage.get(key))
        rows.append(row)
    counts = {status: sum(r["status"] == status for r in rows) for status in sorted(VALID_STATUSES)}
    evaluated = counts["pass"] + counts["fail"]
    wall = number(summary.get("batch_wall_seconds"))
    manifest_hash = hashlib.sha256((directory / "suite-manifest.json").read_bytes()).hexdigest()
    recorded_hash = summary.get("suite_manifest_sha256") or meta.get("suite_manifest_sha256")
    if recorded_hash and recorded_hash != manifest_hash:
        raise ValueError("Recorded suite hash does not match the retained manifest")
    return {"schema_version": SCHEMA_VERSION,
            "run_id": summary.get("run_id") or meta.get("run_id") or directory.name,
            "suite_id": manifest.get("suite_id"),
            "suite_manifest_sha256": manifest_hash,
            "model": summary.get("model_label") or meta.get("model_label"),
            "endpoint_model": summary.get("endpoint_model") or meta.get("endpoint_model"),
            "execution_status": summary.get("status", "incomplete"),
            "configuration": {k: meta.get(k) for k in ("thinking", "concurrency", "context_window", "max_completion_tokens", "prompt_policy", "sampling_policy", "pi_version", "task_runner_sha256", "suite_controller_sha256", "host", "node_version", "pi_dependency_lock_sha256", "python_dependency_lock_sha256", "evaluator_python_version")},
            "counts": counts, "task_count": len(rows), "evaluated_tasks": evaluated,
            "pass_rate_all_tasks": counts["pass"] / len(rows) if rows else None,
            "pass_rate_evaluated_tasks": counts["pass"] / evaluated if evaluated else None,
            "batch_wall_seconds": wall,
            "accepted_fixes_per_hour": counts["pass"] * 3600 / wall if wall else None,
            "tasks": rows,
            "metric_notes": ["Missing measurements are null, not zero.",
                             "Timing rates are client-observed estimates, not engine kernel timings.",
                             "Timeouts, infrastructure failures and missing tasks remain distinct from correctness failures.",
                             "This small selected suite is not an official SWE-bench leaderboard score."]}


def text(value):
    if value is None:
        return "—"
    return f"{value:.3f}" if isinstance(value, float) else str(value)


def markdown_cell(value):
    return text(value).replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ").replace("<", "&lt;").replace(">", "&gt;")


def safe_csv(value):
    value = "" if value is None else str(value)
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def xml_text(value):
    return "".join(c for c in str(value) if c in "\t\n\r" or 0x20 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF)


def write_reports(run, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output / "results.json").write_text(json.dumps(run, indent=2, allow_nan=False) + "\n", encoding='utf-8')
    with (output / "tasks.csv").open("w", newline="", encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(FIELDS)
        for row in run["tasks"]:
            writer.writerow(safe_csv(row.get(k)) for k in FIELDS)
    title = f"Coding Benchmark: {run['model']}"
    lines = ["# " + markdown_cell(title), "", f"Run: {markdown_cell(run['run_id'])}", "",
             f"Accepted fixes: **{run['counts']['pass']}/{run['task_count']}**. "
             f"Quality failures: {run['counts']['fail']}; timeouts: {run['counts']['timeout']}; "
             f"infrastructure errors: {run['counts']['infrastructure_error']}; not run: {run['counts']['not_run']}.", "",
             "| Task | Language | Status | Task seconds | Total tokens |",
             "|---|---|---|---:|---:|"]
    for row in run["tasks"]:
        lines.append("| " + " | ".join(markdown_cell(row[k]) for k in ("task", "language", "status", "task_seconds", "total_tokens")) + " |")
    lines += ["", *["- " + note for note in run["metric_notes"]], ""]
    (output / "report.md").write_text("\n".join(lines), encoding='utf-8')
    table = "".join("<tr>" + "".join("<td>" + escape(text(row[k])) + "</td>" for k in FIELDS) + "</tr>" for row in run["tasks"])
    html = '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">'
    html += "<title>" + escape(title) + "</title><style>body{font:16px system-ui;max-width:1400px;margin:40px auto;padding:0 20px;color:#182235}table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:10px;text-align:left;border-bottom:1px solid #ddd}.scroll{overflow:auto}p{line-height:1.6}</style>"
    html += "<h1>" + escape(title) + "</h1><p>Accepted fixes: <strong>" + str(run["counts"]["pass"]) + "/" + str(run["task_count"]) + "</strong></p>"
    html += "<p>" + escape(json.dumps(run["counts"])) + "</p><div class=scroll><table><thead><tr>" + "".join("<th>" + escape(k.replace("_", " ")) + "</th>" for k in FIELDS) + "</tr></thead><tbody>" + table + "</tbody></table></div><ul>"
    html += "".join("<li>" + escape(note) + "</li>" for note in run["metric_notes"]) + "</ul></html>"
    (output / "report.html").write_text(html, encoding='utf-8')
    suite = ET.Element("testsuite", name=xml_text(run["run_id"]), tests=str(run["task_count"]), failures=str(run["counts"]["fail"]), errors=str(run["counts"]["timeout"] + run["counts"]["infrastructure_error"]), skipped=str(run["counts"]["not_run"]))
    for row in run["tasks"]:
        case = ET.SubElement(suite, "testcase", name=xml_text(row["task"]), classname=xml_text(row["language"]), time=str(row["task_seconds"] or 0))
        kind = {"fail": "failure", "timeout": "error", "infrastructure_error": "error", "not_run": "skipped"}.get(row["status"])
        if kind:
            ET.SubElement(case, kind, message=row["status"], type=row["status"])
    ET.ElementTree(suite).write(output / "junit.xml", encoding="utf-8", xml_declaration=True)
    return output


def compare_runs(runs, output):
    if len(runs) < 2:
        raise ValueError("Comparison requires at least two runs")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    same_suite = len({(r["suite_id"], r["suite_manifest_sha256"]) for r in runs}) == 1 and all(r["suite_manifest_sha256"] for r in runs)
    same_config = all(all(value is not None for value in r["configuration"].values()) for r in runs) and all(r["configuration"] == runs[0]["configuration"] for r in runs[1:])
    data = {"schema_version": SCHEMA_VERSION, "same_suite": same_suite, "same_configuration": same_config,
            "comparison_note": "Same recorded harness configuration; not a fairness certification" if same_suite and same_config else "Different or incomplete suite/configuration metadata: descriptive comparison only",
            "runs": runs}
    (output / "comparison.json").write_text(json.dumps(data, indent=2, allow_nan=False) + "\n", encoding='utf-8')
    with (output / "comparison.csv").open("w", newline="", encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(["run", "model", "accepted", "tasks", "fail", "timeout", "infrastructure_error", "not_run", "batch_seconds"])
        for run in runs:
            writer.writerow(safe_csv(v) for v in [run["run_id"], run["model"], run["counts"]["pass"], run["task_count"], run["counts"]["fail"], run["counts"]["timeout"], run["counts"]["infrastructure_error"], run["counts"]["not_run"], run["batch_wall_seconds"]])
    lines = ["# Coding Benchmark comparison", "", data["comparison_note"], "", "| Model | Run | Accepted | Fail | Timeout | Infrastructure | Not run |", "|---|---|---:|---:|---:|---:|---:|"]
    for run in runs:
        values = [run["model"], run["run_id"], f"{run['counts']['pass']}/{run['task_count']}", *[run["counts"][k] for k in ("fail", "timeout", "infrastructure_error", "not_run")]]
        lines.append("| " + " | ".join(markdown_cell(v) for v in values) + " |")
    (output / "comparison.md").write_text("\n".join(lines) + "\n", encoding='utf-8')
    return data
