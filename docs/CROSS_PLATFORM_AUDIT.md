# Cross-platform and first-run audit

> **Naming note:** this project was originally developed as "Coding Benchmark" (package `coding_benchmark`, command `coding-benchmark`) and is now named OlymPiad (package and command `olympiad`). The validation records in `docs/validation*` and `docs/validation-*.json` are historical and keep the earlier names and file paths; their recorded hashes describe the files as validated then.

Audit date: 2026-10-31. Scope: source/wheel installation, prerequisites, pinned agent adapter, server configuration, execution lifecycle, Unicode, reporting, and public-release claims.

**Verdict: substantial portability defects fixed; not yet certified as a fully qualified public release on every OS.** Source/package checks and clean-install release qualification are separate phases. At the owner’s direction, clean installs from the eventual GitHub repository on macOS, Windows and Linux follow this audit. The remaining release gates are listed below. A successful package/adapter test is not a successful 15-task benchmark run.

## Findings and corrections

| Finding | Correction | Evidence / limit |
|---|---|---|
| Missing Git, Bash, Docker, Node or Pi could fail late or produce a traceback | `doctor` reports all missing prerequisites with fixes; `run` enforces checks before task cloning, image pulls or model requests | Mocked missing-tool tests plus actual unavailable-Docker diagnosis on macOS |
| Docker CLI presence did not prove the daemon was usable | Check engine availability, Linux-container mode, access permissions and memory | Tests cover missing, stopped, permission-denied and Windows-container cases |
| Arbitrary/uninstalled global Pi | User-local `setup --install-pi`, exact Pi 1.4.0 and npm transitive integrity lock; no global npm or system changes | Actual clean installation and real Pi adapter tests on macOS/Linux |
| Native Windows cannot use Unix `killpg`/`SIGKILL` | Own process groups; Windows `taskkill /T` and Ctrl-Break handling; controller stops its task runners | Actual POSIX child-process timeout test and Windows branch tests; native Windows execution still pending |
| Virtualenv assumed `bin/python` | Windows uses `Scripts/python.exe`; compatible evaluator interpreter is detected separately | Windows-path regression; locked Django evaluator requires Python 3.11 |
| Windows `.cmd` shim quoting could corrupt prompts/paths | Launch managed Pi as `node` plus its literal JS path; locate npm's JS entrypoint on Windows | No prompt is passed through `cmd.exe`; native Windows CI configured |
| Windows argument-length limits can reject long issue prompts | Feed prompts to Pi through UTF-8 stdin rather than command-line arguments | Actual pinned Pi test hashes the entire long Unicode payload after Pi's outer-whitespace trimming |
| Default host encodings could break task prompts, Unicode names and reports | Explicit UTF-8 file/process/report I/O | Unicode directory and model-name tests |
| Existing personal Pi/project configuration could affect runs | Isolated agent directory and flags disabling project approval/resources/extensions/skills/MCP discovery | Verified against actual pinned Pi help and actual synthetic completion |
| Killing a Docker client can leave its evaluator container running | Unique owned container name and scoped cleanup, without touching other jobs | Source correction; Docker-backed timeout test still requires an available engine |
| Cancellation could leave running worker processes or continue queued work | Registered own workers, cancellation flag, task-runner cleanup, cancelled queued futures | POSIX timeout proof; complete live cancellation/evaluator qualification remains a gate |
| Server setup required repeatedly entering flags | Non-secret `configure` file, CLI overrides, optional metadata-only server check | Config and precedence tests; no credential values saved |
| Auth errors could be presented as code-quality failures | Provider/protocol errors are infrastructure errors | Authentication/error classification regressions |
| Reasoning was forced on every generic server/model | Default thinking is off; reasoning metadata follows the explicit thinking setting | Plain/reasoning configuration regression; runtime parameters remain caller-configurable |
| Missing usage could look like measured zero | Null absent counts and measured-task aggregate counts | Missing-usage regression and real provider-event parser test |
| Model-discovery redirect could forward credentials to another host | Same-origin-only metadata redirects, bounded/validated model listing | Cross-origin negative regression; Node provider generation behavior remains upstream |
| Source archive omitted documentation/CI files | Explicit distribution manifest for docs, notices, contribution instructions and workflow | Archive/wheel inspection during packaging |
| Old bundled pip could mishandle modern source-package metadata | First-run instructions upgrade pip inside the dedicated virtualenv; distribute a built wheel too | Modern isolated build/install verification; no global Python changes |
| Cross-host comparisons could overlook runtime differences | Retain host, Node, Pi lock and evaluator Python identity; record per-task Python package versions | Changed recorded host is flagged; Python 3.11 bootstrap/runtime wheels are SHA256-locked; reference behavior remains to be qualified |
| Platform testing only existed on Linux CI configuration | macOS, Windows and Linux matrix plus real pinned Pi/synthetic endpoint test | Workflow prepared; not represented as executed Windows evidence |

