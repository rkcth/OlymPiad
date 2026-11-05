#!/usr/bin/env python3
"""Run a real Pi coding loop against one hidden-test SWE-bench task."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import uuid
from process_control import group_options, kill_tree
from runtime import pi_command, bash_command, python_lock_files
from server_http import read_models
import time
import urllib.request
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / 'benchmarks/swe-agent/tasks'
RESULTS = ROOT / 'results/swe-agent'
DEADLINE_EPOCH = None
ACTIVE_PI = None
ACTIVE_COMMAND = None


def bounded_timeout(timeout):
    if DEADLINE_EPOCH is None:
        return timeout
    remaining = DEADLINE_EPOCH - time.time()
    if remaining <= 0:
        raise TimeoutError('benchmark phase deadline reached')
    return max(1, min(timeout, remaining))


def stop_active_pi(signum, _frame):
    for process in (ACTIVE_PI, ACTIVE_COMMAND):
        if process is not None:
            kill_tree(process)
    raise SystemExit(128 + signum)


def run(cmd, cwd=None, timeout=900, input_text=None, env=None):
    global ACTIVE_COMMAND
    started = time.monotonic()
    process = subprocess.Popen(cmd, cwd=cwd, env=env, text=True, encoding='utf-8', errors='replace',
                               stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **group_options())
    ACTIVE_COMMAND = process
    try:
        output, _ = process.communicate(input_text, timeout=bounded_timeout(timeout))
    except BaseException:
        kill_tree(process)
        process.communicate()
        raise
    finally:
        ACTIVE_COMMAND = None
    return {'command': cmd, 'returncode': process.returncode,
            'wall_seconds': round(time.monotonic() - started, 3), 'output': output}


def must(result):
    if result['returncode']:
        raise RuntimeError(f"command failed: {result['command']}\n{result['output'][-4000:]}")
    return result


def list_field(task, name):
    value = task.get(name) or []
    if isinstance(value, str):
        value = json.loads(value)
    # Real SWE-bench test names are '<method> (<class>)' or '<module>::<test>'.
    # Log-based extraction can capture stray docstring lines as names; drop
    # anything not in either shape so bogus labels never reach the test runner.
    shape = re.compile(r'^(?:.+ \([^)]+\)|.+::.+|[\w.-]+\.[\w.-]+)$')
    return [v for v in value if isinstance(v, str) and shape.match(v)]


def test_modules(task):
    modules = []
    for test in list_field(task, 'FAIL_TO_PASS') + list_field(task, 'PASS_TO_PASS'):
        match = re.search(r'\(([^)]+)\)', test)
        if match:
            bits = match.group(1).split('.')
            modules.append('.'.join(bits[:-1]) if bits[-1][:1].isupper() else match.group(1))
        else:
            modules.append(test.split('::')[0].replace('/', '.').removesuffix('.py'))
    if not modules:
        raise ValueError('task has no acceptance tests')
    return sorted(set(modules))


def parse_test_outcomes(output, expected):
    outcomes = {}
    for test in expected:
        # Django's unittest-style reporter uses THREE line forms:
        #   1. inline:   "test_x (mod.Class) ... ok"            (most tests)
        #   2. header:   "FAIL: test_x (mod.Class)" + traceback (failures/errors)
        #   3. docstring: "test_x (mod.Class)" newline "first-line-of-docstring ... ok"
        # Match 1, then 2, then attribute 3 via the line following the bare name.
        inline = re.search(
            rf'^{re.escape(test)}\s+\.\.\.\s+(ok|FAIL|ERROR|skipped.*|expected failure.*)$',
            output, re.M | re.I,
        )
        header = re.search(
            rf'^(?:FAIL|ERROR): {re.escape(test)}(?:\s|$)',
            output, re.M | re.I,
        )
        bare = re.search(rf'^{re.escape(test)}$', output, re.M | re.I)
        follow = None
        if bare is not None:
            idx = output.index(bare.group(0))
            rest = output[idx + len(bare.group(0)):]
            for line in rest.splitlines():
                if line.startswith('test_') and ' (' in line:
                    break
                candidate = line.strip()
                if candidate in {'ok', 'FAIL', 'ERROR'}:
                    follow = candidate
                    break
                if candidate.startswith('expected failure') or candidate.startswith('skipped'):
                    follow = candidate
                    break
                if candidate.endswith(' ... expected failure'):
                    follow = 'expected failure'
                    break
                if candidate.endswith(' ... skipped') or candidate.endswith(' ... skipped (expected)'):
                    follow = 'skipped'
                    break
                if candidate.endswith(' ... ok'):
                    follow = 'ok'
                    break
                if candidate.endswith(' ... FAIL'):
                    follow = 'FAIL'
                    break
                if candidate.endswith(' ... ERROR'):
                    follow = 'ERROR'
                    break
        if inline:
            outcomes[test] = inline.group(1).lower()
            if outcomes[test].startswith('expected failure'):
                outcomes[test] = 'expected failure'
        elif header:
            outcomes[test] = header.group(0).split(':', 1)[0].lower()
        elif follow:
            outcomes[test] = follow.lower()
            if outcomes[test].startswith('expected failure'):
                outcomes[test] = 'expected failure'
        else:
            outcomes[test] = None
    return outcomes


def baseline_is_valid(task, outcomes):
    failing = list_field(task, 'FAIL_TO_PASS')
    passing = list_field(task, 'PASS_TO_PASS')
    return (
        all(outcomes.get(name) in {'fail', 'error'} for name in failing)
        and all(outcomes.get(name) in {'ok', 'expected failure'} for name in passing)
    )


def all_tests_pass(task, outcomes, returncode):
    failing = list_field(task, 'FAIL_TO_PASS')
    passing = list_field(task, 'PASS_TO_PASS')
    return (
        returncode == 0
        and all(outcomes.get(name) == 'ok' for name in failing)
        and all(outcomes.get(name) in {'ok', 'expected failure'} for name in passing)
    )


def apply_patch(repo, patch):
    if not patch.strip():
        return {'command': ['git', 'apply'], 'returncode': 0, 'wall_seconds': 0, 'output': ''}
    return run(['git', 'apply', '--binary', '--whitespace=nowarn', '-'], cwd=repo, input_text=patch)


def reset_repo(repo, commit):
    must(run(['git', 'reset', '--hard', '-q', commit], cwd=repo))
    must(run(['git', 'clean', '-fdx', '-q'], cwd=repo))


def clone_task_repo(task, agent_repo, evaluator_repo):
    url = f"https://github.com/{task['repo']}.git"
    must(run(['git', 'init', '-q', str(agent_repo)]))
    must(run(['git', 'config', 'core.autocrlf', 'false'], cwd=agent_repo))
    must(run(['git', 'remote', 'add', 'origin', url], cwd=agent_repo))
    must(run(['git', 'fetch', '-q', '--depth', '1', 'origin', task['base_commit']], cwd=agent_repo, timeout=1200))
    must(run(['git', 'checkout', '-q', '--detach', 'FETCH_HEAD'], cwd=agent_repo))
    must(run(['git', 'clone', '-q', '--no-hardlinks', str(agent_repo), str(evaluator_repo)], timeout=1200))
    must(run(['git', 'config', 'core.autocrlf', 'false'], cwd=evaluator_repo))
    must(run(['git', 'checkout', '-q', '--detach', task['base_commit']], cwd=evaluator_repo))


def make_venv(python, venv, repo):
    must(run([python, '-m', 'venv', str(venv)], timeout=300))
    py = venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    bootstrap, dependencies = python_lock_files()
    for requirement in (bootstrap, dependencies):
        must(run([str(py), '-m', 'pip', '--disable-pip-version-check', 'install',
                  '--only-binary=:all:', '--require-hashes', '-r', str(requirement)], cwd=repo, timeout=1200))
    # The repository is already pinned by the suite. Its editable build must
    # use only the locked environment, never fetch fresh build/dependency tools.
    must(run([str(py), '-m', 'pip', '--disable-pip-version-check', 'install',
              '--no-build-isolation', '--no-deps', '--no-index', '-e', '.'], cwd=repo, timeout=1200))
    must(run([str(py), '-m', 'pip', '--disable-pip-version-check', 'check'], cwd=repo))
    must(run([str(py), '-c', 'import django; print(django.get_version())'], cwd=repo))
    must(run(['git', 'clean', '-fdx', '-q'], cwd=repo))
    return py


def run_non_python_image_tests(task, patch, log_path, timeout):
    image = task.get('_benchmark_image')
    if not image or '@sha256:' not in image:
        raise RuntimeError(f"task {task['instance_id']} has no immutable evaluator image")
    import base64
    encoded_patch = base64.b64encode((patch or '').encode()).decode()
    prelude = [
        # SWE task scripts print test output and then clean up with git checkout;
        # without errexit that cleanup overwrites the test command's exit code.
        # The container is disposable, so stop at the first failed test command.
        'set -euo pipefail',
        'cd /testbed',
        f"printf '%s' '{encoded_patch}' | base64 -d | git apply --binary --whitespace=nowarn -" if patch else ':',
    ]
    if task.get('repo') == 'fmtlib/fmt':
        # The frozen fmt task declares the exact GoogleTest cases that make
        # up its regression check. Run those instead of every unrelated fmt
        # test: one unrelated stress test attempts an invalid multi-petabyte
        # allocation under Docker Desktop's linux/amd64 emulation on Apple
        # Silicon. GTest applies this filter inside the existing pinned image.
        import shlex
        excluded = {item['test'] for item in task.get('benchmark_host_exclusions', [])}
        declared = [test for test in list_field(task, 'FAIL_TO_PASS') + list_field(task, 'PASS_TO_PASS')
                    if test not in excluded]
        eval_script = task['eval_script']
        ctest_command = 'ctest --test-dir build -V -R format-test'
        if ctest_command not in eval_script:
            raise RuntimeError('fmt evaluator changed; refusing to run a different acceptance-test scope')
        gtest_command = './build/bin/format-test --gtest_filter=' + shlex.quote(':'.join(declared))
        eval_script = eval_script.replace(ctest_command, gtest_command, 1)
    else:
        eval_script = task['eval_script']
    command = '\n'.join(prelude) + '\n' + eval_script
    name = 'olympiad-eval-' + uuid.uuid4().hex
    try:
        result = run([
            'docker', 'run', '--rm', '--name', name, '--label', 'olympiad.owned=true',
            '--platform', 'linux/amd64', '--network', 'none',
            '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=2048',
            '-i', '--workdir', '/testbed', image, '/bin/bash', '-s',
        ], timeout=timeout, input_text=command)
    finally:
        # Killing a Docker CLI does not kill its container. This exact random
        # name belongs to this evaluator invocation; never remove other jobs.
        cleanup = subprocess.run(['docker', 'rm', '-f', name], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 encoding='utf-8', errors='replace', timeout=20)
        if cleanup.returncode and 'no such container' not in cleanup.stdout.lower():
            raise RuntimeError(f'Could not confirm cleanup of owned container {name}; check Docker and remove that exact container if still present.')
    log_path.write_text(result['output'], encoding='utf-8')
    result['test_passed'] = result['returncode'] == 0
    result['evaluator'] = 'pinned SWE-bench task image; eval_script; network disabled'
    result['owned_container_name'] = name
    result['output'] = result['output'][-12000:]
    return result


def run_tests(task, repo, python, log_path, timeout, patch=None):
    if task['repo'] != 'django/django':
        return run_non_python_image_tests(task, patch, log_path, timeout)
    expected = list_field(task, 'FAIL_TO_PASS') + list_field(task, 'PASS_TO_PASS')
    result = run(
        [str(python), 'tests/runtests.py', *test_modules(task), '-v', '2', '--settings=test_sqlite'],
        cwd=repo, timeout=timeout,
    )
    log_path.write_text(result['output'], encoding='utf-8')
    result['outcomes'] = parse_test_outcomes(result['output'], expected)
    result['output'] = result['output'][-12000:]
    return result


def parse_pi_metrics(stdout, line_elapsed=None):
    usage = {'input': None, 'output': None, 'cacheRead': None, 'cacheWrite': None, 'reasoning': None, 'totalTokens': None}
    tools = 0
    stop = None
    error = None
    requests = []
    active = None
    for line_number, line in enumerate(stdout.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_type = event.get('type')
        message = event.get('message') or {}
        elapsed = (line_elapsed or {}).get(line_number)
        if event_type == 'message_start' and message.get('role') == 'assistant':
            active = {
                'started_elapsed_seconds': elapsed,
                'first_generated_elapsed_seconds': None,
                'ended_elapsed_seconds': None,
                'usage': {},
            }
        elif event_type == 'message_update' and active is not None:
            streamed = event.get('assistantMessageEvent') or {}
            if (streamed.get('type') in {'text_delta', 'thinking_delta', 'toolcall_delta'}
                    and streamed.get('delta') and active['first_generated_elapsed_seconds'] is None):
                active['first_generated_elapsed_seconds'] = elapsed
        if event.get('type') == 'tool_execution_start':
            tools += 1
        if event_type == 'message_end' and message.get('role') == 'assistant':
            for key, value in (message.get('usage') or {}).items():
                if key in usage and isinstance(value, (int, float)):
                    usage[key] = (usage[key] or 0) + value
            if active is None:
                active = {'started_elapsed_seconds': None, 'first_generated_elapsed_seconds': None,
                          'ended_elapsed_seconds': None, 'usage': {}}
            active['ended_elapsed_seconds'] = elapsed
            active['usage'] = {k: v for k, v in (message.get('usage') or {}).items()
                               if isinstance(v, (int, float))}
            started = active.get('started_elapsed_seconds')
            first = active.get('first_generated_elapsed_seconds')
            ended = active.get('ended_elapsed_seconds')
            ttft = first - started if first is not None and started is not None else None
            generation = ended - first if ended is not None and first is not None else None
            input_tokens = active['usage'].get('input')
            output_tokens = active['usage'].get('output')
            active.update({
                'time_to_first_generated_content_seconds': round(ttft, 4) if ttft is not None else None,
                'client_estimated_prefill_tokens_per_second': round(input_tokens / ttft, 2)
                    if input_tokens and ttft and ttft > 0 else None,
                'generation_seconds': round(generation, 4) if generation is not None else None,
                'client_estimated_decode_tokens_per_second': round(output_tokens / generation, 2)
                    if output_tokens and generation and generation > 0 else None,
            })
            requests.append(active)
            active = None
            stop = message.get('stopReason') or stop
            error = message.get('errorMessage')
    return usage, tools, stop, error, requests


def classify_pi_failure(agent, stop, error):
    if agent['timed_out']:
        return None, None
    if agent['returncode']:
        return 'infra', f"Pi exited with status {agent['returncode']}"
    if stop is None:
        return 'infra', 'Pi produced no assistant completion'
    if stop != 'error':
        return None, None
    message = error or 'Pi stopped with an unspecified provider error'
    # A provider/protocol/authentication error is not a failed code fix.
    return 'infra', f'Pi provider error: {message}'


def run_pi(cmd, cwd, env, timeout, input_text=None, process_observer=None):
    global ACTIVE_PI
    effective_timeout = bounded_timeout(timeout)
    started = time.monotonic()
    started_at = dt.datetime.now(dt.timezone.utc).isoformat()
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, text=True, bufsize=1,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        encoding="utf-8", errors="replace", **group_options(),
    )
    ACTIVE_PI = proc
    if process_observer:
        process_observer(proc, True)
    def write_input():
        try:
            proc.stdin.write(input_text)
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
    input_thread = threading.Thread(target=write_input, daemon=True) if input_text is not None else None
    if input_thread:
        input_thread.start()
    stdout_lines, stderr_lines, line_elapsed = [], [], {}
    def drain(stream, lines, capture_times=False):
        for line in iter(stream.readline, ''):
            if capture_times:
                line_elapsed[len(stdout_lines)] = time.monotonic() - started
            lines.append(line)
        stream.close()
    stdout_thread = threading.Thread(target=drain, args=(proc.stdout, stdout_lines, True), daemon=True)
    stderr_thread = threading.Thread(target=drain, args=(proc.stderr, stderr_lines), daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    try:
        proc.wait(timeout=effective_timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        proc.wait()
        timed_out = True
    except BaseException:
        if proc.poll() is None:
            kill_tree(proc)
        proc.wait()
        raise
    finally:
        stdout_thread.join()
        stderr_thread.join()
        if input_thread:
            input_thread.join(timeout=5)
        ACTIVE_PI = None
        if process_observer:
            process_observer(proc, False)
    stdout, stderr = ''.join(stdout_lines), ''.join(stderr_lines)
    return {
        'returncode': proc.returncode,
        'timed_out': timed_out,
        'started_at': started_at,
        'ended_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'wall_seconds': round(time.monotonic() - started, 3),
        'stdout': stdout,
        'stderr': stderr,
        'line_elapsed_seconds': line_elapsed,
    }


def changed_paths_and_patch(repo, base_commit):
    must(run(['git', 'add', '-A', '--', '.'], cwd=repo))
    patch = must(run(['git', 'diff', '--cached', '--binary', base_commit], cwd=repo))['output']
    raw = must(run(['git', 'diff', '--cached', '--name-only', '-z', base_commit], cwd=repo))['output']
    ignored = must(run(['git', 'ls-files', '--others', '--ignored', '--exclude-standard', '-z'], cwd=repo))['output']
    return patch, [p for p in raw.split('\0') if p], [p for p in ignored.split('\0') if p]


def patch_paths(patch):
    paths = set()
    for match in re.finditer(r'^diff --git a/(.+?) b/(.+)$', patch, re.M):
        paths.update(match.groups())
    return paths


def is_test_path(path):
    parts = Path(path).parts
    name = Path(path).name.lower()
    return (
        any(part.lower() in {'test', 'tests', '__tests__'} for part in parts)
        or name.startswith(('test_', 'spec_'))
        or name.endswith(('_test.py', '_test.cc', '_test.cpp', '_test.rs',
                          '.test.js', '.test.jsx', '.test.ts', '.test.tsx',
                          '.spec.js', '.spec.jsx', '.spec.ts', '.spec.tsx'))
    )


def find_protected_paths(changed, test_patch):
    acceptance_files = patch_paths(test_patch)
    return sorted(path for path in changed if is_test_path(path) or path in acceptance_files)


def endpoint_info(base_url):
    return read_models(base_url, timeout=bounded_timeout(15))


def choose_endpoint_model(info, requested=None):
    model_ids = [model['id'] for model in info.get('data', [])]
    if requested:
        return requested
    if len(model_ids) != 1:
        raise RuntimeError(f'endpoint must serve exactly one model or --endpoint-model must be set: {model_ids}')
    return model_ids[0]


def write_pi_config(path, base_url, endpoint_model, label):
    path.mkdir(parents=True)
    config = {
        'providers': {
            'benchmark': {
                'baseUrl': base_url.rstrip('/'),
                'apiKey': '${OLYMPIAD_API_KEY}' if os.environ.get('OLYMPIAD_API_KEY') else 'local',
                'api': 'openai-completions',
                'models': [{
                    'id': endpoint_model,
                    'name': label,
                    'reasoning': os.environ.get('OLYMPIAD_REASONING') == '1',
                    'input': ['text'],
                    'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0},
                    'contextWindow': int(os.environ.get('OLYMPIAD_CONTEXT_WINDOW', '131072')),
                    'maxTokens': int(os.environ.get('OLYMPIAD_MAX_TOKENS', '65536')),
                }],
            }
        }
    }
    shell = bash_command()
    if shell:
        (path / 'settings.json').write_text(json.dumps({'shellPath': shell}), encoding='utf-8')
    (path / 'models.json').write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')


def load_task(name):
    path = Path(name)
    if not path.exists():
        path = TASKS / (name if name.endswith('.json') else f'{int(name):04d}.json')
    return path, json.loads(path.read_text(encoding='utf-8'))


def validate_saved_preflight(task, task_path, validation_path):
    saved = json.loads(Path(validation_path).read_text(encoding='utf-8'))
    task_hash = hashlib.sha256(Path(task_path).read_bytes()).hexdigest()
    if not saved.get('validated') or saved.get('instance_id') != task['instance_id']:
        raise RuntimeError('saved task preflight is missing, incomplete, or for a different task')
    if saved.get('base_commit') != task['base_commit'] or saved.get('task_file_sha256') != task_hash:
        raise RuntimeError('saved task preflight does not match the frozen task fixture')
    if saved.get('evaluator_image') != task.get('_benchmark_image'):
        raise RuntimeError('saved task preflight does not match the pinned evaluator image')
    return saved


def resolve_output(path, default):
    return (path or default).expanduser().resolve()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', default='7', help='task index or JSON path')
    parser.add_argument('--base-url', default='http://127.0.0.1:8000/v1')
    parser.add_argument('--endpoint-model', help='default: discover the endpoint’s single served model')
    parser.add_argument('--model-label', default='unnamed-model')
    parser.add_argument('--thinking', default='off')
    parser.add_argument('--agent-timeout', type=int, default=1800)
    parser.add_argument('--test-timeout', type=int, default=900)
    parser.add_argument('--python', default=shutil.which('python3.11') or sys.executable)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--cleanup-workdirs', action='store_true')
    parser.add_argument('--deadline-epoch', type=float)
    parser.add_argument('--validate-only', action='store_true',
                        help='run baseline+gold validation, skip the agent; exit 0 iff validated')
    parser.add_argument('--validated-result', type=Path,
                        help='reuse a matching successful --validate-only result from suite preflight')
    args = parser.parse_args()
    endpoint = urlsplit(args.base_url)
    if endpoint.scheme not in {'http', 'https'} or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        parser.error('Use an http(s) endpoint without embedded credentials, query or fragment; set OLYMPIAD_API_KEY')


    global DEADLINE_EPOCH
    DEADLINE_EPOCH = args.deadline_epoch
    signal.signal(signal.SIGTERM, stop_active_pi)
    signal.signal(signal.SIGINT, stop_active_pi)
    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, stop_active_pi)

    os.environ['OLYMPIAD_REASONING'] = '0' if args.thinking == 'off' else '1'
    task_path, task = load_task(args.task)
    stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
    out = resolve_output(args.output, RESULTS / f"{stamp}-{task['instance_id']}-{args.model_label}")
    out.mkdir(parents=True, exist_ok=False)
    task_started = time.monotonic()
    result = {
        'task_file': str(task_path),
        'instance_id': task['instance_id'],
        'repo': task['repo'],
        'base_commit': task['base_commit'],
        'task_file_sha256': hashlib.sha256(task_path.read_bytes()).hexdigest(),
        'evaluator_image': task.get('_benchmark_image'),
        'model_label': args.model_label,
        'base_url': args.base_url,
        'endpoint_model': args.endpoint_model,
        'thinking': args.thinking,
        'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'validated': False,
    }

    try:
        # Validation (baseline/gold) must not depend on the LLM endpoint being
        # up: the model may be mid-switch on the serving box.
        if not args.validate_only:
            result['endpoint_before'] = {'data': [{'id': args.endpoint_model}], 'source': 'explicit configuration; discovery skipped'} if args.endpoint_model else endpoint_info(args.base_url)
            endpoint_model = choose_endpoint_model(result['endpoint_before'], args.endpoint_model)
            result['endpoint_model'] = endpoint_model
        agent_repo = out / 'agent-repo'
        evaluator_repo = out / 'evaluator-repo'
        clone_task_repo(task, agent_repo, evaluator_repo)
        if task['repo'] == 'django/django':
            evaluator_python = make_venv(args.python, out / 'evaluator-venv', evaluator_repo)
            agent_python = make_venv(args.python, out / 'agent-venv', agent_repo)
            result['python_environments'] = {}
            for name, executable in [('agent', agent_python), ('evaluator', evaluator_python)]:
                version = must(run([str(executable), '--version']))['output'].strip()
                packages = must(run([str(executable), '-m', 'pip', 'freeze', '--all']))['output'].splitlines()
                result['python_environments'][name] = {'python': version, 'installed_packages': packages}

        else:
            evaluator_python = agent_python = None

        if args.validated_result:
            validation = validate_saved_preflight(task, task_path, args.validated_result)
            result['baseline'] = validation.get('baseline')
            result['gold'] = validation.get('gold')
            result['validation_record'] = str(args.validated_result)
            result['validated'] = True
        else:
            reset_repo(evaluator_repo, task['base_commit'])
            must(apply_patch(evaluator_repo, task.get('test_patch') or ''))
            baseline = run_tests(task, evaluator_repo, evaluator_python, out / 'baseline.log', args.test_timeout)
            result['baseline'] = baseline
            baseline_valid = (
                baseline_is_valid(task, baseline['outcomes']) if task['repo'] == 'django/django'
                else baseline['returncode'] != 0
            )
            if not baseline_valid:
                raise RuntimeError('baseline did not fail acceptance tests as expected')

            reset_repo(evaluator_repo, task['base_commit'])
            must(apply_patch(evaluator_repo, task.get('test_patch') or ''))
            must(apply_patch(evaluator_repo, task.get('patch') or ''))
            gold = run_tests(task, evaluator_repo, evaluator_python, out / 'gold.log', args.test_timeout,
                             patch=task.get('patch'))
            result['gold'] = gold
            gold_valid = (
                all_tests_pass(task, gold['outcomes'], gold['returncode']) if task['repo'] == 'django/django'
                else gold['returncode'] == 0
            )
            if not gold_valid:
                raise RuntimeError('gold patch did not pass the acceptance tests')
            result['validated'] = True
        if args.validate_only:
            result['score'] = {'passed': None, 'invalid': 'validate-only'}
            raise _ValidatedOnly
        reset_repo(evaluator_repo, task['base_commit'])

        pi_config = out / 'pi-config'
        write_pi_config(pi_config, args.base_url, endpoint_model, args.model_label)
        environment_hint = (
            f'A Python environment with this checkout installed is at {agent_python}; use it for test commands.'
            if agent_python else
            'Use the repository’s available language tools and package scripts for checks.'
        )
        prompt = f"""Fix this issue in the current {task['repo']} checkout at commit {task['base_commit']}.

