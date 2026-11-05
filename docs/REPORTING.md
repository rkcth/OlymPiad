# Reporting contract

## Destinations

`run`, `sweep` and `report` accept `--output -` (stdout, default) or `--output NEW_FILE`. A file is opened exclusively and is never silently truncated. Events are serialized under a write lock and flushed after every line. `tee` provides stdout and a file together without a third product mode.

Plain text is the default. It contains one timestamped event per line, per-task outcomes and a final summary. Progress reflects task completion, not token-by-token model output. Raw agent transcripts are separate local artifacts.

## NDJSON

`--format ndjson` is the machine-readable alternative. Each UTF-8 line is an entire JSON object, terminated by a newline. There is no top-level array or closing delimiter to wait for. A downstream process can consume events incrementally using standard line-based tools.

Every event includes:

```json
{"schema_version":"1.0","timestamp":"2026-01-01T00:00:00+00:00","event":"task_finished","message":"tasks 1/15 example: pass","stage":"tasks","completed":1,"total":15,"task":"example","status":"pass"}
```

This is a schema illustration, not a real benchmark result. Other event types include `plan`, `prerequisite`, `error`, `blocked`, `interrupted`, `doctor_complete`, `preflight_complete`, `task_result`, `run` (offline rendering), and `complete`.

`complete` signals that a run reached its normal final report. Inspect its counts and summary: completion is not a perfect score. Preflight-only execution ends with `preflight_complete`, not a scored-completion claim. Treat an abruptly truncated stream as incomplete. With parallel workers, task completion order can differ from manifest order; `task` identifies the case.

## Result distinctions

- `pass`: acceptance evaluation passed.
- `fail`: acceptance evaluation ran and failed correctness checks.
- `timeout`: agent exceeded its time limit.
- `infrastructure_error`: execution/evaluation failed for an infrastructure reason.
- `not_run`: no scored outcome is available.

Normalized offline results preserve missing measurements as `null`. They expose both pass/all-tasks and pass/evaluated-task rates, with denominators retained. Report timeouts and infrastructure failures alongside either rate.

Timing rates from Pi events are client-observed estimates. They must not be presented as engine-internal prefill/decode measurements. Absent usage counts are null, not zero. Aggregate token totals include measured tasks only, with per-field measured-task counts retained in the raw suite summary.

## Optional exports

`export` writes JSON, CSV, Markdown, standalone HTML and JUnit into a new directory. These are offline views of retained evidence. JUnit records correctness failures as `failure`, timeouts/infrastructure failures as `error`, and missing outcomes as `skipped`.

`compare` emits JSON/CSV/Markdown. A retained manifest hash must match the recorded hash. Equal task-suite identity alone does not certify comparable inference configuration; missing or changed configuration is explicitly descriptive-only. Compare hardware and provider policies separately.

No network callbacks, telemetry or uploads occur when reporting/exporting/comparing. Live webhooks can be implemented by an external reader of the NDJSON stream; this release does not send them automatically.

Capacity sweep streams have their own [documented events and measurements](SWEEP.md), separate from scored suite results. A sweep can finish without all levels stabilizing; inspect `complete` and exit status rather than assuming the presence of a final event implies success.
