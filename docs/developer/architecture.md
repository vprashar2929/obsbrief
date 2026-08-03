# Architecture

## High-Level Components

```mermaid
flowchart LR
    CLI[opsbrief CLI] --> CFG[Config Loader]
    CLI --> BRAND[Brand Profile Loader]
    CLI --> AUTH[Auth Resolver]
    CLI --> ORCH[Collector Orchestrator]
    CFG --> ORCH
    AUTH --> ORCH
    ORCH --> C1[Preflight]
    ORCH --> C2[GKE Inventory]
    ORCH --> C3[Kubernetes Health]
    ORCH --> C4[Prometheus Monitoring]
    ORCH --> C5[Logging]
    ORCH --> C6[Audit]
    ORCH --> C7[Network]
    ORCH --> C8[Mesh]
    ORCH --> C9[Trend Metrics]
    ORCH --> C10[Backup]
    ORCH --> C11[Services]
    C1 --> MODEL[Report Model]
    C2 --> MODEL
    C3 --> MODEL
    C4 --> MODEL
    C5 --> MODEL
    C6 --> MODEL
    C7 --> MODEL
    C8 --> MODEL
    C9 --> MODEL
    C10 --> MODEL
    C11 --> MODEL
    MODEL --> REPORTING[reporting.py]
    REPORTING --> CHARTS[charting.py]
    REPORTING --> HTML[html_report.py]
    REPORTING --> PDF[pdf_report.py]
    BRAND --> REPORTING
    BRAND --> HTML
    BRAND --> PDF
    REPORTING --> OUT[Markdown/JSON + Evidence/Charts]
    CHARTS --> OUT
    HTML --> OUT
    PDF --> OUT
```

## Weekly Command Execution

```mermaid
sequenceDiagram
    autonumber
    participant U as User
    participant CLI as opsbrief.cli
    participant COL as collectors
    participant GCP as GCP APIs
    participant K8S as Kubernetes API
    participant BRAND as Brand Profile
    participant REP as reporting/html_report/pdf_report
    U->>CLI: opsbrief weekly --config ... --brand-profile ...
    CLI->>CLI: load config + apply runtime overrides
    CLI->>BRAND: load optional brand profile
    CLI->>COL: run enabled collectors
    COL->>GCP: read-only API requests
    COL->>K8S: read-only cluster requests
    GCP-->>COL: responses/errors
    K8S-->>COL: responses/errors
    COL-->>CLI: CheckResult[]
    CLI->>REP: build report + write artifacts with brand profile
    REP-->>U: local files (md/html/pdf/json/evidence)
```

## Replay Command Execution

```mermaid
sequenceDiagram
    autonumber
    participant U as User
    participant CLI as opsbrief.cli
    participant JSON as Previous Report JSON
    participant BRAND as Brand Profile
    participant REP as reporting
    U->>CLI: opsbrief weekly --from-report-json ... --brand-profile ...
    CLI->>JSON: read collected report model
    CLI->>BRAND: load optional brand profile
    CLI->>REP: re-render artifacts from stored report data
    REP-->>U: local files (md/html/pdf/json/evidence)
```

Replay mode does not invoke the auth resolver, collectors, GCP APIs, or Kubernetes API.
It is intended for iterating on report layout/branding from already collected data.

## Design Notes

- Current provider support is GCP-first.
- Generic usage is achieved through runtime scoping (project/region/cluster args) and config-driven behavior.
- `reporting.py` assembles deterministic Markdown sections and writes artifacts.
- `charting.py` renders deterministic PNG charts from stored time-series evidence.
- `html_report.py` converts generated Markdown to branded HTML and owns print layout rules.
- `pdf_report.py` renders the branded HTML document to PDF with WeasyPrint.
- Client-specific styling is achieved through brand profile YAML, not collector logic.
- No organization-specific decision logic is required for core collectors.
