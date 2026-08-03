# Contributing

Thanks for considering a contribution to `opsbrief`.

`opsbrief` is a read-only operational reporting CLI. Contributions should preserve that boundary:
collectors may read from GCP, GKE, or local config, but they must not mutate cloud resources or
write to external destinations.

## Development Setup

Use Hatch for local workflows:

```bash
hatch env create
hatch run lint
hatch run typecheck
hatch run test
```

Or use the Makefile wrappers:

```bash
make env lint typecheck test
make check                     # lint + typecheck + test in one step
make clean                     # remove build artifacts and caches
```

Run focused tests while developing, then run the full suite before opening a pull request.

## Contribution Guidelines

- Keep changes small and single-purpose.
- Prefer existing collector, config, and reporting patterns over new abstractions.
- Add or update tests for behavior changes.
- Mock GCP and Kubernetes clients in tests; do not require live infrastructure.
- Do not commit credentials, customer identifiers, private hostnames, or generated reports.
- Keep the CLI read-only. Do not add remediation, deletion, update, or notification side effects.

## Pull Requests

Pull requests should include:

- What changed and why.
- Any config or report output impact.
- Test evidence, usually `make check` (or individually: `hatch run lint`, `hatch run typecheck`, `hatch run test`).
- Screenshots or artifact excerpts for report rendering changes when useful.

Commit messages should follow Conventional Commits, for example:

```text
feat: add cloud sql backup evidence
fix: handle empty cluster discovery response
docs: clarify brand profile configuration
```