## Actual verification versus planned coverage

| Environment | What has actually run | What has not been proved |
|---|---|---|
| macOS arm64, Python 3.9.6, Node 22.23.2 | Package/CLI/report tests, locked Pi install in a path with spaces, real Pi-to-loopback OpenAI request/stream/usage/auth, Unicode paths, owned-child timeout, Docker-stopped diagnostic | Full frozen evaluator preflight/scored run; Docker engine was stopped and was not started by the audit |
| Linux x86-64, Python 3.14.4, Node 24.21.0 | Isolated temporary source install and real locked Pi/synthetic-endpoint test, package/CLI/report/process regressions | Full frozen evaluator preflight/scored run; Python 3.14 is the controller test interpreter, not a certified Django evaluator |
| Native Windows | Static correction and branch-level regressions for data paths, virtualenv paths, npm/Pi invocation and process cleanup; CI workflow defined | No native Windows machine/CI job has executed this candidate yet. Do not claim Windows certification |
| WSL2 / other ARM environments | Documented installation approach and architecture warnings | No separate WSL2 or non-macOS ARM qualification |

The Python 3.9 macOS entry above is historical v0.2 evidence; v0.3 requires Python 3.11+. See `docs/validation-summary.json` for exact recorded test counts, source hashes and evidence scope. Raw temporary test artifacts are not public model-performance results.

## First-run experience

1. Download/extract the source or install the supplied wheel in a Python virtualenv. Python 3.11+ is required for the tool; Python 3.11 is required for Django evaluation.
2. Install Git, Node.js 22.19+ and Docker through their official OS installers/package manager. Native Windows also needs Git Bash and Linux-container mode.
3. Run `olympiad setup --install-pi` to install the integrity-locked user-local agent runtime.
4. Run `olympiad doctor`; fix errors. No image downloads, model calls or service starts occur during ordinary doctor checks.
5. Run `configure` for the server URL/model, optionally `doctor --check-server`, then `run --config benchmark.json --pull-images`.

Doctor checks prerequisite readiness, not correctness of all 15 reference tests or generation compatibility. The actual benchmark performs differential baseline/reference validation before model requests. The tool never installs Docker or changes socket permissions automatically.

## Next phase: clean GitHub installs and release qualification

- Execute the configured native Windows jobs, including real managed Pi, Unicode paths and process-tree timeout behavior. Fix actual findings; static mocks are not equivalent evidence.
- Execute all 15 baseline/reference evaluator checks from the installed distribution with an available Linux-container Docker engine, including the chosen native Windows/WSL workflow.
- Complete a controlled full 15-task endpoint run; verify artifacts, both streaming destinations, interruption cleanup and score classification from the installed wheel.
- Validate Docker-backed timeout cleanup against an actual engine. Do not claim that killing only its CLI removes a container.
- Publish actual supported OS/architecture and resource requirements. Pinned evaluator images are linux/amd64; ARM needs emulation and is not a native-performance comparison.
- Qualify the newly hash-locked Python 3.11 evaluator environment against all six Django baseline/reference tasks. No unbounded pip dependency or build-tool resolution remains in evaluator setup.
- Review external evaluator-image and bundled test-data licenses separately from the included top-level project notices. Docker Desktop terms are separate from this tool's MIT license.
- Select the public repository/package name and run its CI before publishing. No GitHub or PyPI publication was performed by this audit.

The local agent and Python evaluator still run with the caller's OS permissions. This is not an adversarial contest sandbox. Raw logs/transcripts can contain sensitive data; only deliberately reviewed artifacts should be published. Use a disposable user/VM for untrusted agent execution.
