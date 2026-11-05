# Contributing

Use a new suite version for any task, patch, test, exclusion or evaluator-image change. Do not edit frozen cases to improve a model's score. Keep negative and interrupted outcomes visible and retain independent agent/evaluator workspaces.

Use Python 3.11+ for the tool and exactly Python 3.11 for Django evaluation. Bootstrap/runtime wheels are version/hash-locked; changes require provenance updates and all Django reference checks.

Run the offline tests before proposing changes:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Changes to scoring require regressions for missing/skipped tests, false success, timeout, infrastructure failure, candidate test modification and reference validation. Streaming changes require proof that consumers can read each complete event before the next one exists. Model-specific APIs belong in adapters, not source-suite edits.

Before the first public release: execute baseline/reference preflight and a complete controlled endpoint run from the installed wheel, run the real native Windows CI/test matrix, document OS/architecture support, and review transitive image/test-data licensing. Managed Pi 1.4.0 is pinned with a transitive npm lock. Use synthetic reports only for reporting/adapter tests and label them as such. Never publish synthetic scores as measured model performance.

Do not commit endpoint credentials, `.env` files or raw private run artifacts. CI may download locked Pi dependencies and use a loopback simulated OpenAI server, but must not need real provider credentials or start a paid model benchmark. Keep Docker/evaluator qualification separate from package/adapter tests.

The next validation phase is a fresh download/install from the GitHub repository on macOS, native Windows and Linux. Do not substitute source-tree tests or mocked Windows branches for those clean installs.
