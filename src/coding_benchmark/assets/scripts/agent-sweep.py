#!/usr/bin/env python3
"""Bounded Pi capacity probes; never a substitute for frozen-task scoring."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import signal
import statistics
import tempfile
import threading
import time
import uuid

from configuration import read_config, validate_endpoint
from process_control import kill_tree
from report_stream import ReportStream
from runtime import pi_command, PI_VERSION
from server_http import check_server

RATE_KEYS = ('prefill_tokens_per_second', 'tg_tokens_per_second')
PROMPT_VERSION = 'coding-capacity-v1'
CONTEXT = '\n'.join(f'# fixture {i:03d}: validate inputs, retain ordering, test empty and duplicate values.' for i in range(128))
PROMPT = ('You are measuring coding generation speed, not solving a repository task. Do not call tools.\n'
          'Read the following synthetic review context:\n' + CONTEXT + '\n'
          'Write only Python code: a small bounded LRU cache with get/put methods, type hints, '
          'docstrings and three example assertions. Aim for about 200 output tokens. No explanation.')


def levels(max_agents):
    if type(max_agents) is not int or max_agents < 1 or max_agents & (max_agents - 1):
        raise ValueError('--max-agents must be a positive power of two: 1, 2, 4, 8, 16, …')
    return [1 << i for i in range(max_agents.bit_length())]


def stable(rows, window, tolerance):
    if len(rows) < window:
        return False
    for key in RATE_KEYS:
        values = [row.get(key) for row in rows[-window:]]
        if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in values):
            return False
        center = statistics.median(values)
        if (max(values) - min(values)) / center > tolerance:
            return False
    return True


def summarize(probes, wall_seconds, min_output_tokens):
    """Rates remain unknown unless EVERY simultaneous probe has usable timing."""
    metrics = []
    intervals = []
    for probe in probes:
        interval = probe.get('request_interval')
        if not interval or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in interval):
            return None
        intervals.append(interval)
        requests = probe['requests']
        if len(requests) != 1:
            return None
        request = requests[0]
        usage = request.get('usage', {})
        pp = request.get('client_estimated_prefill_tokens_per_second')
        tg = request.get('client_estimated_decode_tokens_per_second')
        if not all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in (pp, tg)):
            return None
        if usage.get('output', 0) < min_output_tokens:
            return None
        metrics.append((pp, tg, usage))
    if not metrics or wall_seconds <= 0:
        return None
    if max(start for start, _ in intervals) >= min(end for _, end in intervals):
        # Starting N processes alone does not prove the N requests overlapped.
        return None
    output = sum(item[2]['output'] for item in metrics)
    return {'prefill_tokens_per_second': statistics.median(item[0] for item in metrics),
            'tg_tokens_per_second': statistics.median(item[1] for item in metrics),
            'aggregate_output_tokens_per_second': output / wall_seconds,
            'output_tokens': output,
            'cache_read_tokens': sum(item[2]['cacheRead'] for item in metrics)
                if all(isinstance(item[2].get('cacheRead'), (int, float)) for item in metrics) else None,
            'overlapping_requests': len(probes),
            'wall_seconds': wall_seconds}


class Processes:
    def __init__(self):
        self.lock = threading.Lock()
        self.cancelled = False
        self.children = set()

    def observe(self, process, started):
        with self.lock:
            if started:
                self.children.add(process)
                if self.cancelled:
                    kill_tree(process)
            else:
                self.children.discard(process)

    def stop(self):
        with self.lock:
            self.cancelled = True
            for process in self.children:
                kill_tree(process)


def load_runner():
    path = Path(__file__).with_name('swe-agent-bench.py')
    spec = importlib.util.spec_from_file_location('sweep_pi_runner', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def probe(runner, command, root, env, timeout, processes, nonce):
    root.mkdir()
    prompt = f'Independent probe {nonce}.\n' + PROMPT
    started = time.monotonic()
    result = runner.run_pi(command, root, env, timeout, input_text=prompt, process_observer=processes.observe)
    _, tools, stop, error, requests = runner.parse_pi_metrics(result['stdout'], result['line_elapsed_seconds'])
    failure, _ = runner.classify_pi_failure(result, stop, error)
    if result['timed_out'] or failure or tools or stop != 'stop':
        # Provider error bodies/transcripts can include secrets; keep public errors generic.
        raise ValueError('Capacity probe failed, timed out, or did not complete normally; no automatic retry or higher load was started.')
    interval = None
    if len(requests) == 1:
        beginning = requests[0].get('started_elapsed_seconds')
        end = requests[0].get('ended_elapsed_seconds')
        if beginning is not None and end is not None:
            interval = (started + beginning, started + end)
    return {'requests': requests, 'request_interval': interval}


def sweep(args, sink, wave):
    """Injectable wave runner keeps stability/stop semantics independently testable."""
    results = []
    for agents in levels(args.max_agents):
        sink.emit('sweep_level_start', f'{agents} agent(s): warming up', agents=agents)
        deadline = time.monotonic() + args.level_timeout
        history = []
        status = 'unstable'
        rounds = 0
        for index in range(args.max_rounds + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sample = wave(agents, index, min(args.request_timeout, remaining))
            rounds += 1
            warmup = index == 0
            sink.emit('sweep_sample', f'{agents} agent(s), round {index}: ' + ('warmup' if warmup else str(sample)),
                      agents=agents, round=index, warmup=warmup, metrics=sample)
            if not warmup:
                # Missing usage/timing resets the whole contiguous stability window.
                history.append(sample or {key: None for key in RATE_KEYS})
                if stable(history, args.window, args.tolerance):
                    status = 'stable'
                    break
        measured = history[-args.window:]
        row = {'agents': agents, 'status': status, 'rounds_including_warmup': rounds,
               'metrics': {key: statistics.median(r[key] for r in measured)
                           for key in (*RATE_KEYS, 'aggregate_output_tokens_per_second')}
               if status == 'stable' else None}
        results.append(row)
        sink.emit('sweep_level_complete', f'{agents} agent(s): {status}; {row["metrics"]}', **row)
        # No benefit from doubling when the lower level cannot be measured reliably.
        if status != 'stable':
            sink.emit('sweep_stopped', 'No stable measurement before the limit; higher concurrency was not attempted.', agents=agents)
            break
    complete = len(results) == len(levels(args.max_agents)) and all(r['status'] == 'stable' for r in results)
    sink.emit('sweep_complete', 'Sweep complete' if complete else 'Sweep incomplete', complete=complete, levels=results)
    return 0 if complete else 3


def main(argv=None):
    parser = argparse.ArgumentParser(description='Fast doubling-concurrency Pi capacity sweep; no Docker or task scoring.')
    parser.add_argument('--max-agents', required=True, type=int, help='Power-of-two ceiling: 1, 2, 4, 8, …')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--base-url')
    parser.add_argument('--endpoint-model')
    parser.add_argument('--output', default='-', help='- for stdout or a NEW report file')
    parser.add_argument('--format', choices=['text', 'ndjson'], default='text')
    parser.add_argument('--window', type=int, default=3, help='Consecutive measured waves required (default: 3)')
    parser.add_argument('--tolerance', type=float, default=0.10, help='Maximum (max-min)/median spread for both rates (default: 0.10)')
    parser.add_argument('--max-rounds', type=int, default=10, help='Measured waves per level, excluding one warmup')
    parser.add_argument('--level-timeout', type=float, default=180, help='Maximum measurement time per level in seconds')
    parser.add_argument('--request-timeout', type=float, default=60)
    parser.add_argument('--min-output-tokens', type=int, default=64)
    parser.add_argument('--max-tokens', type=int, default=256)
    args = parser.parse_args(argv)
    try:
        levels(args.max_agents)
        if args.window < 3 or args.max_rounds < args.window:
            raise ValueError('--window must be at least 3 and --max-rounds must be at least --window')
        if not math.isfinite(args.tolerance) or not 0 < args.tolerance <= 1:
            raise ValueError('--tolerance must be in (0, 1]')
        if any(not math.isfinite(v) or v <= 0 for v in (args.level_timeout, args.request_timeout)):
            raise ValueError('Timeouts must be finite positive seconds')
        if not 1 <= args.min_output_tokens <= args.max_tokens:
            raise ValueError('Require 1 <= --min-output-tokens <= --max-tokens')
        config = read_config(args.config) if args.config else {}
        endpoint = validate_endpoint(args.base_url or config.get('base_url', 'http://127.0.0.1:8000/v1'))
        model = args.endpoint_model or config.get('endpoint_model')
        command = pi_command()  # fail before contacting the endpoint if Pi/Node is unavailable
        server = check_server(endpoint, model)
        if server['status'] == 'error':
            raise ValueError(server['message'])
        model = server['model']
        runner = load_runner()
        processes = Processes()
        sink = ReportStream(args.output, args.format)
        pool = ThreadPoolExecutor(max_workers=args.max_agents)
        old_handlers = {}
        def interrupted(signum, frame):
            raise KeyboardInterrupt
        try:
            for sig in (signal.SIGINT, signal.SIGTERM, *([signal.SIGBREAK] if hasattr(signal, 'SIGBREAK') else [])):
                old_handlers[sig] = signal.signal(sig, interrupted)
            with tempfile.TemporaryDirectory(prefix='coding-capacity-') as tmp:
                root = Path(tmp)
                runner.write_pi_config(root / 'config', endpoint, model, model)
                # Explicit bounded generation config, independent of the full coding suite.
                path = root / 'config/models.json'
                settings = json.loads(path.read_text(encoding='utf-8'))
                settings['providers']['benchmark']['models'][0].update(maxTokens=args.max_tokens, reasoning=False)
                path.write_text(json.dumps(settings), encoding='utf-8')
                path = root / 'config/settings.json'
                settings = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
                settings.update(retry={'enabled': False, 'maxRetries': 0, 'provider': {'maxRetries': 0}},
                                compaction={'enabled': False})
                path.write_text(json.dumps(settings), encoding='utf-8')
                env = os.environ.copy()
                env.update(PI_CODING_AGENT_DIR=str(root / 'config'), PI_OFFLINE='1', PI_SKIP_VERSION_CHECK='1')
                command += ['--model', f'benchmark/{model}', '--thinking', 'off', '--mode', 'json', '--no-session',
                            '--no-approve', '--no-extensions', '--no-skills', '--no-prompt-templates', '--no-themes', '--no-mcp', '--no-tools', '-p']
                sink.emit('sweep_start', f'Pi capacity sweep through {args.max_agents} agents; client-observed estimates, not task scores.',
                          model=model, pi_version=PI_VERSION, max_agents=args.max_agents, window=args.window,
                          tolerance=args.tolerance, max_rounds=args.max_rounds, level_timeout=args.level_timeout,
                          max_tokens=args.max_tokens, min_output_tokens=args.min_output_tokens,
                          workload=PROMPT_VERSION, prompt_sha256=hashlib.sha256(PROMPT.encode()).hexdigest(),
                          pi_lock_sha256=hashlib.sha256((Path(__file__).parents[1] / 'pi-runtime/package-lock.json').read_bytes()).hexdigest(),
                          rate_scope='Median per-agent client-observed input/TTFT and output/generation; caching/network/queueing included. Aggregate output includes Pi startup.')
                def wave(agents, index, timeout):
                    started = time.monotonic()
                    batch = uuid.uuid4().hex
                    futures = [pool.submit(probe, runner, command, root / f'{batch}-{i}', env, timeout, processes, f'{batch}-{i}') for i in range(agents)]
                    try:
                        probes = [f.result() for f in futures]
                    except BaseException:
                        processes.stop()
                        raise
                    return summarize(probes, time.monotonic() - started, args.min_output_tokens)
                return sweep(args, sink, wave)
        except KeyboardInterrupt:
            sink.emit('sweep_cancelled', 'Sweep interrupted; stopping owned Pi processes.')
            return 130
        except (ValueError, OSError) as exc:
            sink.emit('sweep_failed', str(exc), complete=False)
            return 2
        finally:
            processes.stop()
            pool.shutdown(wait=True, cancel_futures=True)
            for sig, old in old_handlers.items():
                signal.signal(sig, old)
            sink.close()
    except (ValueError, OSError) as exc:
        parser.exit(2, f'coding-benchmark sweep: {exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
