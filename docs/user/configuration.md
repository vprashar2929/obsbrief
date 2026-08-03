# User Configuration

`opsbrief` uses two YAML inputs:

- Runtime config (`--config`): environment scope, collectors, discovery, services, and
  report options.
- Brand profile (`--brand-profile`): client-facing report title, logo, font, and colors.

Keeping these separate lets you regenerate the same collected evidence with a different
brand profile without running collectors again.

## Recommended Start Point

Use `config/runtime.yaml` and set target scope at runtime:

```bash
hatch run opsbrief weekly \
  --config config/runtime.yaml \
  --env dev \
  --project example-dev-project \
  --region us-central1
```

For committed, fully expanded examples, start from
`config/examples/auto-discovery.yaml` or `config/examples/explicit-clusters.yaml` and
replace placeholder projects, regions, clusters, and service settings with your
environment values.

## Core Config Shape

```yaml
environment: dev
default_region: us-central1
timeout_seconds: 45
projects:
  primary: example-dev-project
collectors:
  preflight: true
  gke_inventory: true
  kubernetes_health: true
  monitoring: false
  prometheus_monitoring: true
  logging: true
  audit: true
  network: true
  mesh: true
  trend_metrics: true
  backup: true
  services: true
clusters: [] # optional; empty = auto-discovery
services:
  cloud_sql: true
  redis: true
  managed_kafka: true
  utilization_thresholds: {}
  elasticsearch_backup_checks: []
  mesh_api_proxies: []
  compute_instances: []
  load_balancers: []
network:
  required_service_fqdns: []
  required_internal_zones: []
time_windows:
  trend_days: 7
discovery:
  auto_discover_clusters: true
  include_discovered_clusters: false
  auto_discover_compute_instances: true
  auto_discover_load_balancers: true
reporting:
  include_evidence_index: false
  include_collector_warnings_and_gaps: false
prometheus:
  url: https://prometheus.example
  token_env: "" # optional bearer token env var
  labels:
    environment: dev
    cluster:
      - dev-cluster
```

## CLI Overrides

- `--project` (repeatable): override config project scope
- `--region`: override default region
- `--cluster` (repeatable):
  - `NAME`
  - `PROJECT/REGION/NAME`
- `--timeout-seconds`: override `timeout_seconds` from config for each collector
  subprocess and downstream API calls
- `--trend-days`: override trend window (`1..365`)
- `weekly --brand-profile`: YAML brand profile for report title, colors, and font
- `weekly --from-report-json`: rebuild report artifacts from a previous weekly report
  JSON without running collectors or calling GCP/Kubernetes APIs
- `weekly --include-evidence-index`: include the rendered Evidence Index section
  (disabled by default; evidence files are still written)

`--config` is required for live `weekly` runs. It is optional with
`weekly --from-report-json`; pass it in replay mode only when you want config-backed
report options such as `reporting.include_evidence_index`.

`timeout_seconds` defaults to `45` when omitted. Increase it for environments where
long-running collectors such as audit log review or 7-day trend metrics regularly exceed
the default. Command-line `--timeout-seconds` takes precedence for a single run.

## Mesh API Proxy Checks

Use `services.mesh_api_proxies` for configured mesh API proxies that should appear in
the Service Mesh Health section. This supports legacy Squid-style proxies and
NGINX-style Istio API proxies without hard-coding environment assumptions. Existing
`services.squid_proxies` entries are still accepted for backward compatibility, but new
configs should use `mesh_api_proxies`.

## Prometheus Monitoring

Use `collectors.prometheus_monitoring` when a read-only Prometheus API endpoint is an
approved monitoring source. The collector queries Prometheus HTTP API endpoints only and
does not mutate dashboards, alert rules, or monitored workloads.

Always scope queries with `prometheus.labels` or explicit clusters so one environment
report does not include another environment's series:

```yaml
collectors:
  prometheus_monitoring: true
prometheus:
  url: https://prometheus.example
  token_env: "" # set to an env var name if bearer auth is required
  labels:
    environment: dev
    cluster:
      - dev-cluster
```

The collector records HPA condition series, scrape target health, and selected
Prometheus range series used for report charts. The range-series charts are rendered from
stored report JSON into `evidence/charts/*.png` during Markdown/HTML/PDF generation.
It reports unavailable or empty query results as such; it does not infer Grafana
dashboard state from unrelated metrics.

## Brand Profile

Use `config/brand.example.yaml` as a ready-to-run, provider-neutral sample theme. It uses an
original OpsBrief mark and a neutral palette, rather than a cloud-provider or design-system
logo. Copy it before changing it for your organization. The brand profile is intentionally
structured YAML rather than free-form PDF input, so generated reports use explicit colors and
labels without guessing.

```yaml
organization_name: OpsBrief
report_title: OpsBrief Weekly Operational Report
logo_path: sample-assets/opsbrief-mark.svg
logo_alt: OpsBrief sample mark
logo_width_mm: 18
font_family: "Arial, Helvetica, sans-serif"
font_faces: []
colors:
  primary: "#17324d"
  secondary: "#244d6d"
  accent: "#0f8b8d"
  background: "#f6f8fb"
  surface: "#ffffff"
  text: "#152536"
  muted: "#536779"
  border: "#d7e0e8"
  header_background: "#e9f2f5"
  row_alternate: "#f8fafc"
  code_background: "#edf3f6"
report_theme:
  theme_lines:
    enabled: true
    count: 6
    color: "#7fd1c9"
  corner_motif:
    enabled: true
    fill: "#17324d"
    border: "#7fd1c9"
  status_colors:
    ok: "#18794e"
    warning: "#a56300"
    critical: "#b3261e"
    scoped: "#52616b"
  chart_palette:
    - "#0f8b8d"
    - "#3d6a99"
    - "#8b5e83"
    - "#b06d28"
```

Logo and font paths are resolved relative to the brand YAML file. `font_family` controls
the CSS font stack; `font_faces` is optional and embeds approved local font files into
HTML/PDF output so rendering does not depend on fonts installed on the workstation.
`report_theme` is optional report presentation metadata: motif toggles, semantic status
colors, and chart series palette. Use `config/brand.example.yaml` as the public starting
point, then copy it before adding organization-specific profiles, logos, fonts, or brand
guidelines. Keep those private artifacts outside this public repository.

The tool does not parse or infer rules from a brand guideline PDF. Encode approved
values in the brand profile YAML and store logo assets beside that YAML or use absolute
paths.

## Reporting Options

`reporting.include_evidence_index` controls only the rendered Evidence Index section in
Markdown/HTML/PDF. Evidence files such as `evidence/collector-status.json`,
`evidence/collectors/*.json`, and generated `evidence/charts/*.png` are still written
when this setting is `false`.

`reporting.include_collector_warnings_and_gaps` controls whether the rendered report
includes the `Collector Warnings And Gaps` section. It is disabled by default.

## Auth Modes

- `auto`: try ADC, then fallback to active `gcloud` token
- `adc`: enforce Application Default Credentials
- `metadata`: use VM metadata credentials (useful on GCE)
- `impersonation`: use IAM service account impersonation

## Notes

- No cloud write operations are performed.
- Report statuses are derived from observed collector outcomes.
- The report intentionally avoids inferred remediation recommendations by default.
- GKE monitoring/alerting assessment is scoped to Prometheus/Grafana evidence only.