{task.get('problem_statement') or ''}

Hints from the issue discussion:
{task.get('hints_text') or '(none)'}

Inspect the repository, edit the implementation, and run relevant existing tests. {environment_hint} Acceptance tests are hidden and will be applied after you finish. Do not modify tests, fixtures, benchmark files, or commit history. Make the smallest correct production-code change, verify it, then stop. Do not commit.
"""
        (out / 'prompt.txt').write_text(prompt, encoding='utf-8')
        selector = f'benchmark/{endpoint_model}'
        cmd = [
            *pi_command(), '--model', selector, '--thinking', args.thinking,
            '--mode', 'json', '--no-session', '--no-approve', '--no-extensions', '--no-skills', '--no-prompt-templates', '--no-themes', '--no-mcp',
            '--tools', 'read,bash,edit,write', '-p',
        ]
        env = os.environ.copy()
        env.update({
            'PI_CODING_AGENT_DIR': str(pi_config),
            'PI_OFFLINE': '1',
            'PI_SKIP_VERSION_CHECK': '1',
        })
        agent = run_pi(cmd, agent_repo, env, args.agent_timeout, input_text=prompt)
        (out / 'agent.stdout.jsonl').write_text(agent.pop('stdout'), encoding='utf-8')
        (out / 'agent.stderr.txt').write_text(agent.pop('stderr'), encoding='utf-8')
        stdout = (out / 'agent.stdout.jsonl').read_text(encoding='utf-8')
        usage, tool_count, stop, agent_error, request_timings = parse_pi_metrics(
            stdout, agent.pop('line_elapsed_seconds', None))
        result['agent'] = {
            **agent, 'usage': usage, 'tool_count': tool_count,
            'stop_reason': stop, 'error': agent_error, 'requests': request_timings,
        }
        failure_kind, failure_message = classify_pi_failure(agent, stop, agent_error)
        if failure_kind == 'infra':
            result['infra_error'] = failure_message
        elif failure_kind == 'agent':
            result['agent_failure'] = failure_message

        candidate, changed, ignored = changed_paths_and_patch(agent_repo, task['base_commit'])
        (out / 'candidate.patch').write_text(candidate, encoding='utf-8')
        protected = find_protected_paths(changed, task.get('test_patch') or '')
        result['candidate'] = {
            'changed_paths': changed,
            'protected_paths': protected,
            'ignored_untracked_paths': ignored,
            'patch_bytes': len(candidate.encode()),
        }

        if result.get('infra_error'):
            result['score'] = {'passed': None, 'invalid': 'agent infrastructure error'}
        elif result.get('agent_failure'):
            result['score'] = {'passed': False, 'invalid': 'agent/provider error'}
        elif protected:
            result['score'] = {'passed': False, 'invalid': 'candidate modified protected test paths'}
        elif agent['timed_out']:
            result['score'] = {'passed': False, 'invalid': 'agent timeout'}
        else:
            reset_repo(evaluator_repo, task['base_commit'])
            must(apply_patch(evaluator_repo, task.get('test_patch') or ''))
            candidate_apply = apply_patch(evaluator_repo, candidate)
            result['candidate_apply'] = candidate_apply
            if candidate_apply['returncode']:
                result['score'] = {'passed': False, 'invalid': 'candidate patch did not apply'}
            else:
                final = run_tests(task, evaluator_repo, evaluator_python, out / 'final.log', args.test_timeout,
                                  patch=candidate)
                result['final_tests'] = final
                result['score'] = {
                    'passed': all_tests_pass(task, final['outcomes'], final['returncode'])
                        if task['repo'] == 'django/django' else final['returncode'] == 0,
                    'outcomes': final.get('outcomes'),
                }
        result['endpoint_after'] = {'data': [{'id': endpoint_model}], 'source': 'explicit configuration; discovery skipped'} if args.endpoint_model else endpoint_info(args.base_url)
    except _ValidatedOnly:
        pass
    except Exception as exc:
        result['infra_error'] = f'{type(exc).__name__}: {exc}'
    finally:
        result['ended_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        if 'task_started' in locals():
            result['task_wall_seconds'] = round(time.monotonic() - task_started, 3)
        (out / 'result.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
        if args.cleanup_workdirs:
            for name in ('agent-repo', 'evaluator-repo', 'agent-venv', 'evaluator-venv'):
                shutil.rmtree(out / name, ignore_errors=True)

    print(json.dumps({
        'output': str(out),
        'validated': result.get('validated'),
        'passed': (result.get('score') or {}).get('passed'),
        'infra_error': result.get('infra_error'),
        'agent': result.get('agent'),
        'candidate': {
            key: value for key, value in (result.get('candidate') or {}).items()
            if key != 'ignored_untracked_paths'
        },
    }, indent=2))
    return 0 if result.get('validated') and (args.validate_only or (result.get('score') or {}).get('passed')) else 1


class _ValidatedOnly(Exception):
    pass


if __name__ == '__main__':
    raise SystemExit(main())
