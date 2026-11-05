# Coding Benchmark

A standalone, open-source package for a fixed **15-task coding-agent benchmark**. The first adapter runs [Pi](https://github.com/badlogic/pi-mono) against an OpenAI-compatible endpoint. No private server, Strata installation, image-maker application or account is required.

**Two primary output modes: the same live report on stdout, or in a file.** Each event is written and flushed immediately. Plain text is the default. Optional NDJSON writes one independently parseable event per line; it is not a partially written JSON document.

This is an extracted **alpha**, not a published PyPI release. The original frozen task/patch hashes are retained. The managed Pi adapter has been exercised against a local simulated OpenAI server on macOS and Linux. Full evaluator qualification and native Windows validation remain release gates; see [the audit](docs/CROSS_PLATFORM_AUDIT.md).

## Install

Install **Python 3.11**, **Node.js 22.19+**, **Git** and **Docker**. Python 3.11 or newer can run the controller/reporting; the frozen Django evaluator uses exactly Python 3.11. On Windows, install Git for Windows including Git Bash and use Docker Desktop in **Linux-container mode**. On Linux, Docker Engine is sufficient; Docker Desktop is optional. On macOS, use Docker Desktop. Docker Desktop has its own [subscription terms](https://docs.docker.com/subscription/desktop-license/), separate from this tool's MIT license.

Official downloads: [Python](https://www.python.org/downloads/), [Node.js](https://nodejs.org/en/download), [Git](https://git-scm.com/downloads), [Docker](https://docs.docker.com/get-started/get-docker/). Use the OS-specific installer/package instructions there; reopen the terminal after installing tools so PATH updates take effect.

The source/wheel includes all 15 task snapshots, reports, prerequisite checks and a locked Pi dependency manifest. `setup --install-pi` downloads **Pi 1.4.0 and integrity-pinned transitive dependencies** into a user-local tools directory. It does not require a global Pi installation, modify global npm packages, install Docker, start services, or change system permissions.

After extracting the downloaded source archive, open a terminal in its directory.

### macOS / Linux

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install .
.venv/bin/coding-benchmark setup --install-pi
.venv/bin/coding-benchmark doctor
.venv/bin/coding-benchmark plan
```

On Debian/Ubuntu, creating virtualenvs may also require `python3.11-venv`. The `doctor` output gives specific fixes for missing tools, an unavailable Docker engine, socket permissions, unsupported container mode and incompatible Node/Python versions. It warns about low disk/memory and ARM emulation. Fix errors before continuing; warnings do not silently change scoring.

### Windows PowerShell

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\coding-benchmark.exe setup --install-pi
.\.venv\Scripts\coding-benchmark.exe doctor
.\.venv\Scripts\coding-benchmark.exe plan
```

These commands do not require activating a PowerShell script or changing its execution policy. Use a short output path (for example `C:\bench\results`) to reduce legacy Windows path-length problems. WSL2 is also an option: install/run the Linux toolchain within WSL and enable Docker Desktop's WSL integration.

For the examples below, activate the virtualenv (`. .venv/bin/activate` on macOS/Linux) or use the full executable path shown above. On Windows you can use that executable path without activation.

`plan` verifies all task, reference patch, acceptance test and evaluator-image pins offline. It does not pull images or call a model.

## Save server settings once

```sh
coding-benchmark configure --base-url http://localhost:8000/v1 --model my-model --agents 3
coding-benchmark doctor --config benchmark.json --check-server
coding-benchmark run --config benchmark.json --pull-images
```

`configure` creates a new non-secret `benchmark.json` file; it never overwrites an existing file. CLI options override config values. `doctor --check-server` reads metadata, not a billable completion. A reachable model listing does not prove generation compatibility; the first scored request is still validated by Pi. A gateway without `/models` needs an explicit model ID.

Reasoning is off by default for compatibility with ordinary chat models. For a reasoning-capable model, choose `--thinking medium` (or another supported level). Set `--context-window` and `--max-tokens` to limits actually supported by your server. Token usage is reported, but provider billing is not estimated.

For authenticated servers, keep the key in the environment:

```sh
# macOS / Linux
export CODING_BENCHMARK_API_KEY="your-key"
```

```powershell
# Windows PowerShell
$env:CODING_BENCHMARK_API_KEY = "your-key"
```

Do not put keys in URLs or config files. Use HTTPS for remote authenticated servers. Metadata checks refuse cross-origin redirects rather than forwarding credentials elsewhere.

## Fast capacity sweep

```sh
coding-benchmark sweep --config benchmark.json --max-agents 8
```

Measures 1, 2, 4 and 8 simultaneous Pi agents, advancing as soon as both client-observed prefill and TG rates stabilize. The maximum must be a power of two. Supports the same stdout/file and text/NDJSON destinations. This short generation probe needs no Docker and produces capacity measurements rather than a 15-task correctness score. See [sweep limits and measurement definitions](docs/SWEEP.md).

## Run: stdout

```sh
coding-benchmark run \
  --base-url http://127.0.0.1:8000/v1 \
  --endpoint-model my-model \
  --model-label "My model / my settings" \
  --thinking off --pull-images
```

Example shape (illustrative, not a measured score):

```text
[timestamp] plan: pi-coding-suite-v2: 15 tasks, concurrency 3
[timestamp] task_finished: preflight 1/15 django__django-11910: validated
...
[timestamp] task_finished: tasks 1/15 vuejs__core-11589: pass
...
[timestamp] complete: Accepted N/15; evaluated M; batch ...s; tokens ...
```

Pipe it to another tool or save the exact same stream with `tee`:

```sh
coding-benchmark run --endpoint-model my-model | tee report.txt
```

The key is referenced through environment interpolation in Pi's configuration; its value is not written there. Explicit model selection avoids guessing; gateways returning 404/405 for `/models` can still be used with `--endpoint-model`.

## Run: a live file

```sh
coding-benchmark run \
  --endpoint-model my-model \
  --model-label "My model" \
  --output report.txt
```

The file must not already exist. It is flushed after every event, so another process can read it while the run is in progress:

```sh
tail -f report.txt
```

In PowerShell, the equivalent is `Get-Content report.txt -Wait`. Report files are UTF-8 on every platform.

`--output -` explicitly selects stdout. For machine readers, the same two destinations work with `--format ndjson`:

```sh
coding-benchmark run --endpoint-model my-model --format ndjson > events.ndjson
coding-benchmark run --endpoint-model my-model --format ndjson --output events.ndjson
```

Every NDJSON line contains `schema_version`, `timestamp`, `event`, `message` and event-specific fields. A consumer can parse each line without waiting for completion. It must check the terminal event and retained results; EOF alone does not prove a completed benchmark.

## Configure a run

Choose the maximum number of simultaneous agents with `--agents N` (`--concurrency N` is an alias):

```sh
coding-benchmark run --endpoint-model my-model --agents 4
```

The default is **3**. `--agents 1` runs sequentially. When a task finishes, its slot takes the next task until all 15 finish; increasing this switch does not change the task suite. The same limit applies to parallel baseline/reference validation. The chosen limit is recorded in run metadata and reports.

```sh
coding-benchmark run --help
```

Useful options:

| Option | Default | Meaning |
|---|---|---|
| `--base-url` | `http://127.0.0.1:8000/v1` | OpenAI-compatible endpoint |
| `--config` | None | Load non-secret settings from `configure` |
| `--endpoint-model` | Discover the single served model | Explicit model ID; avoids discovery |
| `--model-label` | `unnamed-model` | Human-readable model/build/settings label |
| `--agents`, `--concurrency` | `3` | Maximum simultaneous agent tasks; refill freed slots |
| `--thinking` | `off` | Pi thinking setting; model support varies |
| `--context-window` | `131072` | Declared model context window |
| `--max-tokens` | `65536` | Declared maximum completion tokens |
| `--agent-timeout` | `1800` seconds | Per-task agent timeout |
| `--test-timeout` | `900` seconds | Per evaluator invocation timeout |
| `--output` | `-` | Live stdout report or new report file |
| `--format` | `text` | Plain text or `ndjson` event stream |
| `--output-root` | `./benchmark-results` | Raw, never-reused run directories |
| `--preflight-only` | Off | Validate baseline/reference behavior without model calls |
| `--pull-images` | Off | Pull missing digest-pinned evaluator images |
| `--engine-version`, `--engine-revision` | Unspecified | Optional inference-engine provenance |
| `--python` | Auto-detected Python 3.11 | Django evaluator virtualenv interpreter |

Run checks prerequisites **before cloning tasks, pulling images or sending model requests**. All task baselines must fail the declared regression checks, and reference patches must pass, before any model request is sent. Preflight can clone repositories, install evaluator dependencies and pull images; it is not an offline command. Scored execution can incur provider charges. The controller adds no provider fallback or model-call replay policy; any retry behavior of the pinned upstream provider adapter remains upstream behavior.

Managed runtime locations are `~/Library/Application Support/CodingBenchmark` on macOS, `%LOCALAPPDATA%\CodingBenchmark` on Windows and `$XDG_DATA_HOME/coding-benchmark` (or `~/.local/share/coding-benchmark`) on Linux. `CODING_BENCHMARK_HOME` can select a separate tools directory. Do not run `setup --install-pi` while another benchmark is using that same managed runtime.

The controller's exit status reports execution: `0` means all 15 evaluations finished (not that all passed), `1` means incomplete/error, and `2` means blocked preflight/configuration. In CI, consume the JUnit export to enforce correctness thresholds.

## Read or export retained results

Re-render a retained run to stdout or one file, with no model/Docker/network calls:

```sh
coding-benchmark report benchmark-results/RUN
coding-benchmark report benchmark-results/RUN --output report.txt
coding-benchmark report benchmark-results/RUN --format ndjson
```

Optional exports are separate from the live stream:

```sh
coding-benchmark export benchmark-results/RUN --output reports/RUN
```

Exports: normalized `results.json`, `tasks.csv`, `report.md`, self-contained `report.html`, and `junit.xml`. No external JavaScript, fonts or telemetry are used by the HTML report. Missing usage/timing measurements remain `null`/blank. CSV output guards spreadsheet formula interpretation.

```sh
coding-benchmark compare benchmark-results/RUN-A benchmark-results/RUN-B \
  --output reports/comparison
```

Comparison writes JSON, CSV and Markdown. Changed suite hashes, changed recorded execution configuration, or missing provenance are explicitly marked descriptive-only. Different inference settings, hardware and provider behavior still need to be considered; this is not an automatic fair-performance certification.

## The frozen suite

| Source | Language | Tasks | Project license |
|---|---|---:|---|
| Django / SWE-bench Lite | Python | 6 | BSD-3-Clause |
| Docusaurus | TypeScript | 3 | MIT |
| Vue | TypeScript | 3 | MIT |
| fmt | C++ | 1 | MIT with optional object-code exception |
| nlohmann/json | C++ | 1 | MIT |
| Tokio | Rust | 1 | MIT |

The suite identity stays `pi-coding-suite-v2`, even though the package has a generic name, so historical results are not relabeled as a different benchmark. Nine non-Python tasks come from SWE-bench Multilingual revision `846e647b9f33c0b51b739d005d13d85493c9af09`. All 15 task files and their raw hashes are unchanged from the source manifest.

The six Django tasks were selected after a historical three-model comparison, favoring harder cases. This is a small selected suite, not a representative sample of all development work or an official SWE-bench leaderboard score. Do not tune on these cases and present the same score as independent held-out evaluation.

One frozen fmt pass-to-pass test is excluded due to a historical Rosetta allocation failure; the exact exclusion remains in the manifest. It is not silently removed from the test definition. New tasks or changed exclusions require a new suite version.

## Execution boundary and result privacy

Agent and evaluator checkouts are separate. Non-Python acceptance evaluation uses pinned Docker images with network disabled. Python evaluation and the Pi agent run as the current user: separate workspaces are **not** an operating-system security sandbox. Use a disposable VM/user for public or untrusted agent runs; don't run against your personal home directory or production server. The repository and hidden reference/test files are available to the host, so the current local harness is not an adversarial contest sandbox.

Raw artifacts include prompts, transcripts, candidate patches, endpoint details and logs. They are retained locally, never automatically uploaded. Review/redact raw artifacts before publishing. Normalized exports omit prompts, response bodies, arbitrary error text and authentication configuration. The package does not promise that an agent with shell access cannot read environment credentials.

## Development

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m pip wheel . --no-deps --wheel-dir dist
```

Tests cover fixture integrity, prerequisite diagnostics, native Windows code paths, UTF-8, real owned-child timeout cleanup, strict evaluation, streamed output, credential references, unknown measurements, error classification, reports and comparison. Set `CODING_BENCHMARK_PI_SMOKE=1` after setup to include the real pinned Pi test against a local synthetic HTTP server; it uses no paid provider. CI is configured for macOS/Windows/Linux, but a configured matrix is not evidence those jobs have run. Full evaluator and live 15-task qualification remain release gates.

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), `licenses/`, and `docs/source-provenance.json` for attribution and extraction history. The runner/reporting code is MIT; upstream task content keeps its original license. This package does not relicense container images or transitive dependencies.

Python evaluator bootstrap/runtime dependencies are version- and SHA256-locked in `assets/python-runtime`. The pinned task repository installs without dependency resolution or isolated build downloads. The run records the lock identity and installed packages. These new locks still require all six Django reference checks in the clean-install validation phase; locking alone does not prove task compatibility.
