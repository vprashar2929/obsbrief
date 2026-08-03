# OpsBrief

`opsbrief` is a local, read-only CLI that collects infrastructure evidence and produces weekly operational reports.

It is designed as a generic OSS-style tool for any GCP environment, not tied to a single organization.
Runtime scope is supplied through the normal config/CLI flags, while report branding is
supplied separately with an optional brand profile YAML.

## What It Does

- Runs read-only collectors across GCP + GKE.
- Produces state-only reports (no remediation actions, no automatic recommendations).
- Exports artifacts as Markdown, HTML, PDF, and JSON for sharing.

Implemented collectors:

- `preflight`
- `gke_inventory`
- `kubernetes_health`
- `prometheus_monitoring`
- `logging`
- `audit`
- `network`
- `mesh`
- `trend_metrics`
- `backup`
- `services`

## Quick Start

1. Authenticate to GCP. `opsbrief` resolves credentials via `--auth-mode auto` by default, which uses Application Default Credentials and falls back to the active `gcloud` user token, so you must have a valid `gcloud` session before running the tool:

```bash
gcloud auth login
gcloud auth application-default login
```

If neither ADC nor an active `gcloud` token is available, the tool exits with an auth error (see `src/opsbrief/gcp_auth.py`). Use `--auth-mode impersonation --impersonate-service-account <sa>` instead when running under a service account.

2. Create a Hatch environment:

```bash
hatch env create          # or: make env
```

PDF generation uses WeasyPrint. On macOS, install its native GLib/Pango dependency with
`brew install pango`.

3. Run preflight:

```bash
hatch run opsbrief preflight \
  --config config/runtime.yaml \
  --env dev \
  --project example-dev-project \
  --region us-central1

# or: make preflight PROJECT=example-dev-project REGION=us-central1
```

4. Generate report:

```bash
hatch run opsbrief weekly \
  --config config/runtime.yaml \
  --env dev \
  --project example-dev-project \
  --region us-central1 \
  --brand-profile config/brand.example.yaml \
  --output-dir ./reports

# or: make weekly PROJECT=example-dev-project REGION=us-central1 OUTPUT_DIR=./reports
```

`config/brand.example.yaml` is a ready-to-run neutral sample theme: it includes an original
OpsBrief mark, a provider-neutral palette, report motifs, and chart colors. Copy it before
adding your own organization name, logo, fonts, or colors. It does not change collector scope or
make cloud calls.
Use `config/examples/auto-discovery.yaml` or `config/examples/explicit-clusters.yaml`
when you want a committed example config instead of runtime-only CLI overrides.

5. Regenerate report artifacts from a previous collection without calling GCP again:

```bash
hatch run opsbrief weekly \
  --from-report-json ./reports/dev/weekly/2026-W20/opsbrief-dev-weekly-report.json \
  --brand-profile config/brand.example.yaml \
  --output-dir ./reports
```

## Output Artifacts

Reports are written under:

`reports/<env>/weekly/<iso-year>-W<iso-week>/`

- `opsbrief-<env>-weekly-report.md`
- `opsbrief-<env>-weekly-report.html`
- `opsbrief-<env>-weekly-report.pdf`
- `opsbrief-<env>-weekly-report.json`
- `evidence/collector-status.json`
- `evidence/collectors/*.json`
- `evidence/charts/*.png` when the stored collector evidence includes chartable time series

Preflight evidence:

- `reports/<env>/preflight/opsbrief-<env>-preflight.json`

## Sample Report

Review the generated, fully anonymized [sample report](docs/examples/sample-report.md), or replay
its [JSON fixture](examples/sample-report.json) locally with the bundled neutral brand profile.
The fixture makes no cloud calls and is useful for evaluating report layout before connecting a
real environment.

## Runtime Flags

- `--project` (repeatable): project scope for discovery + collectors
- `--region`: default region for discovery/shorthand clusters
- `--cluster` (repeatable): `NAME` or `PROJECT/REGION/NAME`
- `--trend-days`: trend window override (`1..365`)
- `--auth-mode`: `auto|adc|metadata|impersonation`
- `--impersonate-service-account`: required for impersonation mode
- `weekly --brand-profile <path>`: YAML brand profile for report title, colors, and font
- `weekly --include-evidence-index`: include the Evidence Index section in rendered reports
  (disabled by default; evidence files are still written)
- `weekly --from-report-json <path>`: re-render Markdown/HTML/PDF/JSON/evidence from a
  previous weekly report JSON without running collectors
  (`--config` is optional in replay mode and only needed for report options such as
  `reporting.include_evidence_index` or `reporting.include_collector_warnings_and_gaps`)

## Scope and Principles

- Read-only evidence collection only.
- Local artifact generation only (no bucket/slack/email writes).
- Current provider scope: GCP APIs + Kubernetes API.
- Auto-discovery enabled by default for clusters, compute instances, and load balancers.
- GKE monitoring/alerting scope is Prometheus/Grafana only; Cloud Monitoring alert-policy
  assessment is out of scope for posture reporting.

## Documentation

- User guide: [docs/user/README.md](docs/user/README.md)
- User configuration: [docs/user/configuration.md](docs/user/configuration.md)
- Developer guide: [docs/developer/README.md](docs/developer/README.md)
- Architecture details: [docs/developer/architecture.md](docs/developer/architecture.md)
- Contributing guide: [CONTRIBUTING.md](CONTRIBUTING.md)
- Security policy: [SECURITY.md](SECURITY.md)

## Notes

- If ADC is unavailable, `--auth-mode auto` falls back to active `gcloud` credentials.
- Kubernetes checks for private endpoints require network reachability to cluster APIs.
- `config/runtime.yaml`, `config/brand.example.yaml`, and `config/examples/*.yaml` are generic starting points.
- Keep organization-specific runtime configs, brand assets, and report artifacts outside this public repository.
