"""No network, model calls or container work unless run is explicitly selected."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

from . import __version__
from .reports import compare_runs, load_run, write_reports
from .assets.scripts.report_stream import ReportStream
from .assets.scripts.prerequisites import inspect_environment
from .assets.scripts.runtime import install_pi, PI_VERSION
from .assets.scripts.configuration import validate_endpoint
from .assets.scripts.process_control import group_options, stop_worker
from .assets.scripts.server_http import check_server
from .assets.scripts.configuration import read_config

ASSETS = Path(__file__).parent / "assets"
CONTROLLER = ASSETS / "scripts/pi-coding-suite-bench.py"
SWEEP = ASSETS / "scripts/agent-sweep.py"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="coding-benchmark")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="Verify the frozen 15-task fixtures, offline")
    doctor = commands.add_parser("doctor", help="Check prerequisites without installs or model calls")
    doctor.add_argument("--output-root", type=Path, default=Path.cwd() / "benchmark-results")
    doctor.add_argument("--python", default=None)
    doctor.add_argument("--format", choices=["text", "ndjson"], default="text")
    doctor.add_argument("--check-server", action="store_true", help="Read /models metadata only; never sends a generation request")
    doctor.add_argument("--config", type=Path)
    doctor.add_argument("--base-url")
    doctor.add_argument("--endpoint-model")
    setup = commands.add_parser("setup", help="Install the locked user-local Pi runtime; no global/system changes")
    setup.add_argument("--install-pi", action="store_true", help=f"Explicitly download/install locked Pi {PI_VERSION} and dependencies")
    configure = commands.add_parser("configure", help="Save non-secret server settings in a new config file")
    configure.add_argument("--base-url", required=True)
    configure.add_argument("--model", required=True)
    configure.add_argument("--agents", type=int, default=3)
    configure.add_argument("--output", type=Path, default=Path("benchmark.json"))
    run = commands.add_parser("run", help="Run through Pi against an OpenAI-compatible endpoint", add_help=False)
    run.add_argument("arguments", nargs=argparse.REMAINDER)
    sweep = commands.add_parser("sweep", help="Quick doubling-concurrency capacity sweep through Pi", add_help=False)
    sweep.add_argument("arguments", nargs=argparse.REMAINDER)
    report = commands.add_parser("report", help="Stream a retained run report to stdout or a new file")
    report.add_argument("run_directory", type=Path)
    report.add_argument("--output", default="-", help="- for stdout, or a new report file")
    report.add_argument("--format", choices=["text", "ndjson"], default="text")
    export = commands.add_parser("export", help="Generate optional JSON, CSV, Markdown, HTML and JUnit files")
    export.add_argument("run_directory", type=Path)
    export.add_argument("--output", type=Path, required=True, help="New export directory; never overwritten")
    compare = commands.add_parser("compare", help="Compare retained runs offline")
    compare.add_argument("run_directories", nargs="+", type=Path)
    compare.add_argument("--output", type=Path, required=True)
    # parse_known_args lets the existing controller own run flags and --help.
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] in {"run", "sweep"}:
        arguments = list(argv[1:])
        try:
            child = subprocess.Popen([sys.executable, str(CONTROLLER if argv[0] == "run" else SWEEP), *arguments], **group_options())
            try:
                return child.wait()
            except KeyboardInterrupt:
                stop_worker(child)
                return 130
        except OSError as exc:
            parser.exit(2, f"Cannot start benchmark controller: {exc}\n")
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            result = inspect_environment(args.output_root, args.python)
            if args.check_server:
                config = read_config(args.config) if args.config else {}
                server = check_server(validate_endpoint(args.base_url or config.get("base_url", "http://127.0.0.1:8000/v1")), args.endpoint_model or config.get("endpoint_model"))
                result["checks"].append({"name": "Server", "status": server["status"], "message": server["message"]})
                result["ready"] = result["ready"] and server["status"] != "error"
            sink = ReportStream(format=args.format)
            for check in result["checks"]:
                sink.emit("prerequisite", f"{check['status'].upper()} {check['name']}: {check['message']}", name=check["name"], status=check["status"])
            sink.emit("doctor_complete", "Ready for task preflight" if result["ready"] else "Fix the errors above before running the benchmark", ready=result["ready"])
            return 0 if result["ready"] else 2
        if args.command == "setup":
            if not args.install_pi:
                print("Install the pinned runtime with: coding-benchmark setup --install-pi")
                return 0
            print(f"Installing locked Pi {PI_VERSION} into your user-local benchmark tools directory…", flush=True)
            result = install_pi()
            print(f"Pi {result['version']} installed: {result['directory']}\nNext: coding-benchmark doctor")
            return 0
        if args.command == "configure":
            endpoint = validate_endpoint(args.base_url)
            if args.agents < 1:
                raise ValueError("--agents must be positive")
            with args.output.expanduser().open("x", encoding="utf-8") as stream:
                json.dump({"base_url": endpoint, "endpoint_model": args.model, "model_label": args.model, "concurrency": args.agents}, stream, indent=2)
                stream.write("\n")
            print(f"Saved {args.output}. API keys are not stored. Run: coding-benchmark run --config {args.output}")
            return 0
        if args.command == "plan":
            return subprocess.call([sys.executable, str(CONTROLLER), "--plan-only"])
        if args.command == "report":
            data = load_run(args.run_directory)
            sink = ReportStream(args.output, args.format)
            try:
                sink.emit("run", f"{data['model']}: {data['run_id']}", run_id=data["run_id"], model=data["model"])
                for row in data["tasks"]:
                    sink.emit("task_result", f"{row['task']}: {row['status']}; task {row['task_seconds']}s; tokens {row['total_tokens']}", **row)
                sink.emit("complete", f"Accepted {data['counts']['pass']}/{data['task_count']}; {data['counts']}", counts=data["counts"], batch_wall_seconds=data["batch_wall_seconds"])
            finally:
                sink.close()
            return 0
        if args.command == "export":
            data = load_run(args.run_directory)
            write_reports(data, args.output)
            print(json.dumps({"reports": str(args.output), "counts": data["counts"]}))
            return 0
        if args.command == "compare":
            compare_runs([load_run(path) for path in args.run_directories], args.output)
            print(args.output)
            return 0
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        parser.exit(2, f"coding-benchmark: {exc}\n")
    return 2
