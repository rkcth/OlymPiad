import contextlib
import io
import json
import math
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_benchmark import module
from olympiad.assets.scripts.report_stream import ReportStream

sweep = module('agent-sweep')


def options(**changes):
    return SimpleNamespace(**({'max_agents': 8, 'window': 3, 'tolerance': .10,
                               'max_rounds': 10, 'level_timeout': 180, 'request_timeout': 60} | changes))


def sample(pp=100, tg=50, aggregate=50):
    return {'prefill_tokens_per_second': pp, 'tg_tokens_per_second': tg,
            'aggregate_output_tokens_per_second': aggregate}


class SweepTests(unittest.TestCase):
    def test_power_of_two_levels_and_invalid_ceiling_before_any_network(self):
        self.assertEqual(sweep.levels(8), [1, 2, 4, 8])
        self.assertEqual(sweep.levels(1), [1])
        for value in (0, -1, 3, 6, 10):
            with self.assertRaises(ValueError):
                sweep.levels(value)
        with patch.object(sweep, 'check_server') as server, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                sweep.main(['--max-agents', '3'])
            self.assertEqual(error.exception.code, 2)
            server.assert_not_called()

    def test_stability_requires_both_metrics_and_contiguous_full_window(self):
        self.assertFalse(sweep.stable([sample()] * 2, 3, .10))
        self.assertTrue(sweep.stable([sample(), sample(101, 51), sample(99, 49)], 3, .10))
        for bad in (sample(120, 50), sample(100, 70), sample(None, 50), sample(math.nan, 50)):
            self.assertFalse(sweep.stable([sample(), sample(), bad], 3, .10))

    def run_sweep(self, args, wave):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = sweep.sweep(args, ReportStream(format='ndjson'), wave)
        return result, [json.loads(line) for line in output.getvalue().splitlines()]

    def test_switches_immediately_after_stable_window_and_retains_warmup(self):
        calls = []
        def wave(agents, index, timeout):
            calls.append((agents, index))
            return sample(aggregate=50 * agents)
        result, events = self.run_sweep(options(), wave)
        self.assertEqual(result, 0)
        self.assertEqual(calls, [(n, i) for n in (1, 2, 4, 8) for i in range(4)])
        self.assertEqual(len([e for e in events if e['event'] == 'sweep_sample' and e['warmup']]), 4)
        self.assertTrue(events[-1]['complete'])

    def test_unstable_or_missing_metrics_stops_without_higher_load_or_fake_zero(self):
        for value in (None,):
            calls = []
            def wave(agents, index, timeout):
                calls.append(agents)
                return value
            result, events = self.run_sweep(options(max_rounds=3), wave)
            self.assertEqual(result, 3)
            self.assertEqual(calls, [1] * 4)
            self.assertIsNone(events[-1]['levels'][0]['metrics'])
            self.assertFalse(events[-1]['complete'])

    def test_deadline_never_schedules_another_wave(self):
        calls = []
        with patch.object(sweep.time, 'monotonic', side_effect=[0, 0, 181]):
            result, _ = self.run_sweep(options(), lambda *args: calls.append(args) or sample())
        self.assertEqual(result, 3)
        self.assertEqual(len(calls), 1)

    def test_every_agent_needs_positive_usage_timing_and_sufficient_output(self):
        request = {'usage': {'output': 100, 'cacheRead': 7},
                   'client_estimated_prefill_tokens_per_second': 100,
                   'client_estimated_decode_tokens_per_second': 50}
        good = {'requests': [request], 'request_interval': (0, 1)}
        result = sweep.summarize([good, good], 2, 64)
        self.assertEqual(result['aggregate_output_tokens_per_second'], 100)
        self.assertEqual(result['cache_read_tokens'], 14)
        unknown_cache = good | {'requests': [request | {'usage': {'output': 100}}]}
        self.assertIsNone(sweep.summarize([unknown_cache], 1, 64)['cache_read_tokens'])
        self.assertIsNone(sweep.summarize([good, good | {'request_interval': (2, 3)}], 3, 64))
        for bad in ({'requests': []}, {'requests': [request | {'usage': {'output': 3}}]},
                    {'requests': [request | {'client_estimated_decode_tokens_per_second': None}]}):
            self.assertIsNone(sweep.summarize([good, bad], 2, 64))

    def test_cancellation_also_kills_a_late_registering_process(self):
        processes = sweep.Processes()
        first, late = object(), object()
        with patch.object(sweep, 'kill_tree') as kill:
            processes.observe(first, True)
            processes.stop()
            processes.observe(late, True)
            self.assertEqual([call.args[0] for call in kill.call_args_list], [first, late])


