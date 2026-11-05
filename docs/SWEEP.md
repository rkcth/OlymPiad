# Fast concurrency sweep

```sh
coding-benchmark sweep --config benchmark.json --max-agents 8
coding-benchmark sweep --config benchmark.json --max-agents 16 --output sweep.ndjson --format ndjson
```

The maximum must be a power of two, including 1. A maximum of 8 measures **1 → 2 → 4 → 8** simultaneous Pi agents. Each agent generates a short coding response in an isolated temporary directory, with tools, reasoning, extensions, MCP and automatic retries disabled. Docker, task clones, reference patches and Django environments are not needed. Python, Node and the installed locked Pi runtime are needed. This mode sends real generation requests to the configured server and can consume paid tokens.

This is a capacity probe, separate from the frozen 15-task correctness benchmark. It does not assign a SWE score or establish the best concurrency for real repository work. Use `run --agents N` for that benchmark.

## Stop as soon as the measurements settle

Each concurrency level starts with one discarded warmup wave. Then it measures simultaneous waves until **both** the median per-agent prefill estimate and token-generation (TG) estimate have a stable contiguous window. The default is three measured waves, with `(maximum − minimum) / median ≤ 10%` for each metric. It moves to the next level immediately when that condition holds; there is no extra cooldown or fixed-duration run.

Defaults: at most 10 measured waves per level, a 180-second level scheduling budget, 60 seconds per request, a 256-token output cap and at least 64 measured output tokens per agent. Request shutdown/process cleanup can add time to the scheduling budget. Change these with `--window`, `--tolerance`, `--max-rounds`, `--level-timeout`, `--request-timeout`, `--max-tokens` and `--min-output-tokens`. The minimum stability window is three.

Every agent must return usage and positive streaming timings. Their request intervals must actually overlap; merely starting N processes is insufficient. A missing/short/non-overlapping measurement resets the contiguous window. If a level does not stabilize before its limits, it is reported as unstable with null final rates and the sweep stops. It does not silently raise load or present the last noisy reading as stable. Provider errors/timeouts stop the sweep; there is no automatic retry. Ctrl-C stops owned Pi processes.

The coding prompt is fixed and versioned, with a fresh leading probe identifier to reduce exact prompt reuse. Server caching can still occur, including Pi's shared system prefix. Cache-read counts are retained when reported by Pi. This is not a cold-cache or engine-internal benchmark.

## Measurements and streamed reports

The same text or NDJSON report can be streamed to stdout or a new file. Events include `sweep_start`, `sweep_level_start`, `sweep_sample`, `sweep_level_complete` and `sweep_complete`. Interrupted, failed and non-stable outcomes are explicit. Warmup samples remain visible and labeled. Preserve the stream as the sweep result; suite `report`/`export`/`compare` commands currently consume full coding runs, not sweep streams.

- **Prefill estimate:** median across agents of reported input tokens divided by client time to first generated content. Network latency, server queueing and caching affect it.
- **TG estimate:** median across agents of reported output tokens divided by client-observed generation time.
- **Aggregate output throughput:** sum of output tokens divided by whole wave wall time, including Pi process startup. It is not the sum of per-agent rates.

These are client-observed estimates, not server-internal PP/TG counters. The stream records model, Pi/runtime-lock identity, prompt version/hash, limits, every sample, cache-read counts and the final stable window's medians. Default stability is a fast heuristic, not a statistical confidence guarantee. Keep server configuration, hardware and background traffic consistent for comparisons.

Exit statuses: 0 means every requested level stabilized; 3 means a level did not stabilize; 2 means invalid configuration/prerequisite/provider failure; 130 means interruption. An abruptly truncated stream is incomplete.
