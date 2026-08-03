# Repository Guidelines

## Project Structure & Module Organization

Core code lives in `src/opsbrief/`. Entry-point orchestration is in `src/opsbrief/cli.py`, shared models/status/config handling are in sibling modules, and collectors are split by domain in `src/opsbrief/collectors/*.py` (for example `network.py`, `mesh.py`, `backup.py`).
Tests are under `tests/` with focused unit suites such as `test_collectors.py` and `test_reporting.py`.
Runtime configuration examples are in `config/*.yaml` (`runtime.yaml` is the main baseline).
Reference docs are in `docs/user/` and `docs/developer/`.

## Build, Test, and Development Commands

Use Hatch for all local workflows (or the equivalent `make` targets):

- `hatch env create` / `make env`: create the dev environment.
- `hatch run lint` / `make lint`: run Ruff lint checks.
- `hatch run fmt` / `make fmt`: format code with Ruff formatter.
- `hatch run typecheck` / `make typecheck`: run strict MyPy over `src/`.
- `hatch run test` / `make test`: run the Pytest suite.
- `make check`: run lint + typecheck + test in one step.
- `make clean`: remove build artifacts and caches.
- `hatch run opsbrief preflight --config config/runtime.yaml --env dev --project <id> --region us-central1`: run a read-only preflight (or `make preflight PROJECT=<id> REGION=us-central1`).
- `hatch run opsbrief weekly --config config/runtime.yaml --env dev --project <id> --region us-central1 --output-dir ./reports`: generate weekly report artifacts (or `make weekly PROJECT=<id> REGION=us-central1 OUTPUT_DIR=./reports`).

## Coding Style & Naming Conventions

Target runtime is Python 3.11. Use 4-space indentation and keep lines within 100 chars (Ruff setting).
Follow Ruff rules configured in `pyproject.toml` (`E`, `F`, `I`, `UP`, `B`) and run formatter before opening a PR.
MyPy runs in strict mode: new/changed functions should be fully typed.
Use `snake_case` for modules/functions/variables, `PascalCase` for classes, and keep collector filenames aligned to collector names.
Apply the Single Responsibility Principle: each module/function should have one clear purpose.
Avoid overengineering and avoid speculative behavior; implement only requirements grounded in observable repo context.

## Testing Guidelines

Testing uses Pytest. Name files `tests/test_*.py` and tests `test_*`.
Add/extend unit tests for every behavior change, including failure/status mapping paths for collectors.
Prefer mocking external GCP/Kubernetes clients to keep tests deterministic and local.
When making changes, include tests in the same PR; use mocks whenever possible so tests do not depend on a live environment.
Run `hatch run test` before commit; use targeted runs during development (example: `pytest tests/test_collectors.py -k network`).

## Commit & Pull Request Guidelines

Current history shows Conventional Commit style (`feat: ...`) plus merge commits. Prefer `<type>: <short imperative summary>` (for example `fix: handle empty cluster list`).
Commit messages must follow the Conventional Commits 1.0.0 standard: `https://www.conventionalcommits.org/en/v1.0.0/`.
Keep commits small and single-purpose.
PRs should include scope, reasoning, test evidence (`hatch run test` output), and linked issue/task.
For report/output changes, include a sample artifact path or screenshot/excerpt to make review faster.

## Security & Configuration Tips

This CLI is read-only by design; do not introduce mutating cloud operations.
Do not commit credentials or environment-specific secrets. Use ADC/gcloud auth and runtime flags for project/region/cluster scoping.