if __name__ == '__main__':
    unittest.main()

@unittest.skipUnless(os.environ.get('OLYMPIAD_PI_SMOKE') == '1', 'Requires the managed Pi; loopback only')
class RealSweepTests(unittest.TestCase):
    def test_real_pi_capacity_probe_streams_reports_without_tools_or_docker(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import tempfile
        import threading
        import time
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'data': [{'id': 'synthetic-capacity'}]}).encode())
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append(body)
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                base = {'id': 'synthetic-capacity', 'model': 'synthetic-capacity', 'created': 1, 'object': 'chat.completion.chunk'}
                for index in range(3):
                    time.sleep(.06)
                    event = {**base, 'choices': [{'index': 0, 'delta': {'content': 'synthetic code '}, 'finish_reason': None}]}
                    self.wfile.write(('data: ' + json.dumps(event) + '\n\n').encode())
                    self.wfile.flush()
                event = {**base, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
                         'usage': {'prompt_tokens': 2048, 'completion_tokens': 128, 'total_tokens': 2176}}
                self.wfile.write(('data: ' + json.dumps(event) + '\n\ndata: [DONE]\n\n').encode())
                self.wfile.flush()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix='sweep report ü ') as tmp:
                report = Path(tmp) / 'report.ndjson'
                result = sweep.main(['--max-agents', '2', '--base-url', f'http://127.0.0.1:{server.server_port}/v1',
                                     '--endpoint-model', 'synthetic-capacity', '--max-rounds', '3',
                                     '--tolerance', '1', '--output', str(report), '--format', 'ndjson'])
                self.assertEqual(result, 0)
                events = [json.loads(line) for line in report.read_text(encoding='utf-8').splitlines()]
                self.assertTrue(events[-1]['complete'])
                self.assertEqual([row['agents'] for row in events[-1]['levels']], [1, 2])
                self.assertEqual(len(calls), 12)
                self.assertTrue(all(not body.get('tools') for body in calls))
                self.assertTrue(all(0 < (body.get('max_tokens') or body.get('max_completion_tokens') or 0) <= 256 for body in calls))
                self.assertTrue(all(e['metrics'] is not None for e in events if e['event'] == 'sweep_sample'))
                self.assertEqual(len(set(json.dumps(body['messages']) for body in calls)), 12)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_rate_limit_stops_without_retry_or_increasing_load(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import tempfile
        import threading
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"data":[{"id":"synthetic-capacity"}]}')
            def do_POST(self):
                calls.append(self.rfile.read(int(self.headers['Content-Length'])))
                self.send_response(429)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"synthetic rate limit"}}')
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                report = Path(tmp) / 'failed.ndjson'
                result = sweep.main(['--max-agents', '2', '--base-url', f'http://127.0.0.1:{server.server_port}/v1',
                                     '--endpoint-model', 'synthetic-capacity', '--output', str(report), '--format', 'ndjson'])
                self.assertEqual(result, 2)
                self.assertEqual(len(calls), 1)
                events = [json.loads(line) for line in report.read_text(encoding='utf-8').splitlines()]
                self.assertEqual(events[-1]['event'], 'sweep_failed')
                self.assertEqual([e['agents'] for e in events if e['event'] == 'sweep_level_start'], [1])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
