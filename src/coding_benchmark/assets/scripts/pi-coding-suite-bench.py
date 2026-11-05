#!/usr/bin/env python3
"""Run the frozen 15-task suite with a configurable simultaneous-agent limit."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlsplit
import uuid
import threading
import signal
from prerequisites import inspect_environment
from runtime import pi_command
from configuration import read_config, validate_endpoint
from process_control import group_options, stop_worker
from server_http import check_server
from report_stream import ReportStream

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'benchmarks/pi-coding-suite/suite-v2.json'
TASK_RUNNER = ROOT / 'scripts/swe-agent-bench.py'
RESULTS = Path.cwd() / 'benchmark-results'
WORKERS = 3
REPORT = None
ACTIVE_TASKS = set()
ACTIVE_LOCK = threading.Lock()
CANCELLED = threading.Event()


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def iso_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def command_version(command):
    try:
        result = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=10)
        return result.stdout.strip().splitlines()[0] if result.stdout.strip() else None
    except (OSError, subprocess.SubprocessError):
        return None


def git_revision():
    result = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return result.stdout.strip() if result.returncode == 0 else None


def load_and_validate_manifest(path):
    manifest = json.loads(Path(path).read_text(encoding='utf-8'))
    if manifest.get('suite_id') != 'pi-coding-suite-v2' or manifest.get('task_count') != 15:
        raise ValueError('manifest must define the frozen pi-coding-suite-v2 set of 15 tasks')
    if manifest.get('default_concurrency') != 3:
        raise ValueError('the frozen suite default concurrency must remain three; use --agents to change the run limit')
    tasks = manifest.get('tasks') or []
    if len(tasks) != 15 or len({task.get('instance_id') for task in tasks}) != 15:
        raise ValueError('manifest must contain 15 unique task IDs')
    expected_counts = {'Python': 6, 'TypeScript': 6, 'C++': 2, 'Rust': 1}
    counts = {language: sum(task.get('language') == language for task in tasks)
              for language in expected_counts}
    if counts != expected_counts:
        raise ValueError(f'unexpected language mix: {counts}')
    for entry in tasks:
        task_path = (ROOT / entry['task_file']).resolve()
        if not task_path.is_relative_to(ROOT.resolve()) or not task_path.is_file():
            raise ValueError(f"task fixture is missing or outside the project: {entry['task_file']}")
        if sha256(task_path) != entry['task_json_sha256']:
            raise ValueError(f"task fixture hash changed: {entry['instance_id']}")
        data = json.loads(task_path.read_text(encoding='utf-8'))
        if (data.get('instance_id') != entry['instance_id'] or data.get('repo') != entry['repo']
                or data.get('base_commit') != entry['base_commit']):
            raise ValueError(f"task metadata does not match manifest: {entry['instance_id']}")
        if hashlib.sha256((data.get('patch') or '').encode()).hexdigest() != entry['gold_patch_sha256']:
            raise ValueError(f"reference patch hash changed: {entry['instance_id']}")
        if hashlib.sha256((data.get('test_patch') or '').encode()).hexdigest() != entry['test_patch_sha256']:
            raise ValueError(f"acceptance test hash changed: {entry['instance_id']}")
        exclusions = entry.get('evaluator', {}).get('excluded_host_tests', [])
        if exclusions != data.get('benchmark_host_exclusions', []):
            raise ValueError(f"task host exclusions do not match the suite manifest: {entry['instance_id']}")
        p2p = data.get('PASS_TO_PASS') or []
        if isinstance(p2p, str):
            p2p = json.loads(p2p)
        f2p = data.get('FAIL_TO_PASS') or []
        if isinstance(f2p, str):
            f2p = json.loads(f2p)
        if any(item.get('test') in f2p or item.get('test') not in p2p for item in exclusions):
            raise ValueError(f"host exclusions must name only existing pass-to-pass checks: {entry['instance_id']}")
        if entry['language'] != 'Python':
            image = entry.get('evaluator', {}).get('image')
            if not image or '@sha256:' not in image or data.get('_benchmark_image') != image:
                raise ValueError(f"task lacks its matching immutable evaluator image: {entry['instance_id']}")
    return manifest, counts


def endpoint_request(url):
    key = os.environ.get('CODING_BENCHMARK_API_KEY')
    return urllib.request.Request(url, headers={'Authorization': 'Bearer ' + key} if key else {})


def check_endpoint(base_url, requested_model):
    if requested_model:
        return requested_model
    url = base_url.rstrip('/') + '/models'
    with urllib.request.urlopen(endpoint_request(url), timeout=15) as response:
        info = json.loads(response.read())
    ids = [model['id'] for model in info.get('data', [])]
    if len(ids) != 1:
        raise RuntimeError(f'endpoint must serve exactly one model or pass --endpoint-model; available: {ids}')
    return requested_model or ids[0]


def docker_preflight(images, pull_images=False):
    info = subprocess.run(['docker', 'info', '--format', '{{.ServerVersion}}'],
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
    if info.returncode:
        raise RuntimeError(f'Docker engine is unavailable: {info.stdout.strip()}')
    missing = []
    for image in images:
        probe = subprocess.run(['docker', 'image', 'inspect', image],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        if probe.returncode:
            if pull_images:
                pulled = subprocess.run(['docker', 'pull', '--platform', 'linux/amd64', image],
                                        text=True, encoding='utf-8', errors='replace', stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
                if pulled.returncode:
                    raise RuntimeError(f'could not pull pinned image {image}: {pulled.stdout[-3000:]}')
            else:
                missing.append(image)
    if missing:
        raise RuntimeError('pinned task images are not cached; rerun with --pull-images: ' + ', '.join(missing))
    return info.stdout.strip()


def task_command(args, entry, output, validate_only=False, validation_record=None):
    command = [
        sys.executable, str(TASK_RUNNER), '--task', str((ROOT / entry['task_file']).resolve()),
        '--base-url', args.base_url, '--model-label', args.model_label,
        '--thinking', args.thinking, '--agent-timeout', str(args.agent_timeout),
        '--test-timeout', str(args.test_timeout), '--python', args.python,
        '--output', str(output), '--cleanup-workdirs',
    ]
    if args.endpoint_model:
        command += ['--endpoint-model', args.endpoint_model]
    if validate_only:
        command.append('--validate-only')
    if validation_record:
        command += ['--validated-result', str(validation_record)]
    return command


def execute_task(args, entry, output, validate_only=False, validation_record=None):
    started = time.monotonic()
    if CANCELLED.is_set():
        return {'instance_id': entry['instance_id'], 'infra_error': 'Run interrupted before task start'}
    proc = subprocess.Popen(task_command(args, entry, output, validate_only, validation_record),
                            cwd=ROOT, text=True, encoding='utf-8', errors='replace', stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **group_options())
    with ACTIVE_LOCK:
        ACTIVE_TASKS.add(proc)
    try:
        if CANCELLED.is_set():
            stop_worker(proc)
        stdout, _ = proc.communicate()
    finally:
        with ACTIVE_LOCK:
            ACTIVE_TASKS.discard(proc)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'runner.stdout.txt').write_text(stdout, encoding='utf-8')
    result_path = output / 'result.json'
    if not result_path.is_file():
        return {'instance_id': entry['instance_id'], 'infra_error':
                f'runner exited {proc.returncode} without result.json',
                'controller_wall_seconds': round(time.monotonic() - started, 3)}
    result = json.loads(result_path.read_text(encoding='utf-8'))
    result['controller_wall_seconds'] = round(time.monotonic() - started, 3)
    return result


def parallel_stage(args, tasks, output_root, stage, validate_only=False, validation_records=None):
    rows = {}
    completed = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {}
        for entry in tasks:
            out = output_root / stage / entry['instance_id']
            record = (validation_records or {}).get(entry['instance_id'])
            future = pool.submit(execute_task, args, entry, out, validate_only, record)
            futures[future] = entry
        try:
            for future in as_completed(futures):
                entry = futures[future]
                try:
                    rows[entry['instance_id']] = future.result()
                except Exception as exc:
                    rows[entry['instance_id']] = {'instance_id': entry['instance_id'],
                                                   'infra_error': f'{type(exc).__name__}: {exc}'}
                completed += 1
                result = rows[entry['instance_id']]
                if stage == 'preflight':
                    state = 'validated' if result.get('validated') else 'blocked'
                else:
                    state = status_for(result)
                REPORT.emit('task_finished', f'{stage} {completed}/{len(tasks)} {entry["instance_id"]}: {state}',
                            stage=stage, completed=completed, total=len(tasks), task=entry['instance_id'], status=state,
                            task_seconds=result.get('task_wall_seconds'), agent_seconds=(result.get('agent') or {}).get('wall_seconds'),
                            usage=(result.get('agent') or {}).get('usage'))
        except BaseException:
            CANCELLED.set()
            with ACTIVE_LOCK:
                children = list(ACTIVE_TASKS)
            for child in children:
                stop_worker(child)
            for future in futures:
                future.cancel()
            raise
    return rows


def status_for(result):
    if result.get('infra_error') or not result.get('score'):
        return 'infrastructure_error'
    if (result.get('agent') or {}).get('timed_out'):
        return 'timeout'
    return 'pass' if result['score'].get('passed') else 'fail'


def calculate_peak_agents(results):
    events = []
    for result in results:
        agent = result.get('agent') or {}
        try:
            start = dt.datetime.fromisoformat(agent['started_at']).timestamp()
            end = dt.datetime.fromisoformat(agent['ended_at']).timestamp()
        except (KeyError, TypeError, ValueError):
            continue
        events.extend([(start, 1), (end, -1)])
    active = peak = 0
    for _, change in sorted(events, key=lambda item: (item[0], item[1])):
        active += change
        peak = max(peak, active)
    return peak


def summarize(manifest, results, run_id, model, endpoint_model, thinking,
              preflight_seconds, batch_started_at, batch_started_mono, manifest_path=MANIFEST):
    ended_at = iso_now()
    wall = time.monotonic() - batch_started_mono
    statuses = {ident: status_for(result) for ident, result in results.items()}
    usages = [result.get('agent', {}).get('usage', {}) for result in results.values()]
    keys = ('input', 'output', 'cacheRead', 'cacheWrite', 'reasoning', 'totalTokens')
    counts = {key: sum(usage.get(key) is not None for usage in usages) for key in keys}
    totals = {key: sum(float(usage.get(key) or 0) for usage in usages) if counts[key] else None for key in keys}
    passes = sum(status == 'pass' for status in statuses.values())
    finished = sum(status in {'pass', 'fail'} for status in statuses.values())
    request_rows = [request for result in results.values()
                    for request in (result.get('agent') or {}).get('requests', [])]
    prefill_numer = sum(float(request.get('usage', {}).get('input') or 0) for request in request_rows
                        if request.get('client_estimated_prefill_tokens_per_second'))
    prefill_time = sum(float(request.get('time_to_first_generated_content_seconds') or 0)
                       for request in request_rows
                       if request.get('client_estimated_prefill_tokens_per_second'))
    decode_numer = sum(float(request.get('usage', {}).get('output') or 0) for request in request_rows
                       if request.get('client_estimated_decode_tokens_per_second'))
    decode_time = sum(float(request.get('generation_seconds') or 0) for request in request_rows
                      if request.get('client_estimated_decode_tokens_per_second'))
    return {
        'run_id': run_id, 'status': 'complete' if len(results) == manifest['task_count'] else 'incomplete',
        'suite_id': manifest['suite_id'], 'suite_version': manifest['suite_version'],
        'suite_manifest_sha256': sha256(manifest_path), 'model_label': model,
        'endpoint_model': endpoint_model, 'thinking': thinking, 'configured_concurrency': WORKERS,
        'peak_active_agents': calculate_peak_agents(list(results.values())),
        'preflight_wall_seconds': round(preflight_seconds, 3),
        'batch_started_at': batch_started_at, 'batch_ended_at': ended_at,
        'batch_wall_seconds': round(wall, 3), 'whole_run_wall_seconds': round(preflight_seconds + wall, 3),
        'task_count': manifest['task_count'], 'completed_evaluations': finished,
        'accepted_fixes': passes, 'scores': statuses, 'token_totals': totals, 'token_measured_tasks': counts,
        'aggregate_client_estimated_prefill_tokens_per_second':
            round(prefill_numer / prefill_time, 2) if prefill_time else None,
        'aggregate_client_estimated_decode_tokens_per_second':
            round(decode_numer / decode_time, 2) if decode_time else None,
        'attempted_tasks_per_hour': round(finished * 3600 / wall, 3) if wall else None,
        'accepted_fixes_per_hour': round(passes * 3600 / wall, 3) if wall else None,
    }


def write_report(path, summary, results, manifest=None):
    manifest = manifest or json.loads(MANIFEST.read_text(encoding="utf-8"))
    token = summary['token_totals']
    lines = [
        f"# Pi Coding Suite run {summary['run_id']}", '',
        f"Status: **{summary['status']}**. Model: `{summary['model_label']}` / `{summary.get('endpoint_model')}`; effort: `{summary['thinking']}`.",
        f"Tasks: {summary['completed_evaluations']}/{summary['task_count']}; accepted fixes: {summary['accepted_fixes']}; active concurrency: {summary['peak_active_agents']}/{WORKERS}.",
        f"Whole task batch: {summary['batch_wall_seconds']:.1f}s; setup and validation: {summary['preflight_wall_seconds']:.1f}s; total run: {summary['whole_run_wall_seconds']:.1f}s.",
        f"Tokens (measured tasks only; null means unavailable): {json.dumps(token)}.",
        f"Client-estimated prefill: {summary['aggregate_client_estimated_prefill_tokens_per_second']} tok/s; decode: {summary['aggregate_client_estimated_decode_tokens_per_second']} tok/s.", '',
        '| Task | Language | Result | Input / output tokens | Agent seconds | Task seconds | Prefill tok/s | Decode tok/s |',
        '|---|---|---:|---:|---:|---:|---:|---:|',
    ]
    for ident, result in results.items():
        manifest_task = next(item for item in manifest['tasks'] if item['instance_id'] == ident)
        usage = (result.get('agent') or {}).get('usage') or {}
        requests = (result.get('agent') or {}).get('requests') or []
        prefill = [r['client_estimated_prefill_tokens_per_second'] for r in requests
                   if r.get('client_estimated_prefill_tokens_per_second')]
        decode = [r['client_estimated_decode_tokens_per_second'] for r in requests
                  if r.get('client_estimated_decode_tokens_per_second')]
        agent_wall = (result.get('agent') or {}).get('wall_seconds')
        task_wall = result.get('task_wall_seconds')
        lines.append(f"| `{ident}` | {manifest_task['language']} | {status_for(result)} | {usage.get('input',0)} / {usage.get('output',0)} | {agent_wall if agent_wall is not None else '—'} | {task_wall if task_wall is not None else '—'} | {round(sum(prefill)/len(prefill),2) if prefill else '—'} | {round(sum(decode)/len(decode),2) if decode else '—'} |")
    lines.extend(['', 'Each task transcript, patch, validation record, and `result.json` is stored beside this report.',
                  'Rates are client-observed estimates from Pi stream event arrival times, not engine-internal timings.',
                  f"Manifest SHA-256: `{summary['suite_manifest_sha256']}`.", ''])
    path.write_text('\n'.join(lines), encoding='utf-8')



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8000/v1')
    parser.add_argument('--endpoint-model')
    parser.add_argument('--model-label', default='unnamed-model')
    parser.add_argument('--engine-version', default=None)
    parser.add_argument('--engine-revision', default=None)
    parser.add_argument('--thinking', default='off', choices=['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'])
    parser.add_argument('--agent-timeout', type=int, default=1800)
    parser.add_argument('--test-timeout', type=int, default=900)
    parser.add_argument('--python', default=None, help='Python 3.11 evaluator executable; auto-detected if omitted')
    parser.add_argument('--manifest', type=Path, default=MANIFEST)
    parser.add_argument('--pull-images', action='store_true', help='pull missing pinned SWE-bench images')
    parser.add_argument('--preflight-only', action='store_true',
                        help='validate Docker, model endpoint, and all task baselines/gold patches without model requests')
    parser.add_argument('--plan-only', action='store_true', help='validate and print the frozen 15-task plan')
    parser.add_argument('--output-root', type=Path, default=RESULTS)
    parser.add_argument('--output', default='-', help='Live report: - for stdout, or a new file path')
    parser.add_argument('--format', choices=['text', 'ndjson'], default='text')
    parser.add_argument('--agents', '--concurrency', dest='concurrency', type=int, default=3,
                        help='maximum simultaneous agent tasks (default: 3); refill each freed slot until all tasks finish')
    parser.add_argument('--context-window', type=int, default=131072)
    parser.add_argument('--max-tokens', type=int, default=65536)
    parser.add_argument('--config', type=Path, help='Non-secret config saved by coding-benchmark configure')
    pre, _ = parser.parse_known_args()
    if pre.config:
        try:
            parser.set_defaults(**read_config(pre.config))
        except ValueError as exc:
            parser.error(str(exc))
    args = parser.parse_args()
    try:
        args.base_url = validate_endpoint(args.base_url)
    except ValueError as exc:
        parser.error(str(exc))

    if args.thinking not in {'off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'}:
        parser.error('Invalid thinking level in configuration')
    if args.max_tokens > args.context_window:
        parser.error('--max-tokens must not exceed --context-window')
    if min(args.concurrency, args.context_window, args.max_tokens, args.agent_timeout, args.test_timeout) < 1:
        parser.error('concurrency, limits and timeouts must be positive')
    global WORKERS, REPORT
    CANCELLED.clear()
    WORKERS = args.concurrency
    REPORT = ReportStream(args.output, args.format)
    def interrupted(signum, frame):
        CANCELLED.set()
        with ACTIVE_LOCK:
            children = list(ACTIVE_TASKS)
        for child in children:
            stop_worker(child)
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, interrupted)
    os.environ['CODING_BENCHMARK_CONTEXT_WINDOW'] = str(args.context_window)
    os.environ['CODING_BENCHMARK_MAX_TOKENS'] = str(args.max_tokens)
    os.environ['CODING_BENCHMARK_REASONING'] = '0' if args.thinking == 'off' else '1'

    manifest, counts = load_and_validate_manifest(args.manifest)
    REPORT.emit('plan', f"{manifest['suite_id']}: {len(manifest['tasks'])} tasks, concurrency {WORKERS}",
                suite_id=manifest['suite_id'], suite_version=manifest['suite_version'], manifest_sha256=sha256(args.manifest),
                tasks=len(manifest['tasks']), languages=counts, concurrency=WORKERS,
                task_ids=[task['instance_id'] for task in manifest['tasks']])
    if args.plan_only:
        return 0

    diagnostics = inspect_environment(args.output_root, args.python, need_pi=not args.preflight_only)
    for check in diagnostics['checks']:
        REPORT.emit('prerequisite', f"{check['status'].upper()} {check['name']}: {check['message']}", name=check['name'], status=check['status'])
    if not diagnostics['ready']:
        REPORT.emit('blocked', 'Prerequisites are not ready. Run coding-benchmark doctor and fix the errors above; no tasks or model requests started.')
        return 2
    args.python = diagnostics['python']

    non_python_images = sorted({entry['evaluator']['image'] for entry in manifest['tasks']
                                  if entry['language'] != 'Python'})
    try:
        docker_version = docker_preflight(non_python_images, args.pull_images)
        endpoint_model = args.endpoint_model
        if not args.preflight_only:
            server = check_server(args.base_url, args.endpoint_model)
            REPORT.emit('prerequisite', f"{server['status'].upper()} Server: {server['message']}", name='Server', status=server['status'])
            if server['status'] == 'error':
                return 2
            endpoint_model = server['model']
    except Exception as exc:
        REPORT.emit('error', f'Preflight failed before any model request: {type(exc).__name__}: {exc}')
        return 2

    stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
    run_id = f"{stamp}-{re.sub(r'[^a-zA-Z0-9._-]+', '-', args.model_label).strip('-')}-{uuid.uuid4().hex[:8]}"
    run_dir = args.output_root.expanduser().resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / 'suite-manifest.json').write_bytes(Path(args.manifest).read_bytes())
    metadata = {
        'run_id': run_id, 'started_at': iso_now(), 'model_label': args.model_label,
        'engine_version': args.engine_version, 'engine_source_revision': args.engine_revision,
        'endpoint_model': endpoint_model, 'base_url': args.base_url, 'thinking': args.thinking,
        'concurrency': WORKERS, 'docker_server_version': docker_version,
        'suite_manifest_sha256': sha256(args.manifest), 'preflight_status': 'validating tasks',
        'task_order': [task['instance_id'] for task in manifest['tasks']],
        'pi_version': None if args.preflight_only else command_version(pi_command() + ['--version']),
        'host': diagnostics['host'],
        'evaluator_python_version': command_version([args.python, '--version']),
        'pi_dependency_lock_sha256': sha256(ROOT / 'pi-runtime/package-lock.json'),
        'python_dependency_lock_sha256': sha256(ROOT / 'python-runtime/sources.json'),
        'node_version': None if args.preflight_only else command_version([pi_command()[0], '--version']),
        'project_git_revision': git_revision(),
        'suite_controller_sha256': sha256(Path(__file__)),
        'task_runner_sha256': sha256(TASK_RUNNER),
        'prompt_policy': ('Use the issue statement and hints; make the smallest correct production-code '
                          'change, run relevant existing tests, do not modify tests or commit.'),
        'sampling_policy': 'Pi defaults (no temperature, top-p, or seed overrides).',
        'context_window': args.context_window, 'max_completion_tokens': args.max_tokens,
        'agent_timeout_seconds': args.agent_timeout, 'test_timeout_seconds': args.test_timeout,
        'completion_policy': f'{args.thinking} thinking; per-task timeout and token usage are recorded.',
    }
    (run_dir / 'run-metadata.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
    preflight_started = time.monotonic()
    preflight_rows = parallel_stage(args, manifest['tasks'], run_dir, 'preflight', validate_only=True)
    preflight_seconds = time.monotonic() - preflight_started
    invalid = [ident for ident, row in preflight_rows.items() if not row.get('validated') or row.get('infra_error')]
    metadata['preflight_status'] = 'failed' if invalid else 'passed'
    metadata['preflight_wall_seconds'] = round(preflight_seconds, 3)
    metadata['invalid_tasks'] = invalid
    (run_dir / 'run-metadata.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
    if invalid:
        (run_dir / 'suite-result.json').write_text(json.dumps({
            'run_id': run_id, 'status': 'blocked_preflight', 'invalid_tasks': invalid,
            'preflight_wall_seconds': round(preflight_seconds, 3),
            'task_preflight': {ident: {'validated': row.get('validated'), 'infra_error': row.get('infra_error')}
                               for ident, row in preflight_rows.items()},
        }, indent=2) + '\n', encoding='utf-8')
        REPORT.emit('blocked', f'All model requests withheld: task preflight failed for {invalid}. Records: {run_dir}', invalid_tasks=invalid, run_directory=str(run_dir))
        return 2

    if args.preflight_only:
        (run_dir / 'suite-result.json').write_text(json.dumps({
            'run_id': run_id, 'status': 'preflight_passed_no_model_requests',
            'suite_id': manifest['suite_id'], 'suite_version': manifest['suite_version'],
            'suite_manifest_sha256': sha256(args.manifest), 'model_label': args.model_label,
            'endpoint_model': endpoint_model, 'engine_version': args.engine_version,
            'engine_source_revision': args.engine_revision, 'docker_server_version': docker_version,
            'preflight_wall_seconds': round(preflight_seconds, 3),
            'validated_tasks': [task['instance_id'] for task in manifest['tasks']],
            'model_requests_sent': 0,
        }, indent=2) + '\n', encoding='utf-8')
        metadata['preflight_status'] = 'passed'
        metadata['preflight_only'] = True
        metadata['preflight_wall_seconds'] = round(preflight_seconds, 3)
        metadata['ended_at'] = iso_now()
        (run_dir / 'run-metadata.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
        REPORT.emit('preflight_complete', f"All {len(manifest['tasks'])} baselines and reference patches passed; no model requests sent. Records: {run_dir}", model_requests_sent=0, run_directory=str(run_dir))
        return 0

    validation_records = {ident: run_dir / 'preflight' / ident / 'result.json'
                          for ident in preflight_rows}
    batch_started_at = iso_now()
    batch_started_mono = time.monotonic()
    results = parallel_stage(args, manifest['tasks'], run_dir, 'tasks',
                             validation_records=validation_records)
    summary = summarize(manifest, results, run_id, args.model_label, endpoint_model,
                        args.thinking, preflight_seconds, batch_started_at, batch_started_mono, args.manifest)
    summary['docker_server_version'] = docker_version
    summary['batch_statuses'] = {ident: status_for(result) for ident, result in results.items()}
    (run_dir / 'suite-result.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    metadata['ended_at'] = iso_now()
    metadata['batch_started_at'] = batch_started_at
    metadata['batch_ended_at'] = summary['batch_ended_at']
    metadata['batch_wall_seconds'] = summary['batch_wall_seconds']
    (run_dir / 'run-metadata.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
    write_report(run_dir / 'report.md', summary, results, manifest)
    for entry in manifest['tasks']:
        ident=entry['instance_id']; result=results[ident]
        REPORT.emit('task_result', f"{ident}: {status_for(result)}; task {result.get('task_wall_seconds')}s", task=ident, language=entry['language'], status=status_for(result), task_seconds=result.get('task_wall_seconds'), usage=(result.get('agent') or {}).get('usage'))
    REPORT.emit('complete', f"Accepted {summary['accepted_fixes']}/{manifest['task_count']}; evaluated {summary['completed_evaluations']}; batch {summary['batch_wall_seconds']}s; tokens {summary['token_totals']['totalTokens']}. Artifacts: {run_dir}", summary=summary, run_directory=str(run_dir))
    return 0 if summary['status'] == 'complete' and summary['completed_evaluations'] == 15 else 1


if __name__ == '__main__':
    try:
        code = main()
    except (KeyboardInterrupt, BrokenPipeError):
        if REPORT and not sys.stdout.closed:
            try:
                REPORT.emit('interrupted', 'Run interrupted; own workers stopped. Retained artifacts are incomplete.')
            except BrokenPipeError:
                pass
        code = 130
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        if REPORT:
            REPORT.emit('error', f'Cannot continue: {type(exc).__name__}: {exc}')
        else:
            print(f'coding-benchmark: {exc}', file=sys.stderr)
        code = 2
    finally:
        if REPORT:
            REPORT.close()
    raise SystemExit(code)
