# Developer Guide

This guide is for contributors extending collectors, reporting, or CLI behavior.

## Project Goals

- Keep collection read-only.
- Keep report output grounded in observed state.
- Keep runtime scope configurable (project/region/cluster overrides).
- Keep collector design modular and testable.

## Local Dev

```bash
hatch env create
hatch run lint
hatch run typecheck
hatch run test
```

Or use the Makefile wrappers:

```bash
make env lint typecheck test
make check          # lint + typecheck + test in one step
make clean          # remove build artifacts and caches
```

## Package Layout

- `src/opsbrief/cli.py`: command parsing + run orchestration
- `src/opsbrief/config.py`: YAML config loading and normalization
- `src/opsbrief/branding.py`: client brand profile loading and validation
- `src/opsbrief/collectors/`: collector implementations
- `src/opsbrief/reporting.py`: report artifact writing and Markdown section assembly
- `src/opsbrief/charting.py`: deterministic PNG chart rendering from stored time-series data
- `src/opsbrief/html_report.py`: HTML rendering from generated Markdown and brand profile
- `src/opsbrief/pdf_report.py`: WeasyPrint PDF rendering from the branded HTML document
- `src/opsbrief/models.py`: report and collector result models, including JSON replay
- `src/opsbrief/gcp_auth.py`: auth mode handling
- `tests/`: unit tests with mocking

## Collector Contract

Each collector returns `CheckResult` with:

- `collector`
- `status`
- `summary`
- `details`
- `errors`

Collectors should:

- do read-only API calls,
- avoid side effects,
- return structured evidence in `details`,
- map API failures to explicit statuses.

## Add a New Collector

1. Implement `collect(...)` in `src/opsbrief/collectors/<name>.py`.
2. Register it in `src/opsbrief/collectors/__init__.py`.
3. Render a Markdown section in `src/opsbrief/reporting.py` if needed.
4. Add focused unit tests in `tests/test_collectors.py`.

See [architecture.md](architecture.md) for flow diagrams.
See [library-strategy.md](library-strategy.md) for guidance on what should use
OSS libraries versus what should remain project-specific.

## Reporting Changes

Reporting code should remain deterministic and local-only. Brand behavior comes from
`BrandProfile`, not organization-specific branches.

- Put report content changes in `reporting.py`.
- Put stable Markdown layout shells in `src/opsbrief/templates/*.md.j2`; keep
  collector interpretation and table row construction in `reporting.py`.
- Put generated time-series chart rendering changes in `charting.py`.
- Put browser/HTML styling changes in `html_report.py`.
- Put print layout, page-break, logo-clearance, and table-flow changes in `html_report.py`.
- Keep `pdf_report.py` focused on invoking WeasyPrint with the correct base URL.

When changing PDF rendering, preserve logo clear space, keep tables readable in paged
media, and avoid stranding main section headings at page breaks. Local macOS PDF
generation requires WeasyPrint's native GLib/Pango stack, available with
`brew install pango`.
