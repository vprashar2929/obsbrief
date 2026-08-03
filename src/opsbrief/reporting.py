from __future__ import annotations

import json
import math
import re
import shutil
from html import escape
from pathlib import Path
from typing import Any

from jinja2 import Environment, PackageLoader

from opsbrief.branding import BrandProfile, default_brand_profile
from opsbrief.charting import ChartSeries, write_time_series_png
from opsbrief.html_report import render_report_html
from opsbrief.models import CheckResult, Report, Status
from opsbrief.pdf_report import write_pdf_from_html

_UTILIZATION_WARNING_THRESHOLD = 85.0
_NAMESPACE_TOP_VALUE_COUNT = 3
_REPORT_MODE_FULL = "full"
_REPORT_MODE_CONCISE = "concise"
_TREND_ROWS_DISPLAY_LIMIT = 30
_CHART_SERIES_LIMIT = 8
_MARKDOWN_TEMPLATE_ENV = Environment(
    loader=PackageLoader("opsbrief", "templates"),
    autoescape=False,
)


def ensure_report_directory(
    output_dir: str | Path, environment: str, iso_year: int, iso_week: int
) -> Path:
    base = Path(output_dir)
    target = base / environment / "weekly" / f"{iso_year}-W{iso_week:02d}"
    target.mkdir(parents=True, exist_ok=True)
    (target / "evidence").mkdir(parents=True, exist_ok=True)
    return target


def write_report_json(report: Report, report_dir: Path) -> Path:
    json_path = report_dir / f"opsbrief-{report.environment}-weekly-report.json"
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(report.to_dict(), handle, indent=2, sort_keys=True)
        handle.write("\n")
    return json_path


def read_report_json(path: str | Path) -> Report:
    report_path = Path(path)
    with report_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected dict JSON at {report_path}")
    return Report.from_dict(payload)


def write_collector_evidence(report: Report, report_dir: Path) -> Path:
    path = report_dir / "evidence" / "collector-status.json"
    collector_dir = report_dir / "evidence" / "collectors"
    collector_dir.mkdir(parents=True, exist_ok=True)
    for stale_path in collector_dir.glob("*.json"):
        stale_path.unlink()

    payload: dict[str, Any] = {
        "collectors": [item.to_dict() for item in report.collectors],
        "overall_status": report.overall_status.value,
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    for item in report.collectors:
        collector_path = collector_dir / f"{_slug(item.collector)}.json"
        with collector_path.open("w", encoding="utf-8") as handle:
            json.dump(item.to_dict(), handle, indent=2, sort_keys=True)
            handle.write("\n")
    return path


def write_preflight_evidence(result: CheckResult, output_dir: str | Path, environment: str) -> Path:
    base = Path(output_dir)
    target_dir = base / environment / "preflight"
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"opsbrief-{environment}-preflight.json"
    with path.open("w", encoding="utf-8") as handle:
        json.dump(result.to_dict(), handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path


def write_report_markdown(
    report: Report,
    report_dir: Path,
    brand: BrandProfile | None = None,
    *,
    include_evidence_index: bool = False,
    report_mode: str = _REPORT_MODE_FULL,
    include_technical_appendix: bool = False,
    include_collector_warnings_and_gaps: bool = False,
) -> Path:
    markdown_path = report_dir / f"opsbrief-{report.environment}-weekly-report.md"
    chart_refs = _prepare_report_charts(report, report_dir, brand)
    markdown_text = _render_report_markdown(
        report,
        _brand_or_default(brand),
        chart_refs=chart_refs,
        include_evidence_index=include_evidence_index,
        report_mode=report_mode,
        include_technical_appendix=include_technical_appendix,
        include_collector_warnings_and_gaps=include_collector_warnings_and_gaps,
    )
    with markdown_path.open("w", encoding="utf-8") as handle:
        handle.write(markdown_text)
    return markdown_path


def _prepare_report_charts(
    report: Report, report_dir: Path, brand: BrandProfile | None = None
) -> list[dict[str, Any]]:
    _remove_legacy_chart_artifacts(report_dir)
    chart_refs: list[dict[str, Any]] = []
    chart_dir = report_dir / "evidence" / "charts"
    for item in report.collectors:
        if item.collector == "prometheus_monitoring":
            chart_refs.extend(_write_prometheus_time_series_charts(item, chart_dir, brand))
    return chart_refs


def _remove_legacy_chart_artifacts(report_dir: Path) -> None:
    chart_dir = report_dir / "evidence" / "charts"
    if chart_dir.is_dir():
        shutil.rmtree(chart_dir)
    elif chart_dir.exists():
        chart_dir.unlink()


def _write_prometheus_time_series_charts(
    item: CheckResult,
    chart_dir: Path,
    brand: BrandProfile | None = None,
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for block in _as_dict_list(item.details.get("time_series", [])):
        name = str(block.get("name", "")).strip()
        raw_title = str(block.get("title", "")).strip() or name.replace("_", " ").title()
        title = _client_facing_monitoring_text(raw_title)
        series = _chart_series_from_prometheus_block(block)
        if not name or not series:
            continue
        filename = f"monitoring-stack-{_client_facing_monitoring_slug(name)}.png"
        path = chart_dir / filename
        visible_series = series[:_CHART_SERIES_LIMIT]
        unit = str(block.get("unit", ""))
        write_time_series_png(path, visible_series, title=title, y_label=unit, brand=brand)
        if not path.exists():
            continue
        refs.append(
            {
                "collector": item.collector,
                "name": name,
                "title": title,
                "unit": unit,
                "path": f"evidence/charts/{filename}",
                "series_labels": [row.label for row in visible_series],
                "series_count": len(series),
                "sample_count": sum(len(row.samples) for row in visible_series),
                "series_stats": _chart_series_stats(visible_series),
            }
        )
    return refs


def _client_facing_monitoring_text(text: str) -> str:
    placeholder = "__PROMETHEUS_GRAFANA_SCOPE__"
    rendered = text.replace("Prometheus/Grafana", placeholder)
    replacements = (
        ("Down Prometheus Scrape Targets", "Down Monitoring Targets"),
        ("Prometheus Scrape Targets", "Monitoring Targets"),
        ("Prometheus API", "Monitoring source"),
        ("Prometheus reachable", "Monitoring source reachable"),
        ("Collected Prometheus monitoring", "Collected monitoring stack evidence"),
        ("Prometheus monitoring", "monitoring stack"),
        ("Prometheus scope", "Monitoring stack scope"),
        ("down Prometheus targets", "down monitoring targets"),
        ("down Prometheus target", "down monitoring target"),
        ("Prometheus targets", "monitoring targets"),
        ("Prometheus target", "monitoring target"),
        ("Prometheus time series", "Monitoring stack metrics"),
        ("Prometheus", "Monitoring"),
        ("prometheus", "monitoring"),
    )
    for source, replacement in replacements:
        rendered = rendered.replace(source, replacement)
    rendered = rendered.replace(placeholder, "Prometheus/Grafana")
    return rendered


def _client_facing_monitoring_slug(name: str) -> str:
    sanitized = name.replace("prometheus_", "").replace("prometheus", "monitoring")
    sanitized = sanitized.replace("scrape_targets", "monitoring_targets")
    return _slug(sanitized)


def _chart_series_from_prometheus_block(block: dict[str, Any]) -> list[ChartSeries]:
    series: list[ChartSeries] = []
    for row in _as_dict_list(block.get("series", [])):
        label = str(row.get("label", "")).strip() or "unlabelled"
        raw_values = row.get("values", [])
        if not isinstance(raw_values, list):
            continue
        samples: list[tuple[float, float]] = []
        for raw_sample in raw_values:
            if not isinstance(raw_sample, list) or len(raw_sample) < 2:
                continue
            timestamp = _finite_float(raw_sample[0])
            value = _finite_float(raw_sample[1])
            if timestamp is None or value is None:
                continue
            samples.append((timestamp, value))
        if samples:
            samples.sort(key=lambda sample: sample[0])
            series.append(ChartSeries(label=label, samples=tuple(samples)))
    return sorted(series, key=lambda row: row.label)


def _chart_series_stats(series: list[ChartSeries]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in series:
        values = [sample[1] for sample in item.samples]
        if not values:
            continue
        rows.append(
            {
                "label": item.label,
                "latest": item.samples[-1][1],
                "min": min(values),
                "max": max(values),
                "avg": sum(values) / len(values),
                "sample_count": len(values),
            }
        )
    return rows


def _collector_chart_refs(
    chart_refs: list[dict[str, Any]],
    collector: str,
) -> list[dict[str, Any]]:
    return [ref for ref in chart_refs if str(ref.get("collector", "")) == collector]


def write_report_html(
    report: Report,
    report_dir: Path,
    brand: BrandProfile | None = None,
    *,
    include_evidence_index: bool = False,
    report_mode: str = _REPORT_MODE_FULL,
    include_technical_appendix: bool = False,
    include_collector_warnings_and_gaps: bool = False,
) -> Path:
    brand = _brand_or_default(brand)
    html_path = report_dir / f"opsbrief-{report.environment}-weekly-report.html"
    chart_refs = _prepare_report_charts(report, report_dir, brand)
    markdown_text = _render_report_markdown(
        report,
        brand,
        chart_refs=chart_refs,
        include_evidence_index=include_evidence_index,
        report_mode=report_mode,
        include_technical_appendix=include_technical_appendix,
        include_collector_warnings_and_gaps=include_collector_warnings_and_gaps,
    )
    html_document = render_report_html(report, markdown_text, brand)
    with html_path.open("w", encoding="utf-8") as handle:
        handle.write(html_document)
    return html_path


def write_report_pdf(
    report: Report,
    report_dir: Path,
    brand: BrandProfile | None = None,
    *,
    include_evidence_index: bool = False,
    report_mode: str = _REPORT_MODE_FULL,
    include_technical_appendix: bool = False,
    include_collector_warnings_and_gaps: bool = False,
) -> Path:
    brand = _brand_or_default(brand)
    pdf_path = report_dir / f"opsbrief-{report.environment}-weekly-report.pdf"
    chart_refs = _prepare_report_charts(report, report_dir, brand)
    markdown_text = _render_report_markdown(
        report,
        brand,
        chart_refs=chart_refs,
        include_evidence_index=include_evidence_index,
        report_mode=report_mode,
        include_technical_appendix=include_technical_appendix,
        include_collector_warnings_and_gaps=include_collector_warnings_and_gaps,
    )
    html_document = render_report_html(report, markdown_text, brand)
    write_pdf_from_html(html_document, pdf_path, base_url=report_dir)
    return pdf_path


def _brand_or_default(brand: BrandProfile | None) -> BrandProfile:
    return brand if brand is not None else default_brand_profile()


def _render_report_markdown(
    report: Report,
    brand: BrandProfile,
    *,
    chart_refs: list[dict[str, Any]] | None = None,
    include_evidence_index: bool = False,
    report_mode: str = _REPORT_MODE_FULL,
    include_technical_appendix: bool = False,
    include_collector_warnings_and_gaps: bool = False,
) -> str:
    collector_map: dict[str, CheckResult] = {item.collector: item for item in report.collectors}
    report_chart_refs = chart_refs or []
    markdown_name = f"opsbrief-{report.environment}-weekly-report.md"
    json_name = f"opsbrief-{report.environment}-weekly-report.json"
    mode = _normalize_report_mode(report_mode)

    scope_text = (
        "It starts with a stakeholder review, then provides detailed evidence in an appendix."
        if mode == _REPORT_MODE_FULL
        else (
            "It starts with a stakeholder review and includes detailed evidence in an appendix."
            if include_technical_appendix
            else "It focuses on stakeholder and high-priority operational evidence."
        )
    )

    lines: list[str] = _render_report_header_markdown(
        report=report,
        brand=brand,
        scope_text=scope_text,
    )
    lines.extend(_executive_findings(collector_map))
    lines.extend(
        [
            "",
        ]
    )
    lines.extend(_health_overview(report, collector_map))
    lines.append("")
    lines.extend(_platform_scope_summary(collector_map))
    lines.append("")
    lines.extend(_autoscaling_scope_summary(collector_map))
    lines.append("")

    if mode == _REPORT_MODE_CONCISE:
        lines.extend(
            _concise_report_sections(
                report.collectors,
                collector_map,
                include_collector_warnings_and_gaps=include_collector_warnings_and_gaps,
            )
        )
        if include_technical_appendix:
            lines.extend(
                _technical_detail_sections(
                    collector_map,
                    report.collectors,
                    chart_refs=report_chart_refs,
                    include_collector_gaps=False,
                )
            )
    else:
        lines.extend(
            _technical_detail_sections(
                collector_map,
                report.collectors,
                chart_refs=report_chart_refs,
                include_collector_gaps=include_collector_warnings_and_gaps,
            )
        )

    if include_evidence_index:
        lines.extend(
            _render_evidence_index_markdown(
                markdown_name=markdown_name,
                json_name=json_name,
                has_chart_refs=bool(report_chart_refs),
            )
        )

    return _client_facing_monitoring_text("\n".join(lines))


def _render_report_header_markdown(
    *,
    report: Report,
    brand: BrandProfile,
    scope_text: str,
) -> list[str]:
    template = _MARKDOWN_TEMPLATE_ENV.get_template("report_header.md.j2")
    rendered = template.render(
        report_title=brand.report_title,
        environment=report.environment,
        scope_text=scope_text,
        generated_at=report.generated_at,
        overall_status=_format_status(report.overall_status.value),
        organization_name=brand.organization_name,
    )
    return rendered.splitlines()


def _render_evidence_index_markdown(
    *,
    markdown_name: str,
    json_name: str,
    has_chart_refs: bool,
) -> list[str]:
    template = _MARKDOWN_TEMPLATE_ENV.get_template("evidence_index.md.j2")
    rendered = template.render(
        markdown_name=markdown_name,
        json_name=json_name,
        has_chart_refs=has_chart_refs,
    )
    return rendered.splitlines()


def _normalize_report_mode(report_mode: str) -> str:
    mode = report_mode.strip().lower()
    if mode == _REPORT_MODE_CONCISE:
        return _REPORT_MODE_CONCISE
    return _REPORT_MODE_FULL


def _system_summary_header(report: Report, collector_map: dict[str, CheckResult]) -> list[str]:
    projects = sorted(_scope_project_names(collector_map))
    cluster_count = _scope_cluster_count(collector_map)
    cluster_text = str(cluster_count) if cluster_count is not None else "n/a"
    components = _scope_components(collector_map)
    status_counts = _status_rollup_counts(report.collectors)
    return [
        f"- Environment: `{report.environment}`",
        (
            "- Projects: "
            + (", ".join(f"`{project}`" for project in projects) if projects else "`n/a`")
        ),
        f"- GKE Clusters (observed): `{cluster_text}`",
        "- Components In Scope: " + (", ".join(components) if components else "n/a"),
        (
            "- Scope Exclusions: GKE monitoring and alerting are provided by "
            "Prometheus/Grafana. This report does not assess GKE-native "
            "(Cloud Monitoring) alerting and does not provide GKE monitoring/alerting."
        ),
        (
            "- Collector Result Rollup: "
            f"No Findings `{status_counts['No Findings']}`, "
            f"Needs Review `{status_counts['Needs Review']}`, "
            f"Action Required `{status_counts['Action Required']}`, "
            f"Not Assessed `{status_counts['Not Assessed']}`"
        ),
        f"- Run Timestamp (UTC): `{report.generated_at}`",
    ]


def _scope_components(collector_map: dict[str, CheckResult]) -> list[str]:
    labels = [
        ("mesh", "Service Mesh (Istio)"),
        ("network", "Network"),
        ("trend_metrics", "Redis/Kafka/VM Utilization"),
        ("services", "Managed Datastores & VMs"),
        ("backup", "Backup"),
        ("logging", "Logging"),
        ("audit", "Audit"),
        ("kubernetes_health", "Kubernetes Runtime"),
    ]
    return [label for key, label in labels if collector_map.get(key) is not None]


def _status_rollup_counts(collectors: list[CheckResult]) -> dict[str, int]:
    counts = {"No Findings": 0, "Needs Review": 0, "Action Required": 0, "Not Assessed": 0}
    for item in collectors:
        label = _format_status(item.status.value)
        counts[label] = counts.get(label, 0) + 1
    return counts


def _technical_detail_sections(
    collector_map: dict[str, CheckResult],
    collectors: list[CheckResult],
    *,
    chart_refs: list[dict[str, Any]] | None = None,
    include_collector_gaps: bool = False,
) -> list[str]:
    _ = collectors, include_collector_gaps
    lines: list[str] = [
        "",
        "## Technical Evidence Appendix",
        "",
        (
            "Detailed read-only collector evidence for platform engineers. Stakeholders should "
            "use the executive summary and health overview as the primary report view."
        ),
        "",
        "## Runtime And Control Plane Health",
        "",
        "Access, cluster inventory, and Kubernetes runtime health.",
        "",
    ]
    if collector_map.get("preflight") is not None:
        lines.extend(_preflight_summary(collector_map.get("preflight")))
    if collector_map.get("gke_inventory") is not None:
        lines.extend(_gke_inventory_summary(collector_map.get("gke_inventory")))
    if collector_map.get("kubernetes_health") is not None:
        lines.extend(_kubernetes_health_summary(collector_map.get("kubernetes_health")))
    if collector_map.get("mesh") is not None:
        lines.extend(
            [
                "## Service Mesh (Istio)",
                "",
                "Istio control-plane, gateway readiness, remote cluster sync, and proxy posture.",
                "",
            ]
        )
        lines.extend(_mesh_summary(collector_map.get("mesh")))

    if collector_map.get("prometheus_monitoring") is not None:
        lines.extend(
            [
                "## Monitoring And Alerting",
                "",
                (
                    "GKE monitoring and alerting are provided by Prometheus/Grafana. "
                    "GKE-native Cloud Monitoring alerting is out of scope for this report."
                ),
                "",
            ]
        )
        lines.extend(
            _prometheus_monitoring_summary(
                collector_map.get("prometheus_monitoring"),
                _collector_chart_refs(chart_refs or [], "prometheus_monitoring"),
            )
        )

    if collector_map.get("audit") is not None:
        lines.extend(
            [
                "## Change Audit",
                "",
                "Configuration and security changes observed in audit logs.",
                "",
            ]
        )
        lines.extend(_audit_summary(collector_map.get("audit")))

    backup = collector_map.get("backup")
    if backup is not None and not _backup_policy_out_of_scope(backup):
        lines.extend(
            [
                "## Backup And Recovery Posture",
                "",
                "This section reports backup resources and backup configuration when backup "
                "assessment is in scope.",
                "",
            ]
        )
        lines.extend(_backup_summary(backup))

    if collector_map.get("network") is not None:
        lines.extend(
            [
                "## Network And DNS Posture",
                "",
                "Network topology and control-plane posture for in-scope projects.",
                "",
            ]
        )
        lines.extend(_network_summary(collector_map.get("network")))

    if collector_map.get("kubernetes_health") is not None:
        lines.extend(
            [
                "## Kubernetes Capacity Evidence",
                "",
                "This section records Kubernetes resource evidence for weekly reporting only. "
                "It does not represent active GKE monitoring or alerting; alerting and monitoring "
                "stack findings are reported in the monitoring sections.",
                "",
            ]
        )
        lines.extend(_kubernetes_utilization(collector_map.get("kubernetes_health")))
        lines.extend(_kubernetes_namespace_utilization(collector_map.get("kubernetes_health")))

    if collector_map.get("trend_metrics") is not None or collector_map.get("services") is not None:
        lines.extend(
            [
                "## Infrastructure Capacity And Managed Services",
                "",
                "Resource utilization trends and managed service inventory for in-scope projects.",
                "",
            ]
        )
        if collector_map.get("trend_metrics") is not None:
            lines.extend(
                _trend_metrics_summary(
                    collector_map.get("trend_metrics"),
                    collector_map.get("kubernetes_health"),
                    collector_map.get("services"),
                )
            )
        if collector_map.get("services") is not None:
            lines.extend(_services_summary(collector_map.get("services")))

    if collector_map.get("logging") is not None:
        lines.extend(
            [
                "## Logging And Delivery Pipeline",
                "",
                "Cloud Logging sink, bucket, ingestion, retention, and Pub/Sub delivery posture.",
                "",
            ]
        )
        lines.extend(_logging_summary(collector_map.get("logging")))

    return lines


def _concise_report_sections(
    collectors: list[CheckResult],
    collector_map: dict[str, CheckResult],
    *,
    include_collector_warnings_and_gaps: bool = False,
) -> list[str]:
    _ = include_collector_warnings_and_gaps
    lines: list[str] = [
        "## Operational Summary (Concise)",
        "",
        "High-priority platform operations view.",
        "",
        "### Operational Checks",
        "",
    ]
    rows = [
        [
            _collector_display_name(item.collector),
            _format_status(item.status.value),
            _status_driver_for_item(item),
            _truncate(_collector_summary_for_report(item), 180),
        ]
        for item in collectors
    ]
    lines.extend(
        _markdown_table(
            headers=["Operational Check", "Assessment Result", "Reason", "Observed Details"],
            rows=rows,
        )
    )
    lines.append("")
    lines.append("### Key Operational Findings")
    lines.append("")
    key_rows: list[list[str]] = []
    key_rows.extend(_leadership_row_for_availability(collector_map))
    key_rows.extend(_leadership_row_for_incidents(collector_map))
    key_rows.extend(_leadership_row_for_backup(collector_map))
    key_rows.extend(_leadership_row_for_connectivity(collector_map))
    key_rows.extend(_leadership_row_for_capacity(collector_map))
    key_rows.extend(_leadership_row_for_compliance(collector_map))
    lines.extend(
        _markdown_table(
            headers=["Finding Area", "Assessment Result", "Reason", "Observed Details"],
            rows=key_rows,
        )
    )
    lines.append("")
    kubernetes_health = collector_map.get("kubernetes_health")
    if kubernetes_health is not None:
        lines.extend(_crashloop_details(kubernetes_health))
        lines.append("")
    return lines


def _executive_findings(collector_map: dict[str, CheckResult]) -> list[str]:
    findings: list[str] = []

    k8s = collector_map.get("kubernetes_health")
    if k8s is not None:
        for cluster in _as_dict_list(k8s.details.get("clusters", [])):
            name = str(cluster.get("cluster", "unknown-cluster"))
            status = str(cluster.get("status", "unknown"))
            if status == "ok":
                continue
            workloads = _as_dict(cluster.get("workloads", {}))
            unhealthy = _as_int(workloads.get("unhealthy_workload_total", 0))
            waiting = _as_dict(cluster.get("pod_waiting_reasons", {}))
            waiting_text = (
                ", ".join(f"{reason}:{count}" for reason, count in sorted(waiting.items()))
                or "none"
            )
            pod_issue_count = len(_as_dict_list(cluster.get("pod_issues", [])))
            findings.append(
                f"- Cluster `{name}` needs review: unhealthy workloads={unhealthy}, "
                f"pod issues={pod_issue_count}, pod waiting reasons={waiting_text}; "
                f"assessment result {_format_status(status)}."
            )

    prometheus = collector_map.get("prometheus_monitoring")
    if prometheus is not None:
        autoscaling_policy = _as_dict(prometheus.details.get("autoscaling_policy", {}))
        hpa_signals_scored = bool(autoscaling_policy.get("hpa_signals_scored", True))
        for cluster in _as_dict_list(prometheus.details.get("clusters", [])):
            cluster_name = str(cluster.get("cluster", "unknown-cluster"))
            environment = str(cluster.get("environment", "unknown-environment"))
            hpa_failures = _as_int(cluster.get("hpa_failure_condition_series_7d", 0))
            down_targets = _as_int(cluster.get("target_down", 0))
            if (hpa_signals_scored and hpa_failures > 0) or down_targets > 0:
                hpa_text = (
                    f"`{hpa_failures}` scored HPA failure condition series"
                    if hpa_signals_scored
                    else f"`{hpa_failures}` informational HPA failure condition series"
                )
                findings.append(
                    f"- Monitoring stack scope `{environment}/{cluster_name}` observed "
                    f"{hpa_text} in the 7-day window "
                    f"and `{down_targets}` currently down monitoring target(s)."
                )

    audit = collector_map.get("audit")
    if audit is not None:
        for project in _as_dict_list(audit.details.get("projects", [])):
            project_name = str(project.get("project", "unknown-project"))
            high_risk = _as_int(project.get("high_risk_events", 0))
            if high_risk > 0:
                findings.append(
                    f"- Project `{project_name}` has `{high_risk}` high-risk audit event(s) "
                    f"out of `{_audit_reviewed_count_text(project)}` activity events reviewed "
                    "in the last 7 days."
                )

    mesh = collector_map.get("mesh")
    if mesh is not None:
        for cluster in _as_dict_list(mesh.details.get("clusters", [])):
            cluster_name = str(cluster.get("cluster", "unknown-cluster"))
            cluster_status = str(cluster.get("status", "unknown"))
            if cluster_status == Status.OK.value:
                continue
            ingress = _as_dict(cluster.get("ingress_gateway", {}))
            east_west = _as_dict(cluster.get("east_west_gateway", {}))
            findings.append(
                f"- Mesh on `{cluster_name}` needs review "
                f"(ingress `{ingress.get('ready_pods', 0)}/{ingress.get('total_pods', 0)}`, "
                f"east-west `{east_west.get('ready_pods', 0)}/{east_west.get('total_pods', 0)}`; "
                f"assessment result {_format_status(cluster_status)})."
            )

    trend_metrics = collector_map.get("trend_metrics")
    if trend_metrics is not None:
        for project in _as_dict_list(trend_metrics.details.get("projects", [])):
            project_name = str(project.get("project", "unknown-project"))
            sql_risk = _as_int(project.get("sql_high_utilization_count", 0))
            vm_risk = _as_int(project.get("vm_high_cpu_count", 0))
            gke_node_risk = _as_int(project.get("gke_node_high_utilization_count", 0))
            if sql_risk > 0 or vm_risk > 0 or gke_node_risk > 0:
                findings.append(
                    f"- Project `{project_name}` observed resource usage threshold crossings: "
                    f"Cloud SQL={sql_risk}, VM CPU={vm_risk}, "
                    f"Kubernetes nodes={gke_node_risk}."
                )

    if not findings:
        return ["- No warning or critical findings were identified in this report."]
    return findings


def _platform_scope_summary(collector_map: dict[str, CheckResult]) -> list[str]:
    lines: list[str] = [
        "## Platform Inventory",
        "",
        "High-level scope observed in this report.",
        "",
    ]
    services = collector_map.get("services")
    cloud_sql_enabled = not _service_disabled_in_config(services, "cloud_sql")
    redis_enabled = not _service_disabled_in_config(services, "redis")
    kafka_enabled = not _service_disabled_in_config(services, "managed_kafka")
    rows = [
        ["In-scope projects", _count_or_na(len(_scope_project_names(collector_map)), "project")],
        ["Kubernetes clusters", _count_or_na(_scope_cluster_count(collector_map), "cluster")],
        ["Kubernetes nodes", _count_or_na(_scope_node_count(collector_map), "node")],
    ]
    if cloud_sql_enabled:
        rows.append(
            ["Cloud SQL instances", _count_or_na(_scope_cloud_sql_count(collector_map), "instance")]
        )
    if redis_enabled:
        rows.append(
            ["Redis instances", _count_or_na(_scope_redis_count(collector_map), "instance")]
        )
    if kafka_enabled:
        rows.append(
            [
                "Managed Kafka clusters",
                _count_or_na(_scope_managed_kafka_count(collector_map), "cluster"),
            ]
        )
    rows.extend(
        [
            [
                "Compute Engine instances",
                _count_or_na(_scope_compute_instance_count(collector_map), "instance"),
            ],
            [
                "Standalone Compute VMs",
                _count_or_na(_scope_standalone_compute_vm_count(collector_map), "VM"),
            ],
        ]
    )
    lines.extend(_markdown_table(headers=["Scope Item", "Observed In Report"], rows=rows))
    return lines


def _autoscaling_scope_summary(collector_map: dict[str, CheckResult]) -> list[str]:
    lines: list[str] = [
        "## Autoscaling Scope",
        "",
        (
            "Clarifies which autoscaling controls are expected and how observed evidence "
            "is scored for this environment. Detailed HPA conditions remain in the "
            "technical appendix."
        ),
        "",
    ]
    clusters: dict[str, dict[str, Any]] = {}
    k8s = collector_map.get("kubernetes_health")
    if k8s is not None:
        for cluster in _as_dict_list(k8s.details.get("clusters", [])):
            cluster_name = str(cluster.get("cluster", ""))
            if not cluster_name:
                continue
            hpa = _as_dict(cluster.get("hpa", {}))
            policy = _as_dict(hpa.get("policy", {}))
            workload_policy = _as_dict(policy.get("workload_hpa", {}))
            platform_policy = _as_dict(policy.get("platform_hpa", {}))
            entry = clusters.setdefault(cluster_name, _empty_autoscaling_cluster(cluster_name))
            workload_issues = _autoscaling_hpa_issue_count(hpa, "workload")
            platform_issues = _autoscaling_hpa_issue_count(hpa, "platform")
            entry.update(
                {
                    "workload_policy": _autoscaling_workload_policy_text(
                        workload_policy,
                        observed_total=_as_int(hpa.get("workload_hpa_total", 0)),
                    ),
                    "workload_observed": _autoscaling_hpa_observed_text(
                        hpa,
                        "workload",
                        policy=workload_policy,
                    ),
                    "platform_hpa": _autoscaling_platform_hpa_text(
                        hpa,
                        policy=platform_policy,
                    ),
                    "workload_policy_status": str(workload_policy.get("status", "assessed")),
                    "platform_policy_status": str(platform_policy.get("status", "assessed")),
                    "workload_issues": workload_issues,
                    "platform_issues": platform_issues,
                }
            )

    gke = collector_map.get("gke_inventory")
    if gke is not None:
        gke_policy = _as_dict(
            _as_dict(gke.details.get("autoscaling_policy", {})).get("node_pool_autoscaling", {})
        )
        for cluster in _as_dict_list(gke.details.get("clusters", [])):
            cluster_name = str(cluster.get("name", ""))
            if not cluster_name:
                continue
            entry = clusters.setdefault(cluster_name, _empty_autoscaling_cluster(cluster_name))
            node_pools = _as_dict_list(cluster.get("node_pools", []))
            enabled_count = sum(1 for pool in node_pools if bool(pool.get("autoscaling_enabled")))
            entry.update(
                {
                    "node_pool_autoscaling": _node_pool_autoscaling_text(node_pools),
                    "node_policy_status": str(gke_policy.get("status", "assessed")),
                    "node_pool_issues": 0 if enabled_count == len(node_pools) else 1,
                }
            )

    rows = [
        [
            str(entry.get("cluster", "")),
            str(entry.get("workload_policy", "")),
            str(entry.get("workload_observed", "")),
            str(entry.get("platform_hpa", "")),
            str(entry.get("node_pool_autoscaling", "")),
            _autoscaling_cluster_assessment(entry),
        ]
        for _cluster_name, entry in sorted(clusters.items())
    ]
    if not rows:
        lines.append("- No autoscaling evidence was collected.")
        return lines
    lines.extend(
        _markdown_table(
            headers=[
                "Cluster",
                "Workload HPA Policy",
                "Workload HPA Observed",
                "Platform / Istio HPA",
                "Node Pool Autoscaling",
                "Assessment",
            ],
            rows=rows,
        )
    )
    return lines


def _empty_autoscaling_cluster(cluster_name: str) -> dict[str, Any]:
    return {
        "cluster": cluster_name,
        "workload_policy": "No Kubernetes HPA evidence",
        "workload_observed": "No Kubernetes HPA evidence",
        "platform_hpa": "No platform HPA evidence",
        "node_pool_autoscaling": "No node-pool autoscaling evidence",
        "workload_policy_status": "not_assessed",
        "platform_policy_status": "not_assessed",
        "node_policy_status": "not_assessed",
        "workload_issues": 0,
        "platform_issues": 0,
        "node_pool_issues": 0,
    }


def _autoscaling_workload_policy_text(policy: dict[str, Any], *, observed_total: int) -> str:
    status = str(policy.get("status", "assessed"))
    if status == "informational" and observed_total == 0:
        return "Not required for this environment"
    if status == "informational":
        return "Visibility only; not scored"
    if status == "out_of_scope":
        return "Out of scope"
    if status == "assessed":
        return "Assessed"
    return status.replace("_", " ").title()


def _autoscaling_hpa_observed_text(
    hpa: dict[str, Any],
    prefix: str,
    *,
    policy: dict[str, Any],
) -> str:
    total = _as_int(hpa.get(f"{prefix}_hpa_total", 0))
    at_max = _as_int(hpa.get(f"{prefix}_hpas_at_max", 0))
    issues = _as_int(hpa.get(f"{prefix}_hpa_failure_count", 0))
    if total == 0 and str(policy.get("status", "")) == "informational":
        return "0 observed; expected for environment policy"
    if total == 0:
        return "0 observed"
    return f"{total} observed; {at_max} at max replicas; {issues} with issues"


def _autoscaling_platform_hpa_text(hpa: dict[str, Any], *, policy: dict[str, Any]) -> str:
    total = _as_int(hpa.get("platform_hpa_total", 0))
    at_max = _as_int(hpa.get("platform_hpas_at_max", 0))
    issues = _as_int(hpa.get("platform_hpa_failure_count", 0))
    if total == 0:
        observed = "0 observed"
    else:
        observed = f"{total} observed; {at_max} at max replicas; {issues} with issues"
    if str(policy.get("status", "")) == "informational":
        return f"{observed}; visibility only, not scored"
    return observed


def _autoscaling_hpa_issue_count(hpa: dict[str, Any], prefix: str) -> int:
    return _as_int(hpa.get(f"{prefix}_hpas_at_max", 0)) + _as_int(
        hpa.get(f"{prefix}_hpa_failure_count", 0)
    )


def _node_pool_autoscaling_text(node_pools: list[dict[str, Any]]) -> str:
    if not node_pools:
        return "No node-pool evidence"
    enabled_count = sum(1 for pool in node_pools if bool(pool.get("autoscaling_enabled", False)))
    min_total = sum(_as_int(pool.get("autoscaling_min", 0)) for pool in node_pools)
    max_total = sum(_as_int(pool.get("autoscaling_max", 0)) for pool in node_pools)
    suffix = f"; total range {min_total}-{max_total} nodes" if max_total else ""
    if enabled_count == len(node_pools):
        return f"{enabled_count}/{len(node_pools)} node pools enabled{suffix}"
    return f"{enabled_count}/{len(node_pools)} node pools enabled; review disabled pools{suffix}"


def _autoscaling_cluster_assessment(entry: dict[str, Any]) -> str:
    scored_issues = 0
    informational_issues = 0
    if str(entry.get("workload_policy_status", "")) == "assessed":
        scored_issues += _as_int(entry.get("workload_issues", 0))
    elif str(entry.get("workload_policy_status", "")) == "informational":
        informational_issues += _as_int(entry.get("workload_issues", 0))
    if str(entry.get("platform_policy_status", "")) == "assessed":
        scored_issues += _as_int(entry.get("platform_issues", 0))
    elif str(entry.get("platform_policy_status", "")) == "informational":
        informational_issues += _as_int(entry.get("platform_issues", 0))
    if str(entry.get("node_policy_status", "")) == "assessed":
        scored_issues += _as_int(entry.get("node_pool_issues", 0))
    if scored_issues > 0:
        return f"Review {scored_issues} scored autoscaling signal(s)."
    if informational_issues > 0:
        return (
            "Matches environment policy; review "
            f"{informational_issues} informational autoscaling signal(s)."
        )
    return "Matches environment policy."


def _autoscaling_layer_label(value: str) -> str:
    return {
        "workload_hpa": "Application workload",
        "platform_hpa": "Platform / Istio",
    }.get(value, value.replace("_", " ").title() if value else "Unknown")


def _format_policy_status(value: str) -> str:
    return value.replace("_", " ").title() if value else "Assessed"


def _health_overview(report: Report, collector_map: dict[str, CheckResult]) -> list[str]:
    rows = _operational_posture_rows(collector_map)
    critical = [row for row in rows if row[1] == "Action Required"]
    warnings = [row for row in rows if row[1] == "Needs Review"]
    healthy = [row for row in rows if row[1] == "No Findings"]
    not_assessed = [row for row in rows if row[1] == "Not Assessed"]
    action_rows = critical + warnings

    lines: list[str] = [
        "## Infrastructure Health Overview",
        "",
        f"- Overall Assessment Result: {_format_status(report.overall_status.value)}",
        "",
    ]
    lines.extend(_health_overview_bucket("Critical Findings", critical))
    lines.append("")
    lines.extend(_health_overview_bucket("Warning Findings", warnings))
    lines.append("")
    lines.extend(_health_overview_bucket("Healthy Areas", healthy))
    lines.append("")
    lines.extend(_health_overview_bucket("Not Assessed Areas", not_assessed))
    lines.append("")
    lines.append("### Immediate Actions Required")
    lines.append("")
    if action_rows:
        lines.extend(
            _markdown_table(
                headers=["Area", "Assessment Result", "Immediate Action", "Evidence Link"],
                rows=[
                    [
                        row[0],
                        row[1],
                        _next_action_for_posture_row(row),
                        _evidence_location_for_posture_row(row),
                    ]
                    for row in action_rows[:8]
                ],
            )
        )
    else:
        lines.append("- No immediate action items were identified from collected evidence.")
    return lines


def _health_overview_bucket(title: str, rows: list[list[str]]) -> list[str]:
    lines = [f"### {title}", ""]
    if not rows:
        lines.append("- None identified from collected evidence.")
        return lines
    for row in rows[:8]:
        area, assessment, reason, observed = row
        finding = _stakeholder_finding_for_posture_row(row)
        if assessment == "No Findings":
            lines.append(f"- {area}: {observed}.")
        elif assessment == "Not Assessed":
            evidence_location = _evidence_location_for_posture_row(row)
            lines.append(
                f"- {area}: {_trim_sentence(finding)}. Where to look: {evidence_location}."
            )
        else:
            evidence_location = _evidence_location_for_posture_row(row)
            lines.append(
                f"- {area}: {_trim_sentence(finding)}. Evidence: {observed}. "
                f"Where to look: {evidence_location}."
            )
    return lines


def _trim_sentence(value: str) -> str:
    return value.strip().rstrip(".")


def _operational_posture_rows(collector_map: dict[str, CheckResult]) -> list[list[str]]:
    rows: list[list[str]] = []
    rows.extend(_leadership_row_for_availability(collector_map))
    rows.extend(_leadership_row_for_incidents(collector_map))
    rows.extend(_leadership_row_for_backup(collector_map))
    rows.extend(_leadership_row_for_connectivity(collector_map))
    rows.extend(_leadership_row_for_capacity(collector_map))
    rows.extend(_leadership_row_for_compliance(collector_map))
    return rows


def _operational_action_register(collector_map: dict[str, CheckResult]) -> list[str]:
    rows = [row for row in _operational_posture_rows(collector_map) if row[1] != "No Findings"]
    lines: list[str] = [
        "## Operational Action Register",
        "",
        (
            "Follow-up items generated only from observed findings or explicit evidence gaps "
            "in this report."
        ),
        "",
    ]
    if not rows:
        lines.append("- No action-register items were generated from this report.")
        return lines

    lines.extend(
        _markdown_table(
            headers=[
                "Area",
                "Assessment Result",
                "Impact",
                "Suggested Owner",
                "Next Action",
            ],
            rows=[
                [
                    row[0],
                    row[1],
                    _impact_for_posture_row(row),
                    _owner_for_area(row[0]),
                    _next_action_for_posture_row(row),
                ]
                for row in rows[:12]
            ],
        )
    )
    lines.append("")
    lines.append(
        "Use the evidence links below to jump to the supporting appendix section for each item."
    )
    lines.append("")
    lines.append("Action Details:")
    lines.append("")
    for row in rows[:12]:
        lines.append(
            f"- {row[0]} ({row[1]}): {_stakeholder_finding_for_posture_row(row)} "
            f"Evidence: {row[3]}. Confidence: {_evidence_confidence_for_posture_row(row)}. "
            f"Where to look: {_evidence_location_for_posture_row(row)}."
        )
    return lines


def _stakeholder_finding_for_posture_row(row: list[str]) -> str:
    area, assessment, reason, _observed = row
    if assessment == "Not Assessed":
        return reason
    if area == "Resource Usage Trends":
        return _resource_usage_stakeholder_finding(reason)
    if area == "Backup Coverage":
        return _backup_stakeholder_finding(reason)
    if area == "Incidents & Alerts":
        return _incident_stakeholder_finding(reason)
    return reason


def _resource_usage_stakeholder_finding(reason: str) -> str:
    parts: list[str] = []
    if "Cloud SQL high-utilization" in reason:
        parts.append("Cloud SQL utilization crossed the review threshold")
    if "Kubernetes node utilization" in reason:
        parts.append("GKE node utilization crossed the review threshold")
    if "Kafka utilization" in reason:
        parts.append("Kafka utilization crossed the review threshold")
    if "ops agent memory metric missing" in reason.lower():
        parts.append("VM memory telemetry is missing")
    return "; ".join(parts) or reason


def _backup_stakeholder_finding(reason: str) -> str:
    if "Backups expected but none detected" in reason:
        return "Expected GKE and/or Cloud SQL backup evidence was not detected"
    return reason


def _incident_stakeholder_finding(reason: str) -> str:
    parts: list[str] = []
    if "HPA failure condition" in reason or "scaling-limited HPA" in reason:
        parts.append("autoscaling evidence needs review")
    if "down monitoring target" in reason:
        parts.append("monitoring target availability needs review")
    return "; ".join(parts) or reason


def _impact_for_posture_row(row: list[str]) -> str:
    area, assessment = row[0], row[1]
    if assessment == "Action Required":
        if area == "Resource Usage Trends":
            return "Capacity or telemetry risk can hide production-impacting saturation."
        return "Required evidence or a critical operational signal needs immediate review."
    if assessment == "Not Assessed":
        return "No health conclusion can be made for this area from this report."
    impact_map = {
        "Platform Availability": "Workload instability can affect service reliability.",
        "Incidents & Alerts": "Monitoring or autoscaling findings may mask live service risk.",
        "Backup Coverage": "Missing expected backup evidence can increase recovery risk.",
        "Network": "Network or DNS findings can affect platform reachability.",
        "Service Mesh": "Mesh readiness findings can affect east-west or ingress traffic.",
        "Resource Usage Trends": (
            "Sustained utilization or missing telemetry can affect capacity planning."
        ),
        "Logging & Compliance": "Logging pipeline gaps can reduce audit and incident visibility.",
    }
    return impact_map.get(area, "Operational evidence needs owner review.")


def _owner_for_area(area: str) -> str:
    owner_map = {
        "Platform Availability": "Platform / application owner",
        "Incidents & Alerts": "Platform operations",
        "Backup Coverage": "Platform / database owner",
        "Network": "Network platform owner",
        "Service Mesh": "Platform mesh owner",
        "Resource Usage Trends": "Platform capacity owner",
        "Logging & Compliance": "Platform logging owner",
    }
    return owner_map.get(area, "Platform operations")


def _next_action_for_posture_row(row: list[str]) -> str:
    area, assessment = row[0], row[1]
    if assessment == "Not Assessed":
        return "Confirm scope or configure the required read-only evidence source."
    if assessment == "Action Required" and "No evidence was collected" in row[2]:
        return "Restore evidence collection before treating this area as healthy."
    action_map = {
        "Platform Availability": (
            "Review affected cluster, pod, and workload evidence with the owning team."
        ),
        "Incidents & Alerts": (
            "Review active monitoring, autoscaling, and audit signals in the source systems."
        ),
        "Backup Coverage": (
            "Confirm backup policy; if expected, investigate missing backup evidence."
        ),
        "Network": (
            "Review peering, DNS, firewall, and in-cluster DNS evidence for the affected scope."
        ),
        "Service Mesh": (
            "Review Istiod, gateway, proxy, Envoy, and multi-cluster discovery evidence."
        ),
        "Resource Usage Trends": (
            "Review threshold crossings and missing telemetry with capacity owners."
        ),
        "Logging & Compliance": (
            "Review sink, bucket, Pub/Sub, and ingestion evidence for delivery gaps."
        ),
    }
    return action_map.get(area, "Review the evidence with the responsible platform owner.")


def _evidence_location_for_posture_row(row: list[str]) -> str:
    area = row[0]
    if area == "Platform Availability":
        return _appendix_link("Runtime Evidence", "runtime-and-control-plane-health")
    if area == "Incidents & Alerts":
        return _appendix_link("Monitoring Evidence", "monitoring-and-alerting")
    if area == "Backup Coverage":
        return _appendix_link("Backup Evidence", "backup-and-recovery-posture")
    if area == "Network":
        return _appendix_link("Network And DNS Evidence", "network-and-dns-posture")
    if area == "Service Mesh":
        return _appendix_link("Service Mesh Evidence", "service-mesh-istio")
    if area == "Resource Usage Trends":
        return _appendix_link("Capacity Evidence", "infrastructure-capacity-and-managed-services")
    if area == "Logging & Compliance":
        return _appendix_link("Logging Evidence", "logging-and-delivery-pipeline")
    return _appendix_link("Technical Evidence Appendix", "technical-evidence-appendix")


def _appendix_link(label: str, anchor: str) -> str:
    return f"[{label}](#{anchor})"


def _evidence_confidence_for_posture_row(row: list[str]) -> str:
    assessment, reason = row[1], row[2]
    if assessment == "Not Assessed" or "No evidence was collected" in reason:
        return "Evidence gap"
    if assessment == "Action Required" and "could not be collected" in reason:
        return "Evidence gap"
    return "Collector evidence"


def _evidence_gaps_and_limits(
    collectors: list[CheckResult],
    collector_map: dict[str, CheckResult],
) -> list[str]:
    rows = _collector_evidence_gap_rows(collectors)
    rows.extend(_report_interpretation_limit_rows(collector_map))

    lines: list[str] = [
        "## Evidence Gaps And Limits",
        "",
        (
            "Boundaries that affect how stakeholders should interpret this report. These "
            "items are not treated as healthy unless explicit evidence was collected."
        ),
        "",
    ]
    lines.extend(
        _markdown_table(
            headers=["Area", "Assessment Result", "How To Interpret"],
            rows=rows,
        )
    )
    return lines


def _collector_evidence_gap_rows(collectors: list[CheckResult]) -> list[list[str]]:
    rows: list[list[str]] = []
    for item in collectors:
        if item.status not in (
            Status.SKIPPED_CONFIG,
            Status.SKIPPED_PERMISSION,
            Status.SKIPPED_NETWORK,
            Status.FAILED,
        ):
            continue
        rows.append(
            [
                _collector_display_name(item.collector),
                _format_status(item.status.value),
                _evidence_gap_interpretation(item),
            ]
        )
    return rows


def _evidence_gap_interpretation(item: CheckResult) -> str:
    if item.status == Status.SKIPPED_CONFIG:
        return "No applicable scope or configuration was present for this collector."
    if item.status == Status.SKIPPED_PERMISSION:
        return "Read-only permissions were insufficient, so no health claim is made."
    if item.status == Status.SKIPPED_NETWORK:
        return "The evidence source was unreachable, so no health claim is made."
    if item.status in (Status.FAILED, Status.CRITICAL):
        return _status_driver_for_item(item)
    return "No health claim is made for this collector."


def _report_interpretation_limit_rows(collector_map: dict[str, CheckResult]) -> list[list[str]]:
    rows = [
        _application_dependency_gap_row(),
        [
            "GKE Monitoring Source",
            "Scoped",
            (
                "GKE alerting posture is based on Prometheus/Grafana evidence. GKE-native "
                "Cloud Monitoring alerting is intentionally out of scope."
            ),
        ],
    ]
    if collector_map.get("backup") is not None:
        rows.append(
            [
                "Backup Policy Applicability",
                "Evidence-Based",
                (
                    "Needs Review means backup evidence was expected by the collected scope "
                    "but was not detected. Not Assessed means no applicable backup scope or "
                    "configuration was present."
                ),
            ]
        )
    return rows


def _application_dependency_gap_row() -> list[str]:
    return [
        "Application Dependency Connectivity",
        "Not Assessed",
        (
            "This report does not run application-level SQL, Redis, or Kafka connection "
            "checks. DNS checks, when configured, only validate service discovery evidence."
        ),
    ]


def _scope_project_names(collector_map: dict[str, CheckResult]) -> set[str]:
    names: set[str] = set()
    for item in collector_map.values():
        details = _as_dict(item.details)
        for row in _as_dict_list(details.get("projects", [])):
            project = str(row.get("project", "")).strip()
            if project:
                names.add(project)
        for key in (
            "clusters",
            "gke_backup",
            "cloud_sql_backup",
            "cloud_sql",
            "redis",
            "managed_kafka",
            "compute_instances",
            "load_balancers",
        ):
            for row in _as_dict_list(details.get(key, [])):
                project = str(row.get("project", "")).strip()
                if project:
                    names.add(project)
    return names


def _scope_cluster_count(collector_map: dict[str, CheckResult]) -> int | None:
    counts: list[int] = []
    for collector in ("gke_inventory", "kubernetes_health", "mesh"):
        item = collector_map.get(collector)
        if item is None:
            continue
        details = _as_dict(item.details)
        if "clusters" in details:
            counts.append(len(_as_dict_list(details.get("clusters", []))))
    if not counts:
        return None
    return max(counts)


def _scope_node_count(collector_map: dict[str, CheckResult]) -> int | None:
    item = collector_map.get("kubernetes_health")
    if item is None:
        return None
    clusters = _as_dict_list(item.details.get("clusters", []))
    if not clusters:
        return 0
    total = 0
    for cluster in clusters:
        if "node_total" in cluster:
            total += _as_int(cluster.get("node_total", 0))
        else:
            total += len(_as_dict_list(cluster.get("node_inventory", [])))
    return total


def _scope_cloud_sql_count(collector_map: dict[str, CheckResult]) -> int | None:
    services = collector_map.get("services")
    if services is not None:
        rows = _as_dict_list(services.details.get("cloud_sql", []))
        counted_rows = [row for row in rows if "instance_count" in row]
        if counted_rows:
            return sum(_as_int(row.get("instance_count", 0)) for row in counted_rows)
    backup = collector_map.get("backup")
    if backup is not None:
        rows = _as_dict_list(backup.details.get("cloud_sql_backup", []))
        if rows:
            return sum(
                _as_int(row.get("instance_count", len(_as_dict_list(row.get("instances", [])))))
                for row in rows
            )
    trend = collector_map.get("trend_metrics")
    if trend is not None:
        projects = _as_dict_list(trend.details.get("projects", []))
        if projects:
            return sum(len(_as_dict_list(project.get("cloud_sql", []))) for project in projects)
    return None


def _scope_redis_count(collector_map: dict[str, CheckResult]) -> int | None:
    services = collector_map.get("services")
    if services is not None:
        rows = _as_dict_list(services.details.get("redis", []))
        counted_rows = [row for row in rows if "instance_count" in row]
        if counted_rows:
            return sum(_as_int(row.get("instance_count", 0)) for row in counted_rows)
    trend = collector_map.get("trend_metrics")
    if trend is not None:
        instances: set[str] = set()
        for project in _as_dict_list(trend.details.get("projects", [])):
            for row in _as_dict_list(project.get("redis_throughput", [])):
                instance = str(row.get("instance", "")).strip()
                if instance:
                    instances.add(instance)
        return len(instances) if instances else 0
    return None


def _scope_managed_kafka_count(collector_map: dict[str, CheckResult]) -> int | None:
    services = collector_map.get("services")
    if services is not None:
        rows = _as_dict_list(services.details.get("managed_kafka", []))
        counted_rows = [row for row in rows if "cluster_count" in row]
        if counted_rows:
            return sum(_as_int(row.get("cluster_count", 0)) for row in counted_rows)
    trend = collector_map.get("trend_metrics")
    if trend is not None:
        clusters: set[str] = set()
        for project in _as_dict_list(trend.details.get("projects", [])):
            for row in _as_dict_list(project.get("kafka_throughput", [])):
                cluster = str(row.get("cluster", "")).strip()
                if cluster:
                    clusters.add(cluster)
        return len(clusters) if clusters else 0
    return None


def _scope_compute_instance_count(collector_map: dict[str, CheckResult]) -> int | None:
    services = collector_map.get("services")
    if services is not None:
        rows = _as_dict_list(services.details.get("compute_instances", []))
        counted = sum(1 for row in rows if str(row.get("name", "")).strip())
        if counted > 0:
            return counted
    trend = collector_map.get("trend_metrics")
    if trend is not None:
        instances: set[str] = set()
        for project in _as_dict_list(trend.details.get("projects", [])):
            for row in _as_dict_list(project.get("non_gke_vm_cpu", [])):
                instance = str(row.get("instance", "")).strip()
                if instance:
                    instances.add(instance)
        return len(instances) if instances else 0
    return None


def _scope_standalone_compute_vm_count(collector_map: dict[str, CheckResult]) -> int | None:
    services = collector_map.get("services")
    if services is not None:
        rows = _as_dict_list(services.details.get("compute_instances", []))
        counted = sum(
            1
            for row in rows
            if str(row.get("name", "")).strip()
            and not _is_gke_node_instance_name(str(row.get("name", "")))
        )
        if rows:
            return counted
    trend = collector_map.get("trend_metrics")
    if trend is not None:
        instances: set[str] = set()
        for project in _as_dict_list(trend.details.get("projects", [])):
            for row in _as_dict_list(project.get("non_gke_vm_cpu", [])):
                instance = str(row.get("instance", "")).strip()
                if instance:
                    instances.add(instance)
        return len(instances) if instances else 0
    return None


def _is_gke_node_instance_name(name: str) -> bool:
    return name.strip().startswith("gke-")


def _count_or_na(count: int | None, singular: str, plural: str | None = None) -> str:
    if count is None:
        return "n/a"
    return _count_text(count, singular, plural)


def _leadership_snapshot(collector_map: dict[str, CheckResult]) -> list[str]:
    lines: list[str] = [
        "## Operational Posture",
        "",
        "Current observed state from the evidence collected for this report.",
        "",
    ]
    lines.extend(
        _markdown_table(
            headers=["Area", "Assessment Result", "Reason", "Observed Details"],
            rows=_operational_posture_rows(collector_map),
        )
    )
    return lines


def _leadership_row_for_availability(collector_map: dict[str, CheckResult]) -> list[list[str]]:
    item = collector_map.get("kubernetes_health")
    if item is None:
        return []
    clusters = _as_dict_list(item.details.get("clusters", []))
    cluster_total = len(clusters)
    non_ok_clusters = 0
    unhealthy_workloads = 0
    crashloop = 0
    for cluster in clusters:
        if str(cluster.get("status", "unknown")) != Status.OK.value:
            non_ok_clusters += 1
        workloads = _as_dict(cluster.get("workloads", {}))
        unhealthy_workloads += _as_int(workloads.get("unhealthy_workload_total", 0))
        waiting = _as_dict(cluster.get("pod_waiting_reasons", {}))
        crashloop += _as_int(waiting.get("CrashLoopBackOff", 0))
    observed = (
        f"{_count_text(cluster_total, 'cluster')}; "
        f"{_count_text(non_ok_clusters, 'cluster needing review', 'clusters needing review')}; "
        f"{_count_text(unhealthy_workloads, 'unhealthy workload')}; "
        f"{_count_text(crashloop, 'CrashLoopBackOff occurrence')}"
    )
    return [
        [
            "Platform Availability",
            _format_status(item.status.value),
            _status_driver_for_item(item),
            observed,
        ]
    ]


def _leadership_row_for_incidents(collector_map: dict[str, CheckResult]) -> list[list[str]]:
    prometheus = collector_map.get("prometheus_monitoring")
    audit = collector_map.get("audit")
    if prometheus is None and audit is None:
        return []
    high_risk_events = 0
    hpa_failure_series = 0
    down_targets = 0
    hpa_scope = "7-day"
    if prometheus is not None:
        autoscaling_policy = _as_dict(prometheus.details.get("autoscaling_policy", {}))
        if not bool(autoscaling_policy.get("hpa_signals_scored", True)):
            hpa_scope = "informational 7-day"
        for cluster in _as_dict_list(prometheus.details.get("clusters", [])):
            hpa_failure_series += _as_int(cluster.get("hpa_failure_condition_series_7d", 0))
            down_targets += _as_int(cluster.get("target_down", 0))
    if audit is not None:
        for project in _as_dict_list(audit.details.get("projects", [])):
            high_risk_events += _as_int(project.get("high_risk_events", 0))
    observed = (
        f"{high_risk_events} high-risk audit events; "
        f"{hpa_failure_series} HPA failure condition series ({hpa_scope}); "
        f"{down_targets} down monitoring targets"
    )
    return [
        [
            "Incidents & Alerts",
            _worst_status(prometheus, audit),
            _combined_status_driver(prometheus, audit),
            observed,
        ]
    ]


def _leadership_row_for_backup(collector_map: dict[str, CheckResult]) -> list[list[str]]:
    item = collector_map.get("backup")
    if item is None:
        return []
    gke_rows = _as_dict_list(item.details.get("gke_backup", []))
    sql_rows = _as_dict_list(item.details.get("cloud_sql_backup", []))
    gke_plans = sum(_as_int(row.get("plan_count", 0)) for row in gke_rows)
    sql_enabled = sum(
        1
        for row in sql_rows
        for instance in _as_dict_list(row.get("instances", []))
        if bool(instance.get("backup_enabled", False))
    )
    observed = (
        f"{gke_plans} GKE backup plans; {sql_enabled} Cloud SQL instances with backup enabled"
    )
    if _backup_policy_out_of_scope(item):
        return []
    if (
        item.status == Status.OK
        and not _backup_status_drivers(item)
        and gke_plans == 0
        and sql_enabled == 0
    ):
        return [
            [
                "Backup Coverage",
                "Not Assessed",
                "No backup policy applies to this component.",
                observed,
            ]
        ]
    return [
        [
            "Backup Coverage",
            _format_status(item.status.value),
            _status_driver_for_item(item),
            observed,
        ]
    ]


def _network_posture_summary(item: CheckResult, inactive_peerings: int) -> str:
    project_count = 0
    network_count = 0
    firewall_count = 0
    dns_zone_count = 0
    missing_required_zones = 0
    for project in _as_dict_list(item.details.get("projects", [])):
        project_count += 1
        network_count += _as_int(project.get("network_count", 0))
        firewall_count += _as_int(project.get("firewall_rule_count", 0))
        dns_zone_count += _as_int(project.get("dns_zone_count", 0))
        missing_required_zones += len(_str_list(project.get("missing_required_internal_zones", [])))
    parts = [
        _count_text(project_count, "project"),
        _count_text(network_count, "VPC network"),
        _count_text(firewall_count, "firewall rule"),
        _count_text(inactive_peerings, "inactive VPC peering"),
        _count_text(dns_zone_count, "DNS zone"),
        _count_text(missing_required_zones, "missing required DNS zone"),
    ]
    internal_dns = _as_dict(item.details.get("internal_dns", {}))
    failed_fqdns = _as_int(internal_dns.get("failed_fqdn_total", 0))
    checked_fqdns = _as_int(internal_dns.get("checked_fqdn_total", 0))
    if checked_fqdns > 0 or failed_fqdns > 0:
        parts.append(f"in-cluster DNS checks failed {failed_fqdns}/{checked_fqdns}")
    return "; ".join(parts)


def _mesh_posture_summary(item: CheckResult, mesh_gaps: int) -> str:
    clusters = _as_dict_list(item.details.get("clusters", []))
    expected_remote_links = 0
    synced_remote_links = 0
    for cluster in clusters:
        multicluster = _as_dict(cluster.get("multicluster_sync", {}))
        expected_remote_links += _as_int(multicluster.get("expected_remote_links", 0))
        synced_remote_links += _as_int(multicluster.get("synced_remote_clusters", 0))
    proxy_checks = sum(
        len(_as_dict_list(cluster.get("mesh_api_proxies", cluster.get("squid_proxies", []))))
        for cluster in clusters
    )
    envoy_samples = sum(
        len(_as_dict_list(cluster.get("envoy_proxy_samples", []))) for cluster in clusters
    )
    envoy_not_assessed = sum(
        1
        for cluster in clusters
        for sample in _as_dict_list(cluster.get("envoy_proxy_samples", []))
        if str(sample.get("status", Status.OK.value)) != Status.OK.value
    )
    envoy_successful = max(envoy_samples - envoy_not_assessed, 0)
    envoy_summary = _count_text(
        envoy_successful,
        "successful Envoy proxy sample",
        "successful Envoy proxy samples",
    )
    if envoy_not_assessed:
        envoy_not_assessed_summary = _count_text(
            envoy_not_assessed,
            "Envoy proxy sample not assessed",
            "Envoy proxy samples not assessed",
        )
        envoy_summary = f"{envoy_summary}; {envoy_not_assessed_summary}"
    remote_link_text = f"{synced_remote_links} remote cluster links synced"
    if expected_remote_links > 0:
        remote_link_text = f"{remote_link_text} (expected at least {expected_remote_links})"
    return (
        f"{_count_text(len(clusters), 'cluster')}; "
        f"{_count_text(mesh_gaps, 'gateway readiness gap')}; "
        f"{remote_link_text}; "
        f"{_count_text(proxy_checks, 'mesh API proxy check')}; "
        f"{envoy_summary}"
    )


def _leadership_row_for_connectivity(collector_map: dict[str, CheckResult]) -> list[list[str]]:
    network = collector_map.get("network")
    mesh = collector_map.get("mesh")
    if network is None and mesh is None:
        return []
    rows: list[list[str]] = []
    inactive_peerings = 0
    if network is not None:
        for project in _as_dict_list(network.details.get("projects", [])):
            inactive_peerings += _as_int(project.get("peering_inactive_count", 0))
        network_summary = _network_posture_summary(network, inactive_peerings)
        rows.append(
            [
                "Network",
                _format_status(network.status.value),
                _status_driver_for_item(network),
                network_summary,
            ]
        )

    mesh_gaps = 0
    if mesh is not None:
        for cluster in _as_dict_list(mesh.details.get("clusters", [])):
            ingress = _as_dict(cluster.get("ingress_gateway", {}))
            east_west = _as_dict(cluster.get("east_west_gateway", {}))
            if _as_int(ingress.get("ready_pods", 0)) < _as_int(ingress.get("total_pods", 0)):
                mesh_gaps += 1
            if _as_int(east_west.get("ready_pods", 0)) < _as_int(east_west.get("total_pods", 0)):
                mesh_gaps += 1
        rows.append(
            [
                "Service Mesh",
                _format_status(mesh.status.value),
                _status_driver_for_item(mesh),
                _mesh_posture_summary(mesh, mesh_gaps),
            ]
        )
    return rows


def _leadership_row_for_capacity(collector_map: dict[str, CheckResult]) -> list[list[str]]:
    item = collector_map.get("trend_metrics")
    if item is None:
        return []
    sql_risk = 0
    vm_risk = 0
    gke_node_risk = 0
    sql_cpu_peak = 0.0
    sql_memory_peak = 0.0
    sql_disk_peak = 0.0
    vm_cpu_peak = 0.0
    for project in _as_dict_list(item.details.get("projects", [])):
        sql_risk += _as_int(project.get("sql_high_utilization_count", 0))
        vm_risk += _as_int(project.get("vm_high_cpu_count", 0))
        gke_node_risk += _as_int(project.get("gke_node_high_utilization_count", 0))
        for sql in _as_dict_list(project.get("cloud_sql", [])):
            sql_cpu_peak = max(sql_cpu_peak, _as_float(sql.get("cpu_peak_percent", 0.0)))
            sql_memory_peak = max(sql_memory_peak, _as_float(sql.get("memory_peak_percent", 0.0)))
            sql_disk_peak = max(sql_disk_peak, _as_float(sql.get("disk_peak_percent", 0.0)))
        for vm in _as_dict_list(project.get("non_gke_vm_cpu", [])):
            vm_cpu_peak = max(vm_cpu_peak, _as_float(vm.get("cpu_peak_percent", 0.0)))
    observed = (
        f"{_count_text(sql_risk, 'Cloud SQL high-utilization finding')}; "
        f"{_count_text(vm_risk, 'VM high-CPU finding')}; "
        f"{_count_text(gke_node_risk, 'Kubernetes node utilization observation')}; "
        f"highest observed Cloud SQL CPU {_format_utilization_percent(sql_cpu_peak)}, "
        f"memory {_format_utilization_percent(sql_memory_peak)}, "
        f"disk {_format_utilization_percent(sql_disk_peak)}; "
        f"highest observed VM CPU {_format_utilization_percent(vm_cpu_peak)}"
    )
    return [
        [
            "Resource Usage Trends",
            _format_status(item.status.value),
            _status_driver_for_item(item),
            observed,
        ]
    ]


def _leadership_row_for_compliance(collector_map: dict[str, CheckResult]) -> list[list[str]]:
    item = collector_map.get("logging")
    if item is None:
        return []
    sink_count = 0
    pipeline_gap = 0
    for project in _as_dict_list(item.details.get("projects", [])):
        sink_count += _as_int(project.get("sink_count", 0))
        topics = _as_int(project.get("pubsub_topic_count", 0))
        subs = _as_int(project.get("pubsub_subscription_total", 0))
        if topics > 0 and subs == 0:
            pipeline_gap += 1
    pipeline_gap_text = _count_text(
        pipeline_gap,
        "project with topic/subscription gaps",
        "projects with topic/subscription gaps",
    )
    observed = f"{_count_text(sink_count, 'logging sink')}; {pipeline_gap_text}"
    return [
        [
            "Logging & Compliance",
            _format_status(item.status.value),
            _status_driver_for_item(item),
            observed,
        ]
    ]


def _worst_status(*items: CheckResult | None) -> str:
    present = [item for item in items if item is not None]
    if not present:
        return "Not Assessed"
    worst = max(present, key=lambda item: _status_rank(item.status))
    return _format_status(worst.status.value)


def _status_rank(status: Status) -> int:
    rank: dict[Status, int] = {
        Status.OK: 0,
        Status.SKIPPED_CONFIG: 1,
        Status.SKIPPED_PERMISSION: 2,
        Status.SKIPPED_NETWORK: 2,
        Status.WARNING: 3,
        Status.CRITICAL: 4,
        Status.FAILED: 5,
    }
    return rank.get(status, 5)


def _operational_signal_for_item(item: CheckResult) -> str:
    ok_messages = {
        "preflight": "Access and prerequisite checks show no warning findings.",
        "gke_inventory": "Cluster inventory is available for the configured scope.",
        "kubernetes_health": "Kubernetes runtime evidence shows no warning findings.",
        "monitoring": "GCP Monitoring alert evidence shows no warning finding.",
        "prometheus_monitoring": (
            "Monitoring stack shows no autoscaling or target warning finding."
        ),
        "audit": "No high-risk configuration or security change finding was observed.",
        "backup": "Backup evidence shows no configured warning finding.",
        "network": "Network topology and DNS policy evidence shows no warning findings.",
        "mesh": "Service mesh readiness evidence shows no warning findings.",
        "trend_metrics": "Infrastructure trend metrics stayed below warning thresholds.",
        "services": "Managed service inventory and backend health checks completed.",
        "logging": "Logging sink and delivery-path evidence shows no warning findings.",
    }
    skipped_messages = {
        Status.SKIPPED_CONFIG: "Not applicable to the configured scope.",
        Status.SKIPPED_PERMISSION: "Evidence was unavailable because read permission is missing.",
        Status.SKIPPED_NETWORK: "Evidence was unavailable because the endpoint was unreachable.",
    }
    warning_messages = {
        "preflight": "Access or prerequisite validation requires review.",
        "gke_inventory": "Cluster inventory evidence requires review.",
        "kubernetes_health": "Kubernetes runtime health requires workload or pod review.",
        "monitoring": "GCP Monitoring alert evidence requires review.",
        "prometheus_monitoring": (
            "Monitoring stack shows autoscaling or target availability findings."
        ),
        "audit": "Configuration or security change activity requires review.",
        "backup": "Configured backup evidence requires review.",
        "network": "Network topology or DNS policy posture requires review.",
        "mesh": "Service mesh control-plane or gateway readiness requires review.",
        "trend_metrics": (
            "Infrastructure utilization threshold was exceeded in collected trend data."
        ),
        "services": "Managed service inventory or backend health requires review.",
        "logging": "Logging sink or delivery-path posture requires review.",
    }

    if item.status == Status.OK:
        return ok_messages.get(item.collector, "No warning finding was observed.")
    if item.status in skipped_messages:
        return skipped_messages[item.status]
    if item.status in (Status.FAILED, Status.CRITICAL):
        return "A critical finding was observed, or required evidence could not be collected."
    return warning_messages.get(
        item.collector,
        "The observed evidence requires operational review.",
    )


def _combined_operational_signal(*items: CheckResult | None) -> str:
    present = [item for item in items if item is not None]
    if not present:
        return "This area was not assessed."
    non_ok = [item for item in present if item.status != Status.OK]
    if not non_ok:
        return "No alert, high-risk audit, or monitoring stack warning finding was observed."
    descriptions = [
        _operational_signal_for_item(item)
        for item in sorted(non_ok, key=lambda item: _status_rank(item.status), reverse=True)
    ]
    return _truncate("; ".join(_unique_non_empty(descriptions)), 360)


def _combined_status_driver(*items: CheckResult | None) -> str:
    present = [item for item in items if item is not None]
    if not present:
        return "No evidence was collected."
    non_ok = [item for item in present if item.status != Status.OK]
    if not non_ok:
        return "No configured findings were observed."
    parts = [
        f"{_collector_display_name(item.collector)}: {_status_driver_for_item(item)}"
        for item in sorted(non_ok, key=lambda item: _status_rank(item.status), reverse=True)
    ]
    return _truncate("; ".join(parts), 360)


def _status_driver_lines(item: CheckResult) -> list[str]:
    if item.status == Status.OK:
        return []
    return [f"- Reason: {_status_driver_for_item(item)}"]


def _status_driver_for_item(item: CheckResult) -> str:
    if item.status == Status.OK:
        return "No configured findings were observed."

    parts: list[str] = []
    collector = item.collector
    if collector == "preflight":
        parts.extend(_preflight_status_drivers(item))
    elif collector == "kubernetes_health":
        parts.extend(_kubernetes_status_drivers(item))
    elif collector == "monitoring":
        parts.extend(_monitoring_status_drivers(item))
    elif collector == "prometheus_monitoring":
        parts.extend(_prometheus_status_drivers(item))
    elif collector == "audit":
        parts.extend(_audit_status_drivers(item))
    elif collector == "mesh":
        parts.extend(_mesh_status_drivers(item))
    elif collector == "network":
        parts.extend(_network_status_drivers(item))
    elif collector == "trend_metrics":
        parts.extend(_trend_status_drivers(item))
    elif collector == "backup":
        parts.extend(_backup_status_drivers(item))
    elif collector == "services":
        parts.extend(_services_status_drivers(item))
    elif collector == "logging":
        parts.extend(_logging_status_drivers(item))

    if not parts:
        parts.extend(
            f"Collection issue: {_safe_markdown_inline(error, 160)}" for error in item.errors[:2]
        )
    parts = _unique_non_empty(parts)
    if not parts:
        parts = [
            _safe_markdown_inline(_collector_summary_for_report(item), 220)
            or f"Assessment result is {_format_status(item.status.value)}"
        ]
    return _truncate("; ".join(parts), 360)


def _collector_summary_for_report(item: CheckResult) -> str:
    if item.collector == "prometheus_monitoring":
        return _client_facing_monitoring_text(item.summary)
    return item.summary


def _preflight_status_drivers(item: CheckResult) -> list[str]:
    parts: list[str] = []
    for check in _as_dict_list(item.details.get("checks", [])):
        if str(check.get("status", "")) == Status.OK.value:
            continue
        name = _safe_markdown_inline(str(check.get("name", "unknown")), 80)
        message = str(check.get("message", "")).strip()
        detail = _safe_markdown_inline(message, 120) if message else "check needs review"
        parts.append(f"{name}: {detail}")
    return parts[:4]


def _kubernetes_status_drivers(item: CheckResult) -> list[str]:
    parts: list[str] = []
    for cluster in _as_dict_list(item.details.get("clusters", [])):
        cluster_name = str(cluster.get("cluster", "unknown-cluster"))
        cluster_parts: list[str] = []
        ready_nodes = _as_int(cluster.get("node_ready", 0))
        total_nodes = _as_int(cluster.get("node_total", 0))
        if total_nodes > 0 and ready_nodes < total_nodes:
            cluster_parts.append(f"nodes ready {ready_nodes}/{total_nodes}")
        waiting = _as_dict(cluster.get("pod_waiting_reasons", {}))
        waiting_text = _count_mapping_text(waiting, "pod waiting")
        if waiting_text:
            cluster_parts.append(waiting_text)
        pod_issue_count = len(_as_dict_list(cluster.get("pod_issues", [])))
        if pod_issue_count > 0:
            cluster_parts.append(_count_text(pod_issue_count, "pod issue"))
        workloads = _as_dict(cluster.get("workloads", {}))
        unhealthy = _as_int(workloads.get("unhealthy_workload_total", 0))
        if unhealthy > 0:
            cluster_parts.append(_count_text(unhealthy, "unhealthy workload"))
        hpa = _as_dict(cluster.get("hpa", {}))
        hpas_at_max = _as_int(hpa.get("scored_hpas_at_max", hpa.get("hpas_at_max", 0)))
        hpa_failures = _as_int(hpa.get("scored_hpa_failure_count", hpa.get("hpa_failure_count", 0)))
        if hpas_at_max > 0:
            cluster_parts.append(_count_text(hpas_at_max, "autoscaler at max replicas"))
        if hpa_failures > 0:
            cluster_parts.append(_count_text(hpa_failures, "autoscaler failure condition"))
        cluster_status = str(cluster.get("status", ""))
        if cluster_status != Status.OK.value and not cluster_parts:
            cluster_parts.append(f"cluster {_status_reason_phrase(cluster_status)}")
        if cluster_parts:
            parts.append(f"{cluster_name}: {', '.join(cluster_parts)}")
    return parts[:4]


def _monitoring_status_drivers(item: CheckResult) -> list[str]:
    open_alerts = 0
    missing_channels = 0
    parts: list[str] = []
    for project in _as_dict_list(item.details.get("projects", [])):
        project_name = str(project.get("project", "unknown-project"))
        alerts = _as_dict(project.get("alerts", {}))
        channels = _as_dict(project.get("notification_channels", {}))
        project_open = _as_int(alerts.get("open_alerts", 0))
        project_missing = _as_int(channels.get("missing_channels", 0))
        open_alerts += project_open
        missing_channels += project_missing
        if project_open > 0 or project_missing > 0:
            project_parts: list[str] = []
            if project_open > 0:
                project_parts.append(_count_text(project_open, "open alert"))
            if project_missing > 0:
                project_parts.append(
                    _count_text(
                        project_missing,
                        "policy missing notification channel",
                        "policies missing notification channels",
                    )
                )
            parts.append(f"{project_name}: {', '.join(project_parts)}")
    if parts:
        return parts[:4]
    if open_alerts > 0:
        return [_count_text(open_alerts, "open alert")]
    if missing_channels > 0:
        return [
            _count_text(
                missing_channels,
                "policy missing notification channel",
                "policies missing notification channels",
            )
        ]
    return []


def _prometheus_status_drivers(item: CheckResult) -> list[str]:
    autoscaling_policy = _as_dict(item.details.get("autoscaling_policy", {}))
    hpa_signals_scored = bool(autoscaling_policy.get("hpa_signals_scored", True))
    hpa_failures = 0
    current_hpa_failures = 0
    scaling_limited = 0
    down_targets = 0
    for cluster in _as_dict_list(item.details.get("clusters", [])):
        hpa_failures += _as_int(cluster.get("hpa_failure_condition_series_7d", 0))
        current_hpa_failures += _as_int(cluster.get("hpa_current_failure_condition_series", 0))
        scaling_limited += _as_int(cluster.get("hpa_current_scaling_limited", 0))
        down_targets += _as_int(cluster.get("target_down", 0))
    parts: list[str] = []
    if hpa_signals_scored and hpa_failures > 0:
        hpa_failure_text = _count_text(
            hpa_failures,
            "HPA failure condition series",
            "HPA failure condition series",
        )
        parts.append(f"{hpa_failure_text} in 7 days")
    if hpa_signals_scored and current_hpa_failures > 0:
        parts.append(
            _count_text(
                current_hpa_failures,
                "current HPA failure condition series",
                "current HPA failure condition series",
            )
        )
    if hpa_signals_scored and scaling_limited > 0:
        parts.append(
            _count_text(
                scaling_limited,
                "currently scaling-limited HPA series",
                "currently scaling-limited HPA series",
            )
        )
    if down_targets > 0:
        parts.append(_count_text(down_targets, "down monitoring target"))
    return parts


def _audit_status_drivers(item: CheckResult) -> list[str]:
    parts: list[str] = []
    for project in _as_dict_list(item.details.get("projects", [])):
        high_risk = _as_int(project.get("high_risk_events", 0))
        if high_risk > 0:
            project_name = str(project.get("project", "unknown-project"))
            parts.append(f"{project_name}: {_count_text(high_risk, 'high-risk audit event')}")
    return parts[:4]


def _mesh_status_drivers(item: CheckResult) -> list[str]:
    parts: list[str] = []
    for cluster in _as_dict_list(item.details.get("clusters", [])):
        cluster_name = str(cluster.get("cluster", "unknown-cluster"))
        cluster_parts: list[str] = []
        for label, key in (
            ("ingress gateway", "ingress_gateway"),
            ("east-west gateway", "east_west_gateway"),
            ("istiod", "istiod"),
        ):
            component = _as_dict(cluster.get(key, {}))
            ready = _as_int(component.get("ready_pods", 0))
            total = _as_int(component.get("total_pods", 0))
            if total > 0 and ready < total:
                cluster_parts.append(f"{label} ready {ready}/{total}")
        for proxy in _as_dict_list(cluster.get("mesh_api_proxies", [])) + _as_dict_list(
            cluster.get("squid_proxies", [])
        ):
            proxy_status = str(proxy.get("status", ""))
            if proxy_status and proxy_status != Status.OK.value:
                name = str(proxy.get("name", "proxy"))
                note = str(proxy.get("note", "")).strip()
                detail = f"{name} {_status_reason_phrase(proxy_status)}"
                if note:
                    detail = f"{detail}: {_truncate(note, 100)}"
                cluster_parts.append(detail)
        multicluster = _as_dict(cluster.get("multicluster_sync", {}))
        missing_links = _as_int(multicluster.get("missing_remote_links", 0))
        if missing_links > 0:
            cluster_parts.append(_count_text(missing_links, "missing multi-cluster remote link"))
        envoy_sample_failures = sum(
            1
            for sample in _as_dict_list(cluster.get("envoy_proxy_samples", []))
            if str(sample.get("status", Status.OK.value)) != Status.OK.value
        )
        if envoy_sample_failures > 0:
            cluster_parts.append(_count_text(envoy_sample_failures, "Envoy sample issue"))
        cluster_status = str(cluster.get("status", ""))
        if cluster_status != Status.OK.value and not cluster_parts:
            cluster_parts.append(f"cluster {_status_reason_phrase(cluster_status)}")
        if cluster_parts:
            parts.append(f"{cluster_name}: {', '.join(cluster_parts)}")
    return parts[:4]


def _network_status_drivers(item: CheckResult) -> list[str]:
    parts: list[str] = []
    inactive_peerings = 0
    for project in _as_dict_list(item.details.get("projects", [])):
        project_name = str(project.get("project", "unknown-project"))
        project_inactive = _as_int(project.get("peering_inactive_count", 0))
        inactive_peerings += project_inactive
        if project_inactive > 0:
            parts.append(f"{project_name}: {_count_text(project_inactive, 'inactive VPC peering')}")
        for finding in _as_dict_list(project.get("dns_policy_findings", [])):
            if str(finding.get("check", "")) != "required_internal_zones":
                continue
            message = str(finding.get("message", "")).strip()
            if message:
                parts.append(f"{project_name}: {_safe_markdown_inline(message, 180)}")
    internal_dns = _as_dict(item.details.get("internal_dns", {}))
    failed_fqdns = _as_int(internal_dns.get("failed_fqdn_total", 0))
    checked_fqdns = _as_int(internal_dns.get("checked_fqdn_total", 0))
    if failed_fqdns > 0:
        parts.append(
            f"in-cluster DNS resolution failed for {failed_fqdns} of {checked_fqdns} FQDN checks"
        )
    if not parts and inactive_peerings > 0:
        parts.append(_count_text(inactive_peerings, "inactive VPC peering"))
    return parts[:4]


def _trend_status_drivers(item: CheckResult) -> list[str]:
    sql_risk = 0
    vm_risk = 0
    vm_memory_risk = 0
    gke_node_risk = 0
    redis_warning = 0
    kafka_warning = 0
    errors: list[str] = []
    for project in _as_dict_list(item.details.get("projects", [])):
        project_name = str(project.get("project", "unknown-project"))
        sql_risk += _as_int(project.get("sql_high_utilization_count", 0))
        vm_risk += _as_int(project.get("vm_high_cpu_count", 0))
        vm_memory_risk += _as_int(project.get("vm_high_memory_count", 0))
        gke_node_risk += _as_int(project.get("gke_node_high_utilization_count", 0))
        redis_warning += _as_int(project.get("redis_warning_count", 0))
        kafka_warning += _as_int(project.get("kafka_warning_count", 0))
        for key in (
            "gke_utilization_error",
            "redis_throughput_error",
            "kafka_throughput_error",
            "redis_utilization_error",
            "kafka_utilization_error",
            "vm_cpu_error",
            "vm_memory_error",
        ):
            message = str(project.get(key, "")).strip()
            if message:
                errors.append(f"{project_name}: {_safe_markdown_inline(message, 120)}")
    parts: list[str] = []
    if sql_risk > 0:
        parts.append(f"{_count_text(sql_risk, 'Cloud SQL high-utilization finding')} >= 85%")
    if vm_risk > 0:
        parts.append(f"{_count_text(vm_risk, 'VM high-CPU finding')} >= 85%")
    if vm_memory_risk > 0:
        parts.append(f"{_count_text(vm_memory_risk, 'VM high-memory finding')} >= 85%")
    if gke_node_risk > 0:
        parts.append(
            f"{_count_text(gke_node_risk, 'Kubernetes node utilization observation')} >= 85%"
        )
    if redis_warning > 0:
        parts.append(_count_text(redis_warning, "Redis utilization warning finding"))
    if kafka_warning > 0:
        parts.append(_count_text(kafka_warning, "Kafka utilization warning finding"))
    parts.extend(errors[:2])
    return parts


def _backup_status_drivers(item: CheckResult) -> list[str]:
    missing_labels: list[str] = []
    other_parts: list[str] = []
    for key, label in (
        ("gke_backup", "GKE backup"),
        ("cloud_sql_backup", "Cloud SQL backup"),
        ("elasticsearch_backup", "Elasticsearch backup"),
    ):
        for row in _as_dict_list(item.details.get(key, [])):
            status = str(row.get("status", ""))
            if status == Status.SKIPPED_CONFIG.value:
                continue
            if status and status != Status.OK.value:
                target = str(row.get("project", row.get("name", "target"))) or "target"
                reason = _row_reason(row)
                if reason == "Backups expected but none detected.":
                    missing_labels.append(label)
                    continue
                detail = f"{label} {target} {_status_reason_phrase(status)}"
                if reason:
                    detail = f"{detail}: {_truncate(reason, 120)}"
                other_parts.append(detail)
    parts: list[str] = []
    if missing_labels:
        parts.append(f"{', '.join(_unique_non_empty(missing_labels))} expected but not detected")
    parts.extend(other_parts)
    return parts[:4]


def _services_status_drivers(item: CheckResult) -> list[str]:
    parts: list[str] = []
    for key, label in (
        ("cloud_sql", "Cloud SQL"),
        ("redis", "Redis"),
        ("managed_kafka", "Managed Kafka"),
    ):
        for row in _as_dict_list(item.details.get(key, [])):
            status = str(row.get("status", ""))
            if status and status != Status.OK.value:
                project_name = str(row.get("project", "")).strip()
                scope = f" {project_name}" if project_name else ""
                reason = _row_reason(row)
                detail = f"{label}{scope} {_status_reason_phrase(status)}"
                if reason:
                    detail = f"{detail}: {_truncate(reason, 120)}"
                parts.append(detail)
    for row in _as_dict_list(item.details.get("compute_instances", [])):
        instance_status = str(row.get("instance_status", row.get("status", "")))
        if instance_status and instance_status != "RUNNING":
            parts.append(f"Compute instance {row.get('name', 'unknown')} state {instance_status}")
    for row in _as_dict_list(item.details.get("load_balancers", [])):
        status = str(row.get("status", ""))
        backend_health = _format_backend_health(row)
        if status and status != Status.OK.value:
            backend_service = row.get("backend_service", "unknown")
            parts.append(f"Load balancer {backend_service} {_status_reason_phrase(status)}")
        elif "unhealthy=" in backend_health and not backend_health.endswith(
            "unhealthy=0, unknown=0"
        ):
            parts.append(
                f"Load balancer {row.get('backend_service', 'unknown')} backend health "
                f"{backend_health}"
            )
    return parts[:4]


def _logging_status_drivers(item: CheckResult) -> list[str]:
    parts: list[str] = []
    for project in _as_dict_list(item.details.get("projects", [])):
        project_name = str(project.get("project", "unknown-project"))
        project_parts: list[str] = []
        if _as_int(project.get("bucket_count", 0)) == 0:
            project_parts.append("no log buckets discovered")
        if (
            _as_int(project.get("pubsub_topic_count", 0)) > 0
            and _as_int(project.get("pubsub_subscription_total", 0)) == 0
        ):
            project_parts.append("logging Pub/Sub topics have no subscriptions")
        metrics_error = str(project.get("metrics_error", "")).strip()
        if metrics_error:
            project_parts.append(_truncate(metrics_error, 120))
        project_status = str(project.get("status", ""))
        if project_status != Status.OK.value and not project_parts:
            project_parts.append(f"project {_status_reason_phrase(project_status)}")
        if project_parts:
            parts.append(f"{project_name}: {', '.join(project_parts)}")
    return parts[:4]


def _row_reason(row: dict[str, Any]) -> str:
    return str(row.get("reason", row.get("error", ""))).strip()


def _count_text(count: int, singular: str, plural: str | None = None) -> str:
    if count == 1:
        return f"{count} {singular}"
    return f"{count} {plural or singular + 's'}"


def _count_mapping_text(values: dict[str, Any], label: str) -> str:
    parts = [
        f"{key}:{_as_int(value)}" for key, value in sorted(values.items()) if _as_int(value) > 0
    ]
    if not parts:
        return ""
    return f"{label} ({', '.join(parts[:4])})"


def _unique_non_empty(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        cleaned = value.strip()
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def _preflight_summary(item: CheckResult | None) -> list[str]:
    if item is None:
        return _missing_collector("preflight")
    checks = _as_dict_list(item.details.get("checks", []))
    total = len(checks)
    passed = sum(1 for check in checks if str(check.get("status", "")) == "ok")
    lines = [
        _collector_section_heading("Access And API Readiness", item.status),
        f"- Summary: {_safe_markdown_inline(item.summary)}",
        f"- Evidence: checks passed `{passed}/{total}`",
    ]
    lines.extend(_status_driver_lines(item))
    non_ok = [check for check in checks if str(check.get("status", "")) != "ok"]
    if non_ok:
        lines.append("- Checks needing review:")
        for check in non_ok[:5]:
            message = _safe_markdown_inline(str(check.get("message", "")))
            check_name = _safe_markdown_inline(str(check.get("name", "unknown")), 80)
            lines.append(f"  - `{check_name}` -> {message}")
    lines.append("")
    return lines


def _gke_inventory_summary(item: CheckResult | None) -> list[str]:
    if item is None:
        return _missing_collector("gke_inventory")
    lines = [
        _collector_section_heading("GKE Cluster Inventory", item.status),
        f"- Summary: {item.summary}",
    ]
    lines.extend(_status_driver_lines(item))
    lines.append("")
    rows: list[list[str]] = []
    for cluster in _as_dict_list(item.details.get("clusters", [])):
        rows.append(
            [
                str(cluster.get("name", "")),
                str(cluster.get("project", "")),
                str(cluster.get("region", "")),
                str(cluster.get("release_channel", "")),
                str(cluster.get("master_version", "")),
                str(cluster.get("node_pool_count", 0)),
                "yes" if bool(cluster.get("private_nodes", False)) else "no",
            ]
        )
    lines.extend(
        _markdown_table(
            headers=[
                "Cluster",
                "Project",
                "Region",
                "Release Channel",
                "K8s Version",
                "Node Pools",
                "Private Nodes",
            ],
            rows=rows,
        )
    )
    lines.append("")
    return lines


def _kubernetes_health_summary(item: CheckResult | None) -> list[str]:
    if item is None:
        return _missing_collector("kubernetes_health")
    lines = [
        _collector_section_heading("Kubernetes Runtime Health", item.status),
        f"- Summary: {item.summary}",
    ]
    lines.extend(_status_driver_lines(item))
    lines.append("")
    rows: list[list[str]] = []
    for cluster in _as_dict_list(item.details.get("clusters", [])):
        workloads = _as_dict(cluster.get("workloads", {}))
        waiting = _as_dict(cluster.get("pod_waiting_reasons", {}))
        waiting_text = (
            ", ".join(f"{reason}:{count}" for reason, count in sorted(waiting.items())) or "none"
        )
        rows.append(
            [
                str(cluster.get("cluster", "")),
                f"{cluster.get('node_ready', 0)}/{cluster.get('node_total', 0)}",
                str(cluster.get("pod_total", 0)),
                str(workloads.get("unhealthy_workload_total", 0)),
                waiting_text,
            ]
        )
    lines.extend(
        _markdown_table(
            headers=[
                "Cluster",
                "Nodes Ready",
                "Total Pods",
                "Unhealthy Workloads",
                "Pods Waiting By Reason",
            ],
            rows=rows,
        )
    )
    lines.append("")
    lines.extend(_crashloop_details(item))
    lines.append("")
    return lines


def _crashloop_details(item: CheckResult) -> list[str]:
    rows: list[list[str]] = []
    for cluster in _as_dict_list(item.details.get("clusters", [])):
        cluster_name = str(cluster.get("cluster", ""))
        for issue in _as_dict_list(cluster.get("pod_issues", [])):
            symptom = str(issue.get("symptom", ""))
            if symptom not in {"CrashLoopBackOff", "HighRestartCount"}:
                continue
            evidence = _as_dict(issue.get("evidence", {}))
            rows.append(
                [
                    cluster_name,
                    str(issue.get("namespace", "")),
                    str(issue.get("pod", "")),
                    str(issue.get("container", "")),
                    symptom,
                    str(evidence.get("current_state", "")),
                    str(evidence.get("restart_count", 0)),
                    str(evidence.get("last_terminated_reason", "")),
                    str(evidence.get("last_exit_code", "")),
                    str(evidence.get("last_finished_at", "")),
                    str(issue.get("probable_cause", "")),
                    str(issue.get("recommended_action", "")),
                ]
            )

    lines = ["### Pod Restart Diagnostics", ""]
    if not rows:
        lines.append("- No CrashLoopBackOff or high-restart pods detected at collection time.")
        return lines
    lines.append(
        "- Probable cause is inferred from current pod status, previous termination state, "
        "restart count, and recent pod warning events."
    )
    lines.append("")
    lines.extend(
        _markdown_table(
            headers=[
                "Cluster",
                "Namespace",
                "Pod",
                "Container",
                "Symptom",
                "Current State",
                "Restarts",
                "Last Exit Reason",
                "Last Exit Code",
                "Last Finished At",
                "Probable Cause",
                "Recommended Next Action",
            ],
            rows=rows[:30],
        )
    )
    return lines


def _monitoring_summary(item: CheckResult | None) -> list[str]:
    if item is None:
        return _missing_collector("monitoring")
    lines = [
        _collector_section_heading("GCP Monitoring Alerts", item.status),
        f"- Summary: {item.summary}",
    ]
    lines.extend(_status_driver_lines(item))
    lines.append("")
    rows: list[list[str]] = []
    for project in _as_dict_list(item.details.get("projects", [])):
        alerts = _as_dict(project.get("alerts", {}))
        channels = _as_dict(project.get("notification_channels", {}))
        rows.append(
            [
                str(project.get("project", "")),
                _format_status(str(project.get("status", "na"))),
                str(project.get("alert_policy_enabled", 0)),
                str(alerts.get("open_alerts", 0)),
                str(alerts.get("opened_last_7d", 0)),
                str(channels.get("missing_channels", 0)),
            ]
        )
    lines.extend(
        _markdown_table(
            headers=[
                "Project",
                "Assessment",
                "Enabled Alert Policies",
                "Open Alerts",
                "New Alerts (7-Day)",
                "Policies Missing Notifications",
            ],
            rows=rows,
        )
    )
    lines.append("")

    policy_rows: list[list[str]] = []
    policy_total = 0
    for project in _as_dict_list(item.details.get("projects", [])):
        project_name = str(project.get("project", ""))
        sample_policies = _as_dict_list(project.get("sample_policies", []))
        policy_total += _as_int(project.get("alert_policy_total", len(sample_policies)))
        for policy in sample_policies:
            policy_rows.append(
                [
                    project_name,
                    str(policy.get("display_name", "")) or str(policy.get("name", "")),
                    "yes" if bool(policy.get("enabled", True)) else "no",
                ]
            )
    lines.append(
        _per_project_limited_table_heading(
            "GCP Monitoring Alert Policies",
            total_count=policy_total,
            shown_count=len(policy_rows),
            per_project_limit=10,
        )
    )
    lines.append("")
    lines.extend(
        _markdown_table(
            headers=["Project", "Alert Policy", "Enabled"],
            rows=policy_rows,
        )
    )
    lines.append("")
    open_alert_rows: list[list[str]] = []
    top_policy_rows: list[list[str]] = []
    open_alert_total = 0
    for project in _as_dict_list(item.details.get("projects", [])):
        project_name = str(project.get("project", ""))
        alerts = _as_dict(project.get("alerts", {}))
        sample_open_alerts = _as_dict_list(alerts.get("sample_open_alerts", []))
        open_alert_total += _as_int(alerts.get("open_alerts", len(sample_open_alerts)))
        for alert in sample_open_alerts:
            open_alert_rows.append(
                [
                    project_name,
                    str(alert.get("name", "")),
                    str(alert.get("policy", "")),
                    str(alert.get("state", "")),
                    str(alert.get("open_time", "")),
                ]
            )
        for policy in _as_dict_list(alerts.get("top_policies", [])):
            top_policy_rows.append(
                [
                    project_name,
                    str(policy.get("policy", "")),
                    str(policy.get("count", 0)),
                ]
            )
    lines.append(
        _per_project_limited_table_heading(
            "Open GCP Monitoring Alerts",
            total_count=open_alert_total,
            shown_count=len(open_alert_rows),
            per_project_limit=10,
        )
    )
    lines.append("")
    lines.extend(
        _markdown_table(
            headers=["Project", "Alert", "Alert Policy", "Current State", "Opened At"],
            rows=open_alert_rows,
        )
    )
    lines.append("")
    lines.append("Alert Policy Activity (Top Policies):")
    lines.append("")
    lines.extend(
        _markdown_table(
            headers=["Project", "Alert Policy", "Alert Count"],
            rows=top_policy_rows,
        )
    )
    lines.append("")
    return lines


def _prometheus_monitoring_summary(
    item: CheckResult | None,
    chart_refs: list[dict[str, Any]] | None = None,
) -> list[str]:
    if item is None:
        return []
    scope = _as_dict(item.details.get("scope", {}))
    lines = [
        _collector_section_heading("Monitoring Stack", item.status),
    ]
    lines.extend(_status_driver_lines(item))
    labels = _format_prometheus_scope(_as_dict(scope.get("labels", {})))
    if labels:
        lines.append(f"- Metric Scope: {labels}")
    lines.append("")

    rows: list[list[str]] = []
    for cluster in _as_dict_list(item.details.get("clusters", [])):
        rows.append(
            [
                str(cluster.get("environment", "")),
                str(cluster.get("cluster", "")),
                _format_status(str(cluster.get("status", "na"))),
                str(cluster.get("hpa_total", 0)),
                str(cluster.get("hpa_current_failure_condition_series", 0)),
                str(cluster.get("hpa_failure_condition_series_7d", 0)),
                str(cluster.get("hpa_current_scaling_limited", 0)),
                str(cluster.get("hpa_scaling_limited_7d", 0)),
                str(cluster.get("target_total", 0)),
                str(cluster.get("target_down", 0)),
                _format_down_jobs(_as_dict_list(cluster.get("down_jobs", []))),
            ]
        )
    lines.extend(
        _markdown_table(
            headers=[
                "Environment",
                "Cluster",
                "Assessment",
                "HPAs",
                "Current HPA Failure Condition Series",
                "Historical HPA Failure Condition Series (7-Day)",
                "Currently Scaling-Limited Series",
                "Historical Scaling-Limited Series (7-Day)",
                "Monitoring Targets",
                "Down Targets",
                "Down Jobs",
            ],
            rows=rows,
        )
    )
    lines.append("")

    refs = chart_refs or []
    if refs:
        lines.append("Monitoring Stack Metric Summary:")
        lines.append("")
        summary_rows: list[list[str]] = []
        for ref in refs:
            title = _client_facing_monitoring_text(str(ref.get("title", "")))
            unit = str(ref.get("unit", ""))
            for stat in _as_dict_list(ref.get("series_stats", [])):
                summary_rows.append(
                    [
                        title,
                        str(stat.get("label", "")),
                        _format_chart_value(stat.get("latest", 0.0), unit),
                        _format_chart_value(stat.get("min", 0.0), unit),
                        _format_chart_value(stat.get("max", 0.0), unit),
                        _format_chart_value(stat.get("avg", 0.0), unit),
                    ]
                )
        lines.extend(
            _markdown_table(
                headers=["Graph", "Series", "Latest", "Min", "Max", "Avg"],
                rows=summary_rows,
            )
        )
        lines.append("")
        lines.append("### Monitoring Stack Trends")
        lines.append("")
        lines.append(
            "The charts below visualize key time-series metrics from the monitoring "
            "data source over the reporting window. They provide visual context "
            "for utilization, autoscaling behavior, and monitoring target health "
            "summarized in the tables above."
        )
        lines.append("")
        for ref in refs:
            title = _client_facing_monitoring_text(str(ref.get("title", "Monitoring metric")))
            path = str(ref.get("path", ""))
            if not path:
                continue
            lines.append(f"![{title}]({path})")
            series_labels = _str_list(ref.get("series_labels", []))
            if series_labels:
                rendered_labels = ", ".join(
                    f"`{label}`" for label in series_labels[:_CHART_SERIES_LIMIT]
                )
                hidden_count = max(0, _as_int(ref.get("series_count", 0)) - len(series_labels))
                suffix = f"; +{hidden_count} additional series omitted" if hidden_count else ""
                lines.append(f"- Series: {rendered_labels}{suffix}")
            lines.append("")

    condition_rows: list[list[str]] = []
    for row in _as_dict_list(item.details.get("hpa_failure_conditions_7d", [])):
        condition_rows.append(
            [
                str(row.get("environment", "")),
                str(row.get("cluster", "")),
                str(row.get("condition", "")),
                str(row.get("status", "")),
                str(row.get("condition_series_7d", 0)),
            ]
        )
    if condition_rows:
        lines.append("Autoscaler Failure Conditions (7-Day):")
        lines.append("")
        lines.append(
            "- Informational historical monitoring signal. Current autoscaling policy scope "
            "is summarized in Autoscaling Scope."
        )
        lines.append("")
        lines.extend(
            _markdown_table(
                headers=[
                    "Environment",
                    "Cluster",
                    "Condition",
                    "Condition State",
                    "Historical Series",
                ],
                rows=condition_rows,
            )
        )
        lines.append("")
    return lines


def _audit_summary(item: CheckResult | None) -> list[str]:
    if item is None:
        return _missing_collector("audit")
    lines = [
        "### Configuration Change Audit",
        f"- Summary: {item.summary}",
        "- Focus: named ConfigMap, Secret, IAM, firewall, and route changes from audit logs. "
        "Secret values are never collected or displayed.",
    ]
    lines.append("")

    all_review_rows = _audit_review_change_rows(item)
    review_rows = all_review_rows[:25]
    lines.append(_audit_review_table_heading(item, shown_count=len(review_rows)))
    lines.append("")
    lines.append(
        "- Routine leader-election, heartbeat, lease, service-proxy, autoscaler, "
        "and other system-maintenance records are hidden from this table."
    )
    if _audit_has_limited_targeted_results(item):
        lines.append(
            "- A trailing `+` means the targeted audit query hit its collection limit; "
            "counts are lower bounds for the 7-day window."
        )
    lines.append("")
    if not review_rows:
        review_rows = [
            [
                "No review-candidate changes found in targeted audit query",
                "",
                "",
                "",
                "",
                "",
                "",
            ]
        ]
    lines.extend(
        _markdown_table(
            headers=[
                "Project",
                "Time",
                "Change",
                "Resource Type",
                "Namespace/Scope",
                "Resource Name",
                "Changed By",
            ],
            rows=review_rows,
        )
    )
    lines.append("")

    rows: list[list[str]] = []
    for project in _as_dict_list(item.details.get("projects", [])):
        rows.append(
            [
                str(project.get("project", "")),
                _audit_reviewed_count_text(project),
                _audit_count_text(
                    project,
                    "review_candidate_entry_count",
                    limit_key="review_candidate_entries_limited",
                ),
                _audit_count_text(
                    project,
                    "meaningful_change_events",
                    limit_key="review_candidate_entries_limited",
                ),
                _audit_count_text(
                    project,
                    "meaningful_configmap_events",
                    limit_key="review_candidate_entries_limited",
                ),
                _audit_count_text(
                    project,
                    "meaningful_secret_events",
                    fallback_key="new_secret_events",
                    limit_key="review_candidate_entries_limited",
                ),
                _audit_count_text(
                    project,
                    "high_risk_events",
                    limit_key="high_risk_entries_limited",
                ),
                str(project.get("noisy_events_filtered", 0)),
            ]
        )
    lines.append("Audit Coverage And Filtered Routine Activity:")
    lines.append("")
    lines.extend(
        _markdown_table(
            headers=[
                "Project",
                "Audit Events Scanned (7-Day)",
                "Targeted Review Events Scanned",
                "Review Candidate Changes",
                "ConfigMap Changes",
                "Secret Changes",
                "High-Risk Security Events",
                "Routine Events Hidden From View",
            ],
            rows=rows,
        )
    )
    lines.append("")
    return lines


def _audit_review_table_heading(item: CheckResult, shown_count: int) -> str:
    total = 0
    for project in _as_dict_list(item.details.get("projects", [])):
        total += _as_int(project.get("meaningful_change_events", 0))
    if total > shown_count or _audit_has_limited_targeted_results(item):
        return "Configuration And Security Changes Requiring Review (Recent Sample):"
    return "Configuration And Security Changes Requiring Review:"


def _audit_review_change_rows(item: CheckResult) -> list[list[str]]:
    rows: list[list[str]] = []
    for project in _as_dict_list(item.details.get("projects", [])):
        project_name = str(project.get("project", ""))
        for event in _audit_review_candidate_events(project):
            rows.append(
                [
                    project_name,
                    str(event.get("timestamp", "")),
                    _audit_action_label(str(event.get("action", ""))),
                    _audit_event_resource_type(event),
                    _audit_event_resource_scope(event),
                    _audit_event_resource_name(event),
                    str(event.get("principal", "")),
                ]
            )
    return rows


def _audit_review_candidate_events(project: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        event
        for event in _as_dict_list(project.get("meaningful_recent_events", []))
        if _is_audit_review_candidate(event)
    ]


def _audit_has_limited_targeted_results(item: CheckResult) -> bool:
    return any(
        bool(project.get("review_candidate_entries_limited", False))
        or bool(project.get("high_risk_entries_limited", False))
        for project in _as_dict_list(item.details.get("projects", []))
    )


def _audit_count_text(
    project: dict[str, Any],
    key: str,
    *,
    fallback_key: str | None = None,
    limit_key: str | None = None,
) -> str:
    if key in project:
        value = _as_int(project.get(key, 0))
        suffix = "+" if limit_key is not None and bool(project.get(limit_key, False)) else ""
        return f"{value}{suffix}"
    if fallback_key is not None and fallback_key in project:
        value = _as_int(project.get(fallback_key, 0))
        suffix = "+" if limit_key is not None and bool(project.get(limit_key, False)) else ""
        return f"{value}{suffix}"
    return "n/a"


def _is_audit_review_candidate(event: dict[str, Any]) -> bool:
    if _is_routine_audit_event_for_report(event):
        return False
    action = str(event.get("action", ""))
    if action not in ("create", "delete", "update"):
        return False
    resource_type = _audit_event_resource_type(event)
    if resource_type in {"ConfigMap", "Secret", "IAM Policy", "Firewall Rule", "Route"}:
        return True
    method = str(event.get("method", "")).lower()
    return any(
        token in method
        for token in (
            "setiampolicy",
            "secret",
            "firewalls.insert",
            "firewalls.patch",
            "firewalls.delete",
            "routes.insert",
            "routes.delete",
        )
    )


def _is_routine_audit_event_for_report(event: dict[str, Any]) -> bool:
    method = str(event.get("method", "")).lower()
    resource = str(event.get("resource", "")).lower()
    principal = str(event.get("principal", "")).lower()
    if ".leases." in method or "/leases/" in resource:
        return True
    if ".status." in method or resource.endswith("/status") or "/status" in resource:
        return True
    if ".services.proxy.get" in method:
        return True
    if "/pods/gke-system-balloon-pod" in resource and principal == "system:cluster-autoscaler":
        return True
    if "/configmaps/" in resource:
        configmap_name = resource.rsplit("/configmaps/", 1)[-1].split("/", maxsplit=1)[0]
        routine_configmap_tokens = (
            "leader",
            "election",
            "heartbeat",
            "cluster-autoscaler-status",
            "cluster-kubestore",
            "istio-ip-autoallocate",
        )
        if any(token in configmap_name for token in routine_configmap_tokens):
            return True
    return False


def _audit_event_resource_type(event: dict[str, Any]) -> str:
    value = str(event.get("resource_type", "")).strip()
    if value:
        return value
    return _audit_resource_identity_for_report(
        str(event.get("method", "")),
        str(event.get("resource", "")),
    )["type"]


def _audit_event_resource_scope(event: dict[str, Any]) -> str:
    value = str(event.get("resource_scope", "")).strip()
    if value:
        return value
    return _audit_resource_identity_for_report(
        str(event.get("method", "")),
        str(event.get("resource", "")),
    )["scope"]


def _audit_event_resource_name(event: dict[str, Any]) -> str:
    value = str(event.get("resource_name", "")).strip()
    if value:
        return value
    return _audit_resource_identity_for_report(
        str(event.get("method", "")),
        str(event.get("resource", "")),
    )["name"]


def _audit_resource_identity_for_report(method_name: str, resource_name: str) -> dict[str, str]:
    method = method_name.lower()
    parts = [part for part in resource_name.split("/") if part]
    namespace = _resource_part_after(parts, "namespaces")
    project = _resource_part_after(parts, "projects")

    if "configmaps" in parts:
        return {
            "type": "ConfigMap",
            "scope": namespace or "cluster",
            "name": _resource_part_after(parts, "configmaps") or "resource not provided",
        }
    if "secrets" in parts or "secret" in method:
        secret_name = _resource_part_after(parts, "secrets")
        return {
            "type": "Secret",
            "scope": namespace or project or "project",
            "name": secret_name or (parts[-1] if parts else "resource not provided"),
        }
    if "firewalls" in parts:
        return {
            "type": "Firewall Rule",
            "scope": project or "project",
            "name": _resource_part_after(parts, "firewalls") or parts[-1],
        }
    if "routes" in parts:
        return {
            "type": "Route",
            "scope": project or "project",
            "name": _resource_part_after(parts, "routes") or parts[-1],
        }
    if "setiampolicy" in method:
        return {
            "type": "IAM Policy",
            "scope": project or "project",
            "name": resource_name or "resource not provided by audit log",
        }
    if parts:
        return {
            "type": "Audit Resource",
            "scope": namespace or project or "project",
            "name": parts[-1],
        }
    return {
        "type": "Audit Resource",
        "scope": "unknown",
        "name": "resource not provided by audit log",
    }


def _resource_part_after(parts: list[str], marker: str) -> str:
    try:
        index = parts.index(marker)
    except ValueError:
        return ""
    next_index = index + 1
    if next_index >= len(parts):
        return ""
    return parts[next_index]


def _audit_reviewed_count_text(project: dict[str, Any]) -> str:
    count = _as_int(project.get("audit_change_count", 0))
    if bool(project.get("audit_entries_limited", False)):
        return f"{count}+"
    return str(count)


def _audit_action_label(action: str) -> str:
    action_labels = {
        "create": "create",
        "delete": "delete",
        "get": "read",
        "patch": "patch",
        "update": "update",
    }
    return action_labels.get(action, action.replace("_", " "))


def _mesh_summary(item: CheckResult | None) -> list[str]:
    if item is None:
        return _missing_collector("mesh")
    lines = [
        _collector_section_heading("Service Mesh Health", item.status),
        f"- Summary: {item.summary}",
    ]
    lines.extend(_status_driver_lines(item))
    lines.append("")
    rows: list[list[str]] = []
    proxy_rows: list[list[str]] = []
    component_rows: list[list[str]] = []
    remote_cluster_rows: list[list[str]] = []
    proxy_status_rows: list[list[str]] = []
    proxy_log_rows: list[list[str]] = []
    for cluster in _as_dict_list(item.details.get("clusters", [])):
        ingress = _as_dict(cluster.get("ingress_gateway", {}))
        east_west = _as_dict(cluster.get("east_west_gateway", {}))
        istiod = _as_dict(cluster.get("istiod", {}))
        multicluster = _as_dict(cluster.get("multicluster_sync", {}))
        remote_clusters = _as_dict(cluster.get("remote_clusters", {}))
        remote_cluster_status = str(remote_clusters.get("status", ""))
        expected_remote_links = _as_int(multicluster.get("expected_remote_links", 0))
        synced_remote_links = _as_int(multicluster.get("synced_remote_clusters", 0))
        observed_remote_links = _as_int(multicluster.get("remote_cluster_count", 0))
        if expected_remote_links > 0:
            remote_sync_text = (
                f"{synced_remote_links} synced; expected at least {expected_remote_links}"
            )
        elif observed_remote_links > 0:
            remote_sync_text = f"{synced_remote_links} synced"
        elif remote_cluster_status:
            remote_sync_text = _format_status(remote_cluster_status)
        else:
            remote_sync_text = "n/a"
        rows.append(
            [
                str(cluster.get("cluster", "")),
                _format_status(str(cluster.get("status", "na"))),
                f"{_as_int(ingress.get('ready_pods', 0))}/{_as_int(ingress.get('total_pods', 0))}",
                (
                    f"{_as_int(east_west.get('ready_pods', 0))}/"
                    f"{_as_int(east_west.get('total_pods', 0))}"
                ),
                f"{_as_int(istiod.get('ready_pods', 0))}/{_as_int(istiod.get('total_pods', 0))}",
                remote_sync_text,
            ]
        )
        envoy_samples = _as_dict_list(cluster.get("envoy_proxy_samples", []))
        for name, component in (
            ("Ingress Gateway", ingress),
            ("East-West Gateway", east_west),
            ("Istiod", istiod),
        ):
            sample = _envoy_sample_for_component(component, envoy_samples)
            is_control_plane = name == "Istiod"
            if is_control_plane:
                proxy_state = "Not applicable"
                proxy_cluster_id = "Not applicable"
                proxy_network = "Not applicable"
                proxy_discovery_address = "Not applicable"
                notes = "Istiod is the mesh control plane; proxy Envoy metadata is not applicable."
            else:
                proxy_state = str(sample.get("state", "")) or "n/a"
                proxy_cluster_id = str(sample.get("cluster_id", "")) or "n/a"
                proxy_network = str(sample.get("network", "")) or "n/a"
                proxy_discovery_address = str(sample.get("discovery_address", "")) or "n/a"
                notes = str(sample.get("error", ""))
            component_rows.append(
                [
                    str(cluster.get("cluster", "")),
                    name,
                    f"{_as_int(component.get('ready_pods', 0))}/"
                    f"{_as_int(component.get('total_pods', 0))}",
                    ", ".join(_str_list(component.get("pod_names", []))) or "none",
                    ", ".join(_str_list(component.get("ready_pod_names", []))) or "none",
                    _mesh_component_version(component, sample),
                    proxy_state,
                    proxy_cluster_id,
                    proxy_network,
                    proxy_discovery_address,
                    notes,
                ]
            )
        remote_rows = _as_dict_list(remote_clusters.get("rows", []))
        if remote_rows:
            for remote in remote_rows:
                remote_cluster_rows.append(
                    [
                        str(cluster.get("cluster", "")),
                        str(remote.get("name", "")),
                        str(remote.get("sync_status", "")),
                        str(remote.get("istiod", "")),
                        _format_status(remote_cluster_status),
                        "",
                    ]
                )
        elif remote_cluster_status:
            remote_cluster_rows.append(
                [
                    str(cluster.get("cluster", "")),
                    "n/a",
                    "n/a",
                    "n/a",
                    _format_status(remote_cluster_status),
                    str(remote_clusters.get("error", "")),
                ]
            )
        proxy_status = _as_dict(cluster.get("proxy_status", {}))
        proxy_status_value = str(proxy_status.get("status", ""))
        proxy_status_items = _as_dict_list(proxy_status.get("rows", []))
        if proxy_status_items:
            for proxy_sync in proxy_status_items:
                proxy_status_rows.append(
                    [
                        str(cluster.get("cluster", "")),
                        str(proxy_sync.get("name", "")),
                        str(proxy_sync.get("cluster", "")),
                        str(proxy_sync.get("istiod", "")),
                        str(proxy_sync.get("version", "")),
                        str(proxy_sync.get("sync_state", "")),
                        _format_status(proxy_status_value),
                    ]
                )
        elif proxy_status_value:
            proxy_status_rows.append(
                [
                    str(cluster.get("cluster", "")),
                    "n/a",
                    "n/a",
                    "n/a",
                    "n/a",
                    str(proxy_status.get("error", "")),
                    _format_status(proxy_status_value),
                ]
            )
        mesh_proxy_items = _as_dict_list(cluster.get("mesh_api_proxies", []))
        legacy_proxy_items = (
            [] if "mesh_api_proxies" in cluster else _as_dict_list(cluster.get("squid_proxies", []))
        )
        for proxy, fallback_type in [(proxy, "n/a") for proxy in mesh_proxy_items] + [
            (proxy, "squid") for proxy in legacy_proxy_items
        ]:
            proxy_type = str(proxy.get("type", proxy.get("proxy_type", ""))).strip()
            proxy_rows.append(
                [
                    str(cluster.get("cluster", "")),
                    str(proxy.get("name", "")),
                    proxy_type or fallback_type,
                    str(proxy.get("namespace", "")),
                    _format_status(str(proxy.get("status", "na"))),
                    (
                        f"{_as_int(proxy.get('ready_replicas', 0))}/"
                        f"{_as_int(proxy.get('desired_replicas', 0))}"
                    ),
                    (f"{_as_int(proxy.get('pod_ready', 0))}/{_as_int(proxy.get('pod_total', 0))}"),
                    str(proxy.get("service_type", "")) or "n/a",
                    ", ".join(_str_list(proxy.get("service_ports", []))) or "none",
                    ", ".join(_str_list(proxy.get("endpoint_ports", []))) or "none",
                    str(proxy.get("endpoint_address_count", 0)),
                    ", ".join(_str_list(proxy.get("load_balancer_ingress", []))) or "none",
                    _format_mesh_proxy_config_status(proxy),
                    str(proxy.get("note", "")),
                ]
            )
            log_summary = _as_dict(proxy.get("control_plane_tunnel_logs", {}))
            if log_summary:
                latest_success = _as_dict(log_summary.get("latest_success", {}))
                latest_failure = _as_dict(log_summary.get("latest_failure", {}))
                proxy_log_rows.append(
                    [
                        str(cluster.get("cluster", "")),
                        str(proxy.get("name", "")),
                        str(proxy.get("namespace", "")),
                        _format_status(str(log_summary.get("status", "na"))),
                        str(log_summary.get("tunnel_success_count", 0)),
                        str(log_summary.get("tunnel_failure_count", 0)),
                        str(latest_success.get("target", "")) or "none",
                        _mesh_proxy_log_note(log_summary, latest_failure),
                    ]
                )
    lines.extend(
        _markdown_table(
            headers=[
                "Cluster",
                "Assessment",
                "Ingress Gateway Ready",
                "East-West Gateway Ready",
                "Istiod Ready",
                "Remote Cluster Sync",
            ],
            rows=rows,
        )
    )
    lines.append("")
    lines.append("Mesh Component Readiness:")
    lines.append("")
    lines.append(
        "- Proxy Envoy columns apply to Envoy-based gateway components. Istiod is shown "
        "with readiness and version because it is the mesh control plane."
    )
    lines.append("")
    lines.extend(
        _markdown_table(
            headers=[
                "Cluster",
                "Mesh Component",
                "Ready Pods",
                "Pod Names",
                "Ready Pod Names",
                "Version",
                "Proxy Envoy State",
                "Proxy Cluster ID",
                "Proxy Network",
                "Proxy Discovery Address",
                "Notes",
            ],
            rows=component_rows,
        )
    )
    lines.append("")
    lines.append("Remote Cluster Sync:")
    lines.append("")
    if remote_cluster_rows:
        lines.extend(
            _markdown_table(
                headers=[
                    "Cluster",
                    "Remote Cluster",
                    "Sync Status",
                    "Istiod",
                    "Evidence Status",
                    "Note",
                ],
                rows=remote_cluster_rows,
            )
        )
    else:
        lines.append("- No remote cluster sync evidence was available.")
    lines.append("")
    lines.append("Mesh API Proxy Readiness:")
    lines.append("")
    if proxy_rows:
        lines.extend(
            _markdown_table(
                headers=[
                    "Cluster",
                    "Proxy Name",
                    "Proxy Type",
                    "Namespace",
                    "Assessment",
                    "Deployment Ready/Desired",
                    "Pods Ready/Total",
                    "Service Mode",
                    "Service Ports",
                    "Endpoint Ports",
                    "Ready Endpoints",
                    "Load Balancer Address",
                    "Configured Resources",
                    "Note",
                ],
                rows=proxy_rows,
            )
        )
    else:
        lines.append("- No mesh API proxy checks configured for this environment.")
    lines.append("")
    lines.append("Proxy Sync Status:")
    lines.append("")
    if proxy_status_rows:
        lines.extend(
            _markdown_table(
                headers=[
                    "Cluster",
                    "Proxy",
                    "Proxy Cluster",
                    "Istiod",
                    "Version",
                    "Sync State",
                    "Evidence Status",
                ],
                rows=proxy_status_rows,
            )
        )
    else:
        lines.append("- No proxy sync status evidence was available.")
    lines.append("")
    lines.append("Mesh API Proxy Control Plane Tunnel Evidence:")
    lines.append("")
    if proxy_log_rows:
        lines.extend(
            _markdown_table(
                headers=[
                    "Cluster",
                    "Proxy",
                    "Namespace",
                    "Evidence Status",
                    "Successful Tunnels",
                    "Failed Tunnels",
                    "Latest Healthy Target",
                    "Note",
                ],
                rows=proxy_log_rows,
            )
        )
    else:
        lines.append("- No mesh API proxy tunnel log evidence was available.")
    lines.append("")
    return lines


def _envoy_sample_for_component(
    component: dict[str, Any],
    samples: list[dict[str, Any]],
) -> dict[str, Any]:
    pod_names = set(_str_list(component.get("pod_names", [])))
    ready_pod_names = set(_str_list(component.get("ready_pod_names", [])))
    known_pods = pod_names | ready_pod_names
    if not known_pods:
        return {}
    for sample in samples:
        if str(sample.get("pod", "")) in known_pods:
            return sample
    return {}


def _mesh_component_version(component: dict[str, Any], sample: dict[str, Any]) -> str:
    sample_version = str(sample.get("istio_version", "")).strip()
    if sample_version:
        return sample_version
    component_version = str(component.get("version", "")).strip()
    if component_version:
        return component_version
    versions = _str_list(component.get("versions", []))
    if versions:
        rendered = ", ".join(versions[:3])
        if len(versions) > 3:
            rendered = f"{rendered}, +{len(versions) - 3} more"
        return rendered
    return "n/a"


def _mesh_proxy_log_note(
    log_summary: dict[str, Any],
    latest_failure: dict[str, Any],
) -> str:
    error = str(log_summary.get("error", "")).strip()
    if error:
        return error
    if latest_failure:
        code = str(latest_failure.get("code", ""))
        target = str(latest_failure.get("target", ""))
        return f"latest failed tunnel {code} to {target}".strip()
    if _as_int(log_summary.get("tunnel_line_count", 0)) == 0:
        return "no CONNECT tunnel entries found in sampled proxy logs"
    return ""


def _format_mesh_proxy_config_status(proxy: dict[str, Any]) -> str:
    parts: list[str] = []
    for label, name_key, found_key in (
        ("configmap", "configmap", "configmap_found"),
        ("cert", "certificate_secret", "certificate_secret_found"),
        ("token", "token_secret", "token_secret_found"),
    ):
        name = str(proxy.get(name_key, "")).strip()
        if not name:
            continue
        found = proxy.get(found_key)
        if found is True:
            parts.append(f"{label} present")
        elif found is False:
            parts.append(f"{label} missing")
        else:
            parts.append(f"{label} configured")
    return "; ".join(parts) or "n/a"


def _network_summary(item: CheckResult | None) -> list[str]:
    if item is None:
        return _missing_collector("network")
    lines = [
        _collector_section_heading("Network Inventory And DNS Controls", item.status),
        f"- Summary: {item.summary}",
    ]
    lines.extend(_status_driver_lines(item))
    lines.append("")
    rows: list[list[str]] = []
    projects = _as_dict_list(item.details.get("projects", []))
    network_total = 0
    firewall_total = 0
    peering_total = 0
    dns_zone_total = 0
    for project in projects:
        network_total += _as_int(project.get("network_count", 0))
        firewall_total += _as_int(project.get("firewall_rule_count", 0))
        peering_total += _as_int(project.get("peering_count", 0))
        dns_zone_total += _as_int(project.get("dns_zone_count", 0))
        rows.append(
            [
                str(project.get("project", "")),
                _format_status(str(project.get("status", "na"))),
                str(project.get("network_count", 0)),
                str(project.get("firewall_rule_count", 0)),
                str(project.get("firewall_disabled_count", 0)),
                str(project.get("peering_count", 0)),
                str(project.get("peering_inactive_count", 0)),
                str(project.get("forwarding_rule_count", 0)),
                str(project.get("dns_zone_count", 0)),
            ]
        )
    lines.extend(
        _markdown_table(
            headers=[
                "Project",
                "Assessment",
                "VPCs",
                "Firewall Rules",
                "Disabled Firewall Rules",
                "VPC Peerings",
                "Inactive Peerings",
                "Forwarding Rules",
                "DNS Zones",
            ],
            rows=rows,
        )
    )
    lines.append("")
    network_rows: list[list[str]] = []
    firewall_rows: list[list[str]] = []
    zone_rows: list[list[str]] = []
    peering_rows: list[list[str]] = []
    required_zone_finding_rows: list[list[str]] = []
    for project in projects:
        project_name = str(project.get("project", ""))
        for network in _as_dict_list(project.get("networks", [])):
            network_rows.append(
                [
                    project_name,
                    str(network.get("name", "")),
                    "yes" if bool(network.get("auto_create_subnetworks", False)) else "no",
                    str(network.get("routing_mode", "")),
                    str(network.get("mtu", "")),
                    str(network.get("peering_count", 0)),
                ]
            )
        for firewall in _as_dict_list(project.get("firewall_rules", [])):
            firewall_rows.append(
                [
                    project_name,
                    str(firewall.get("name", "")),
                    str(firewall.get("network", "")),
                    str(firewall.get("direction", "")),
                    str(firewall.get("priority", "")),
                    "yes" if bool(firewall.get("disabled", False)) else "no",
                    str(firewall.get("source_ranges", "")),
                    str(firewall.get("target_tags", "")),
                    str(firewall.get("target_service_accounts", "")),
                    str(firewall.get("allowed", "")),
                    str(firewall.get("denied", "")),
                ]
            )
        for zone in _as_dict_list(project.get("dns_zones", [])):
            zone_rows.append(
                [
                    project_name,
                    str(zone.get("name", "")),
                    str(zone.get("dns_name", "")),
                    str(zone.get("visibility", "")),
                    str(zone.get("record_set_count", "n/a")),
                    str(zone.get("network_count", 0)),
                    ", ".join(_last_path_part(item) for item in _str_list(zone.get("networks", [])))
                    or "none",
                ]
            )
        for peering in _as_dict_list(project.get("peerings", [])):
            peering_rows.append(
                [
                    project_name,
                    str(peering.get("network", "")),
                    str(peering.get("name", "")),
                    str(peering.get("state", "")),
                    str(peering.get("peer_network", "")),
                    "yes" if bool(peering.get("import_custom_routes", False)) else "no",
                    "yes" if bool(peering.get("export_custom_routes", False)) else "no",
                ]
            )
        for finding in _as_dict_list(project.get("dns_policy_findings", [])):
            if str(finding.get("check", "")) != "required_internal_zones":
                continue
            required_zone_finding_rows.append(
                [
                    project_name,
                    _format_status(str(finding.get("severity", "na"))),
                    str(finding.get("check", "")),
                    str(finding.get("message", "")),
                ]
            )
    lines.append(_limited_table_heading("VPC Networks", max(network_total, len(network_rows)), 50))
    lines.append("")
    lines.extend(
        _markdown_table(
            headers=[
                "Project",
                "VPC",
                "Auto-created Subnets",
                "Routing Mode",
                "Network MTU",
                "VPC Peerings",
            ],
            rows=network_rows[:50],
        )
    )
    lines.append("")
    lines.append(
        _limited_table_heading("Firewall Rules", max(firewall_total, len(firewall_rows)), 50)
    )
    lines.append("")
    lines.extend(
        _markdown_table(
            headers=[
                "Project",
                "Rule",
                "VPC",
                "Direction",
                "Priority",
                "Disabled",
                "Source IP Ranges",
                "Target Tags",
                "Target Accounts",
                "Allowed Traffic",
                "Denied Traffic",
            ],
            rows=firewall_rows[:50],
        )
    )
    lines.append("")
    lines.append(
        _limited_table_heading("DNS Managed Zones", max(dns_zone_total, len(zone_rows)), 50)
    )
    lines.append("")
    lines.extend(
        _markdown_table(
            headers=[
                "Project",
                "Zone Name",
                "DNS Domain",
                "Visibility",
                "DNS Records",
                "Attached VPCs",
                "VPC Names",
            ],
            rows=zone_rows[:50],
        )
    )
    lines.append("")
    lines.append(_limited_table_heading("VPC Peerings", max(peering_total, len(peering_rows)), 50))
    lines.append("")
    lines.extend(
        _markdown_table(
            headers=[
                "Project",
                "VPC",
                "Peering Name",
                "State",
                "Peer Network",
                "Imports Custom Routes",
                "Exports Custom Routes",
            ],
            rows=peering_rows[:50],
        )
    )
    lines.append("")
    if required_zone_finding_rows:
        lines.append(
            _limited_table_heading(
                "Required Internal DNS Zone Findings",
                len(required_zone_finding_rows),
                50,
            )
        )
        lines.append("")
        lines.extend(
            _markdown_table(
                headers=["Project", "Assessment", "Check", "Finding"],
                rows=required_zone_finding_rows[:50],
            )
        )
        lines.append("")
    internal_dns = _as_dict(item.details.get("internal_dns", {}))
    required_service_fqdns = _str_list(internal_dns.get("required_service_fqdns", []))
    internal_dns_rows = _as_dict_list(internal_dns.get("clusters", []))
    internal_dns_status = str(internal_dns.get("status", Status.OK.value))
    should_render_internal_dns = bool(required_service_fqdns or internal_dns_rows) or (
        internal_dns_status
        not in (
            Status.OK.value,
            Status.SKIPPED_CONFIG.value,
        )
    )
    if should_render_internal_dns:
        lines.append("In-Cluster DNS Resolution Checks:")
        lines.append("")
        lines.append(
            "- Source: Kubernetes API service inventory check for required service "
            "FQDNs in each cluster."
        )
        required_fqdns = ", ".join(f"`{fqdn}`" for fqdn in required_service_fqdns)
        if required_fqdns:
            lines.append(f"- Required service FQDNs: {required_fqdns}")
        reason = str(internal_dns.get("reason", "")).strip()
        if reason and not internal_dns_rows:
            lines.append(f"- Reason: {reason}")
        lines.append("")
        lines.extend(
            _markdown_table(
                headers=[
                    "Cluster",
                    "Project",
                    "Region",
                    "Assessment",
                    "FQDNs Checked",
                    "Failed FQDNs",
                ],
                rows=[
                    [
                        str(row.get("cluster", "")),
                        str(row.get("project", "")),
                        str(row.get("region", "")),
                        _format_status(str(row.get("status", "na"))),
                        str(row.get("checked_fqdn_count", 0)),
                        ", ".join(_str_list(row.get("failed_fqdns", []))) or "none",
                    ]
                    for row in internal_dns_rows
                ],
            )
        )
        lines.append("")
    return lines


def _current_gke_node_lookup(
    item: CheckResult | None,
) -> tuple[dict[tuple[str, str], dict[str, Any]], set[str]]:
    if item is None:
        return {}, set()
    nodes: dict[tuple[str, str], dict[str, Any]] = {}
    clusters_with_inventory: set[str] = set()
    for cluster in _as_dict_list(item.details.get("clusters", [])):
        cluster_name = str(cluster.get("cluster", "")).strip()
        if not cluster_name:
            continue
        if "node_inventory" in cluster:
            clusters_with_inventory.add(cluster_name)
        for node in _as_dict_list(cluster.get("node_inventory", [])):
            node_name = str(node.get("name", "")).strip()
            if node_name:
                nodes[(cluster_name, node_name)] = node
    return nodes, clusters_with_inventory


def _gke_node_state(
    cluster_name: str,
    node_name: str,
    current_nodes: dict[tuple[str, str], dict[str, Any]],
    current_node_clusters: set[str],
) -> str:
    if (cluster_name, node_name) in current_nodes:
        return "Active"
    if cluster_name in current_node_clusters:
        return "Historical"
    return "Unknown"


def _current_node_ready_text(node: dict[str, Any] | None) -> str:
    if node is None:
        return "n/a"
    return "yes" if bool(node.get("ready", False)) else "no"


def _gke_cluster_cpu_note(
    project: dict[str, Any],
    cluster_name: str,
    current_nodes: dict[tuple[str, str], dict[str, Any]],
    *,
    observed: bool,
) -> str:
    if not observed:
        return "Node CPU telemetry was not available."
    node_rows = [
        row
        for row in _as_dict_list(project.get("gke_node_utilization", []))
        if str(row.get("cluster", "")) == cluster_name
    ]
    if not node_rows:
        return ""
    peak_node = max(node_rows, key=lambda row: _as_float(row.get("cpu_allocatable_peak_percent")))
    peak_node_name = str(peak_node.get("node", ""))
    if (cluster_name, peak_node_name) in current_nodes:
        return ""
    active_rows = [
        row for row in node_rows if (cluster_name, str(row.get("node", ""))) in current_nodes
    ]
    if not active_rows:
        return "7-day peak came from a historical node."
    active_peak = max(_as_float(row.get("cpu_allocatable_peak_percent")) for row in active_rows)
    return (
        "7-day peak came from a historical node; highest active node observed "
        f"{_format_percent(active_peak)}."
    )


def _resource_trend_summary(summary: str) -> str:
    return (
        summary.replace("7d trend metrics", "7-day resource usage trends")
        .replace("7d", "7-day")
        .replace("trend metrics", "resource usage trends")
    )


def _service_disabled_in_config(item: CheckResult | None, service_key: str) -> bool:
    if item is None:
        return False
    rows = _as_dict_list(item.details.get(service_key, []))
    return _service_rows_disabled_in_config(rows)


def _service_rows_disabled_in_config(rows: list[dict[str, Any]]) -> bool:
    if not rows:
        return False
    return all(
        str(row.get("status", "")) == Status.SKIPPED_CONFIG.value
        and "disabled in config" in str(row.get("reason", ""))
        for row in rows
    )


def _backup_policy_out_of_scope(item: CheckResult) -> bool:
    policy = _as_dict(item.details.get("policy", {}))
    return str(policy.get("status", "")) == "out_of_scope"


def _trend_metrics_summary(
    item: CheckResult | None,
    kubernetes_health: CheckResult | None = None,
    services: CheckResult | None = None,
) -> list[str]:
    if item is None:
        return _missing_collector("trend_metrics")
    window_days = _as_int(item.details.get("window_days", 7))
    if window_days <= 0:
        window_days = 7
    current_nodes, current_node_clusters = _current_gke_node_lookup(kubernetes_health)
    lines = [
        _collector_section_heading("Infrastructure Utilization Trends", item.status),
        f"- Summary: {_resource_trend_summary(item.summary)}",
        f"- Bold utilization values are observed values >= {_UTILIZATION_WARNING_THRESHOLD:.0f}%.",
    ]
    lines.extend(_status_driver_lines(item))
    lines.append("")

    sql_rows: list[list[str]] = []
    cpu_activity_rows: list[list[str]] = []
    vm_rows: list[list[str]] = []
    vm_notes: list[str] = []
    gke_cluster_rows: list[list[str]] = []
    gke_node_rows: list[list[str]] = []
    gke_namespace_rows: list[list[str]] = []
    gke_pod_rows: list[list[str]] = []
    redis_rows: list[list[str]] = []
    kafka_rows: list[list[str]] = []
    redis_utilization_rows: list[list[str]] = []
    kafka_utilization_rows: list[list[str]] = []
    gke_errors: list[str] = []
    redis_errors: list[str] = []
    kafka_errors: list[str] = []
    redis_enabled = not _service_disabled_in_config(services, "redis")
    kafka_enabled = not _service_disabled_in_config(services, "managed_kafka")
    for project in _as_dict_list(item.details.get("projects", [])):
        project_name = str(project.get("project", ""))
        for sql in _as_dict_list(project.get("cloud_sql", [])):
            instance_name = str(sql.get("instance", ""))
            cpu_peak = _as_float(sql.get("cpu_peak_percent", 0.0))
            sql_rows.append(
                [
                    project_name,
                    instance_name,
                    _format_utilization_percent(cpu_peak),
                    _format_utilization_percent(sql.get("memory_peak_percent", 0.0)),
                    _format_utilization_percent(sql.get("disk_peak_percent", 0.0)),
                ]
            )
        vm_memory_lookup = {
            str(vm.get("instance", "")): vm
            for vm in _as_dict_list(project.get("non_gke_vm_memory", []))
            if str(vm.get("instance", ""))
        }
        seen_vm_instances: set[str] = set()
        for vm in _as_dict_list(project.get("non_gke_vm_cpu", [])):
            instance_name = str(vm.get("instance", ""))
            seen_vm_instances.add(instance_name)
            cpu_peak = _as_float(vm.get("cpu_peak_percent", 0.0))
            memory_row = _as_dict(vm_memory_lookup.get(instance_name, {}))
            memory_peak = _as_float(memory_row.get("memory_peak_percent", 0.0))
            telemetry_status = str(vm.get("telemetry_status", "ok"))
            cpu_source = str(vm.get("cpu_source", "gcp_native"))
            memory_note = str(memory_row.get("note", ""))
            note = "; ".join(item for item in (str(vm.get("note", "")), memory_note) if item)
            vm_rows.append(
                [
                    project_name,
                    instance_name,
                    cpu_source,
                    (
                        _format_utilization_percent(memory_peak)
                        if str(memory_row.get("telemetry_status", telemetry_status)) == "ok"
                        else "n/a"
                    ),
                    note,
                ]
            )
            cpu_activity_rows.append(
                [
                    project_name,
                    "Compute VM",
                    instance_name,
                    "Compute Engine CPU utilization",
                    _format_utilization_percent(cpu_peak) if telemetry_status == "ok" else "n/a",
                    _format_cpu_spare_capacity(vm, observed=telemetry_status == "ok"),
                    "Yes" if telemetry_status == "ok" else "No",
                    str(vm.get("note", "")),
                ]
            )
        for instance_name, memory_row in vm_memory_lookup.items():
            if instance_name in seen_vm_instances:
                continue
            telemetry_status = str(memory_row.get("telemetry_status", "ok"))
            memory_peak = _as_float(memory_row.get("memory_peak_percent", 0.0))
            note = str(memory_row.get("note", ""))
            vm_rows.append(
                [
                    project_name,
                    instance_name,
                    "n/a",
                    _format_utilization_percent(memory_peak) if telemetry_status == "ok" else "n/a",
                    note,
                ]
            )
            cpu_activity_rows.append(
                [
                    project_name,
                    "Compute VM",
                    instance_name,
                    "Compute Engine CPU utilization",
                    "n/a",
                    "n/a",
                    "No",
                    "CPU telemetry was not available; only memory telemetry was returned.",
                ]
            )
        for cluster in _as_dict_list(project.get("gke_cluster_utilization", [])):
            cluster_name = str(cluster.get("cluster", ""))
            max_node_cpu_percent = _as_float(
                cluster.get("max_node_cpu_allocatable_peak_percent", 0.0)
            )
            gke_cluster_rows.append(
                [
                    project_name,
                    cluster_name,
                    _format_float(cluster.get("cpu_peak_cores", 0.0), 2),
                    _format_gib_from_float(cluster.get("memory_peak_bytes", 0.0)),
                ]
            )
            cpu_activity_rows.append(
                [
                    project_name,
                    "GKE cluster",
                    cluster_name,
                    "Highest node CPU allocatable utilization",
                    _format_utilization_percent(max_node_cpu_percent),
                    _format_cpu_spare_capacity(
                        cluster,
                        observed=_as_int(cluster.get("node_series_count", 0)) > 0,
                        key="max_node_cpu_idle_capacity_at_peak_percent",
                    ),
                    "Yes" if _as_int(cluster.get("node_series_count", 0)) > 0 else "No",
                    _gke_cluster_cpu_note(
                        project,
                        cluster_name,
                        current_nodes,
                        observed=_as_int(cluster.get("node_series_count", 0)) > 0,
                    ),
                ]
            )
        for node in _as_dict_list(project.get("gke_node_utilization", [])):
            cluster_name = str(node.get("cluster", ""))
            node_name = str(node.get("node", ""))
            current_node = current_nodes.get((cluster_name, node_name))
            node_state = _gke_node_state(
                cluster_name,
                node_name,
                current_nodes,
                current_node_clusters,
            )
            is_active_node = node_state == "Active"
            current_ready = _current_node_ready_text(current_node)
            gke_node_rows.append(
                [
                    project_name,
                    cluster_name,
                    _bold_if(node_name, is_active_node),
                    _bold_if(node_state, is_active_node),
                    _bold_if(current_ready, is_active_node),
                    _format_utilization_percent(node.get("cpu_allocatable_peak_percent", 0.0)),
                    _format_utilization_percent(node.get("memory_allocatable_peak_percent", 0.0)),
                ]
            )
        for namespace in _as_dict_list(project.get("gke_namespace_utilization", [])):
            gke_namespace_rows.append(
                [
                    project_name,
                    str(namespace.get("cluster", "")),
                    str(namespace.get("namespace", "")),
                    _format_float(namespace.get("cpu_avg_cores", 0.0), 2),
                    _format_float(namespace.get("cpu_p95_cores", 0.0), 2),
                    _format_float(namespace.get("cpu_peak_cores", 0.0), 2),
                    _format_gib_from_float(namespace.get("memory_avg_bytes", 0.0)),
                    _format_gib_from_float(namespace.get("memory_p95_bytes", 0.0)),
                    _format_gib_from_float(namespace.get("memory_peak_bytes", 0.0)),
                ]
            )
        for pod in _as_dict_list(project.get("gke_pod_utilization", [])):
            gke_pod_rows.append(
                [
                    project_name,
                    str(pod.get("cluster", "")),
                    str(pod.get("namespace", "")),
                    str(pod.get("pod", "")),
                    _format_observed_float(
                        pod.get("cpu_peak_cores"),
                        2,
                        bool(pod.get("cpu_peak_observed", pod.get("cpu_peak_cores", 0.0) != 0.0)),
                    ),
                    _format_observed_gib(
                        pod.get("memory_peak_bytes"),
                        bool(
                            pod.get(
                                "memory_peak_observed",
                                pod.get("memory_peak_bytes", 0.0) != 0.0,
                            )
                        ),
                    ),
                ]
            )
        for redis in _as_dict_list(project.get("redis_throughput", [])):
            instance_name = str(redis.get("instance", ""))
            direction = str(redis.get("direction", "total"))
            avg_bytes = _as_float(redis.get("bytes_per_second_avg", 0.0))
            redis_rows.append(
                [
                    project_name,
                    instance_name,
                    direction,
                    _format_bytes_per_second(avg_bytes),
                    _format_bytes_per_second(redis.get("bytes_per_second_peak", 0.0)),
                ]
            )
        for kafka in _as_dict_list(project.get("kafka_throughput", [])):
            cluster_name = str(kafka.get("cluster", ""))
            bytes_in_avg = _as_float(kafka.get("bytes_in_avg", 0.0))
            kafka_rows.append(
                [
                    project_name,
                    cluster_name,
                    _format_bytes_per_second(bytes_in_avg),
                    _format_bytes_per_second(kafka.get("bytes_out_avg", 0.0)),
                    _format_rate(kafka.get("messages_in_avg", 0.0)),
                    _format_bytes_per_second(kafka.get("bytes_in_peak", 0.0)),
                    _format_bytes_per_second(kafka.get("bytes_out_peak", 0.0)),
                    _format_rate(kafka.get("messages_in_peak", 0.0)),
                ]
            )
        for redis in _as_dict_list(project.get("redis_utilization", [])):
            redis_utilization_rows.append(
                [
                    project_name,
                    str(redis.get("instance", "")),
                    _format_status(str(redis.get("status", "na"))),
                    (
                        _format_utilization_percent(redis.get("cpu_peak_percent", 0.0))
                        if bool(redis.get("cpu_observed", True))
                        else "n/a"
                    ),
                    (
                        _format_utilization_percent(redis.get("memory_peak_percent", 0.0))
                        if bool(redis.get("memory_observed", True))
                        else "n/a"
                    ),
                    _format_rate(redis.get("eviction_rate_peak", 0.0)),
                    _format_float(redis.get("connected_clients_peak", 0.0), 0),
                    _format_rate(redis.get("ops_per_second_peak", 0.0)),
                    _format_duration_seconds(redis.get("replication_lag_peak_seconds", 0.0)),
                    str(redis.get("note", "")),
                ]
            )
        for kafka in _as_dict_list(project.get("kafka_utilization", [])):
            kafka_utilization_rows.append(
                [
                    project_name,
                    str(kafka.get("cluster", "")),
                    _format_status(str(kafka.get("status", "na"))),
                    _format_utilization_percent(kafka.get("broker_cpu_peak_percent", 0.0)),
                    _format_utilization_percent(kafka.get("broker_memory_peak_percent", 0.0)),
                    _format_utilization_percent(kafka.get("broker_disk_peak_percent", 0.0)),
                    _format_float(kafka.get("under_replicated_partitions_peak", 0.0), 0),
                    _format_float(kafka.get("offline_partitions_peak", 0.0), 0),
                    _format_float(kafka.get("consumer_lag_peak", 0.0), 0),
                ]
            )
        redis_error = str(project.get("redis_throughput_error", "")).strip()
        kafka_error = str(project.get("kafka_throughput_error", "")).strip()
        gke_error = str(project.get("gke_utilization_error", "")).strip()
        vm_cpu_error = str(project.get("vm_cpu_error", "")).strip()
        vm_memory_error = str(project.get("vm_memory_error", "")).strip()
        redis_util_error = str(project.get("redis_utilization_error", "")).strip()
        kafka_util_error = str(project.get("kafka_utilization_error", "")).strip()
        if gke_error:
            gke_errors.append(f"{project_name}: {gke_error}")
        if vm_cpu_error:
            vm_notes.append(f"{project_name}: {vm_cpu_error}")
        if vm_memory_error:
            vm_notes.append(f"{project_name}: {vm_memory_error}")
        if redis_error:
            redis_errors.append(f"{project_name}: {redis_error}")
        if redis_util_error:
            redis_errors.append(f"{project_name}: {redis_util_error}")
        if kafka_error:
            kafka_errors.append(f"{project_name}: {kafka_error}")
        if kafka_util_error:
            kafka_errors.append(f"{project_name}: {kafka_util_error}")

    cpu_state_summary_rows = _cpu_usage_summary_rows(cpu_activity_rows)

    lines.append(f"Infrastructure CPU Utilization Summary ({window_days}-Day):")
    lines.append("")
    lines.append(
        "- Spare CPU Capacity At Peak is derived as 100% minus Highest CPU Observed, "
        "floored at 0%; it is a remaining-capacity calculation, not separately collected "
        "CPU idle-time telemetry. VM rows use Compute Engine CPU utilization. GKE cluster "
        "rows use the highest node CPU allocatable utilization observed in that cluster; "
        "the row note states when the peak came from a historical node."
    )
    lines.append("")
    lines.append("CPU Usage And Spare Capacity Summary:")
    lines.append("")
    lines.extend(
        _markdown_table(
            headers=[
                "Project",
                "Resource Type",
                "Total Resources",
                "CPU Telemetry Available",
                "CPU Telemetry Missing",
                "Highest CPU Observed (%)",
                "Spare CPU Capacity At Peak (%)",
            ],
            rows=cpu_state_summary_rows,
        )
    )
    lines.append("")
    lines.extend(
        _markdown_table(
            headers=[
                "Project",
                "Resource Type",
                "Resource",
                "CPU Evidence Basis",
                "Highest CPU Observed (%)",
                "Spare CPU Capacity At Peak (%)",
                "CPU Telemetry Available",
                "Telemetry Note",
            ],
            rows=cpu_activity_rows,
        )
    )
    lines.append("")
    lines.append("Cloud SQL Utilization Summary (7-Day):")
    lines.append("")
    lines.append(
        "- `Highest Observed` means the highest point-in-time usage value Cloud Monitoring "
        f"returned during the last {window_days} days. It is not the weekly average and "
        "it is not total usage over the week."
    )
    lines.append("")
    lines.extend(
        _markdown_table(
            headers=[
                "Project",
                "Instance",
                "Highest CPU Observed (%)",
                "Highest Memory Observed (%)",
                "Highest Disk Observed (%)",
            ],
            rows=sql_rows,
        )
    )
    lines.append("")
    lines.append("VM Telemetry Source And Memory Summary (7-Day):")
    lines.append("")
    lines.append(
        "- VM CPU values are reported once in the CPU activity summary above. VM memory "
        "requires Ops Agent and is shown here with telemetry source notes."
    )
    lines.append("")
    lines.extend(
        _markdown_table(
            headers=[
                "Project",
                "Instance",
                "CPU Source",
                "Highest Memory Observed (%)",
                "Note",
            ],
            rows=vm_rows,
        )
    )
    if vm_notes:
        lines.append("")
        lines.append("- VM telemetry notes:")
        for note in vm_notes[:10]:
            lines.append(f"  - {note}")
    lines.append("")
    lines.append(f"GKE Workload Usage Summary ({window_days}-Day):")
    lines.append("")
    lines.append(
        "- Workload CPU is summed container CPU usage for the cluster, reported in cores. "
        "This is workload demand, not a cluster saturation percentage. Node saturation is "
        "reported separately in the CPU activity summary and node utilization table."
    )
    lines.append("")
    lines.extend(
        _markdown_table(
            headers=[
                "Project",
                "Cluster",
                "Highest Workload CPU Observed (cores)",
                "Highest Workload Memory Observed (GiB)",
            ],
            rows=gke_cluster_rows,
        )
    )
    lines.append("")
    lines.append(
        _cloud_monitoring_limited_heading(
            "GKE Node Highest Observed Allocatable Utilization",
            window_days=window_days,
            row_count=len(gke_node_rows),
            limit=_TREND_ROWS_DISPLAY_LIMIT,
            limit_label="Top",
        )
    )
    lines.append("")
    lines.append(
        "- Node State compares current Kubernetes node inventory with Cloud Monitoring history; "
        "Historical means the node was seen in the trend window but is not registered now."
    )
    lines.append("")
    lines.extend(
        _markdown_table(
            headers=[
                "Project",
                "Cluster",
                "Node",
                "Node State",
                "Ready Now",
                "Highest CPU Observed (%)",
                "Highest Memory Observed (%)",
            ],
            rows=gke_node_rows[:_TREND_ROWS_DISPLAY_LIMIT],
        )
    )
    lines.append("")
    lines.append(
        _cloud_monitoring_limited_heading(
            "GKE Namespace 7-Day Usage Trends",
            window_days=window_days,
            row_count=len(gke_namespace_rows),
            limit=_TREND_ROWS_DISPLAY_LIMIT,
            limit_label="Top",
        )
    )
    lines.append("")
    lines.append(
        "- CPU is an hourly rate summed across containers by namespace; memory is an "
        "hourly max sample summed by namespace. This is namespace-level, not per-pod."
    )
    lines.append("")
    lines.extend(
        _markdown_table(
            headers=[
                "Project",
                "Cluster",
                "Namespace",
                "Average CPU (cores)",
                "P95 CPU (cores)",
                "Highest CPU Observed (cores)",
                "Average Memory (GiB)",
                "P95 Memory (GiB)",
                "Highest Memory Observed (GiB)",
            ],
            rows=gke_namespace_rows[:_TREND_ROWS_DISPLAY_LIMIT],
        )
    )
    lines.append("")
    lines.append(
        _cloud_monitoring_limited_heading(
            "GKE Pod Highest Observed Usage",
            window_days=window_days,
            row_count=len(gke_pod_rows),
            limit=_TREND_ROWS_DISPLAY_LIMIT,
            limit_label="Top",
        )
    )
    lines.append("")
    lines.append(
        "- Pod CPU `Highest Observed` is the highest point-in-time CPU rate returned "
        "for the pod; pod memory `Highest Observed` is the highest memory value returned."
    )
    lines.append("- `n/a` means that specific pod value was not returned by the top-N query.")
    lines.append("")
    lines.extend(
        _markdown_table(
            headers=[
                "Project",
                "Cluster",
                "Namespace",
                "Pod",
                "Highest CPU Observed (cores)",
                "Highest Memory Observed (GiB)",
            ],
            rows=gke_pod_rows[:_TREND_ROWS_DISPLAY_LIMIT],
        )
    )
    if gke_errors:
        lines.append("")
        lines.append("- GKE utilization notes:")
        for error in gke_errors[:10]:
            lines.append(f"  - {error}")
    if redis_enabled:
        lines.append("")
        lines.append(f"Redis Throughput Trend ({window_days}-Day):")
        lines.append("")
        lines.extend(
            _markdown_table(
                headers=[
                    "Project",
                    "Instance",
                    "Traffic Direction",
                    "Average Throughput",
                    "Highest Throughput Observed",
                ],
                rows=redis_rows,
            )
        )
        lines.append("")
        lines.append(f"Redis Utilization Details ({window_days}-Day):")
        lines.append("")
        lines.extend(
            _markdown_table(
                headers=[
                    "Project",
                    "Instance",
                    "Assessment",
                    "Highest CPU Observed (%)",
                    "Highest Memory Observed (%)",
                    "Evictions (/s)",
                    "Connected Clients",
                    "Ops (/s)",
                    "Replication Lag",
                    "Metric Note",
                ],
                rows=redis_utilization_rows,
            )
        )
        if redis_errors:
            lines.append("")
            lines.append("- Redis data notes:")
            for error in redis_errors[:10]:
                lines.append(f"  - {error}")
    if kafka_enabled:
        lines.append("")
        lines.append(f"Managed Kafka Throughput Trend ({window_days}-Day):")
        lines.append("")
        lines.extend(
            _markdown_table(
                headers=[
                    "Project",
                    "Cluster",
                    "Average Inbound Traffic",
                    "Average Outbound Traffic",
                    "Average Messages",
                    "Highest Inbound Traffic Observed",
                    "Highest Outbound Traffic Observed",
                    "Highest Messages Observed",
                ],
                rows=kafka_rows,
            )
        )
        lines.append("")
        lines.append(f"Managed Kafka Utilization Details ({window_days}-Day):")
        lines.append("")
        lines.extend(
            _markdown_table(
                headers=[
                    "Project",
                    "Cluster",
                    "Assessment",
                    "Highest Broker CPU Observed (%)",
                    "Highest Broker Memory Observed (%)",
                    "Highest Broker Disk Observed (%)",
                    "Under-Replicated Partitions",
                    "Offline Partitions",
                    "Consumer Lag",
                ],
                rows=kafka_utilization_rows,
            )
        )
        if kafka_errors:
            lines.append("")
            lines.append("- Kafka data notes:")
            for error in kafka_errors[:10]:
                lines.append(f"  - {error}")
    lines.append("")
    return lines


def _backup_summary(item: CheckResult | None) -> list[str]:
    if item is None:
        return _missing_collector("backup")
    gke_rows = _as_dict_list(item.details.get("gke_backup", []))
    sql_rows = _as_dict_list(item.details.get("cloud_sql_backup", []))
    elastic_rows = _as_dict_list(item.details.get("elasticsearch_backup", []))
    policy = _as_dict(item.details.get("policy", {}))
    lines = [
        _collector_section_heading("Backup Discovery", item.status),
        f"- Summary: {item.summary}",
    ]
    lines.extend(_status_driver_lines(item))
    if str(policy.get("status", "")) == "out_of_scope":
        reason = str(policy.get("reason", "")).strip()
        if reason:
            lines.append(f"- Scope: {reason}")
        lines.append("- Detail: Backup resource inventory is not scored for this environment.")
        lines.append("")
        return lines
    lines.extend(
        [
            "",
            "GKE Backup Plans:",
            "",
        ]
    )
    lines.extend(
        _markdown_table(
            headers=["Project", "Region", "Assessment", "Reason", "Backup Plans Found"],
            rows=[
                [
                    str(row.get("project", "")),
                    str(row.get("region", "")),
                    _format_status(str(row.get("status", "na"))),
                    str(row.get("reason", row.get("error", ""))),
                    str(row.get("plan_count", 0)),
                ]
                for row in gke_rows
            ],
        )
    )
    lines.append("")
    lines.append("Cloud SQL Backup Configuration:")
    lines.append("")
    lines.extend(
        _markdown_table(
            headers=[
                "Project",
                "Assessment",
                "Reason",
                "Cloud SQL Instances",
                "With Backups Enabled",
            ],
            rows=[
                [
                    str(row.get("project", "")),
                    _format_status(str(row.get("status", "na"))),
                    str(row.get("reason", row.get("error", ""))),
                    str(row.get("instance_count", 0)),
                    str(
                        sum(
                            1
                            for instance in _as_dict_list(row.get("instances", []))
                            if bool(instance.get("backup_enabled", False))
                        )
                    ),
                ]
                for row in sql_rows
            ],
        )
    )
    lines.append("")
    lines.append("Elasticsearch Backup Checks:")
    lines.append("")
    lines.extend(
        _markdown_table(
            headers=["Target", "Assessment", "Snapshot Check", "Lifecycle Check", "Note"],
            rows=[
                [
                    str(row.get("name", "") or "not configured"),
                    _format_status(str(row.get("status", "na"))),
                    str(row.get("snapshot_http_status", "n/a")),
                    str(row.get("ilm_http_status", "n/a")),
                    str(row.get("reason", row.get("error", ""))),
                ]
                for row in elastic_rows
            ],
        )
    )
    lines.append("")
    return lines


def _kubernetes_utilization(item: CheckResult | None) -> list[str]:
    if item is None:
        return _missing_collector("kubernetes_health")
    lines = [
        _collector_section_heading("Current Kubernetes Cluster Usage", item.status),
    ]
    lines.extend(_status_driver_lines(item))
    lines.extend(
        [
            "",
            "- Source: current Kubernetes resource usage observed at collection time; "
            "these are not 7-day trend values.",
            "",
        ]
    )
    rows: list[list[str]] = []
    hpa_rows: list[list[str]] = []
    for cluster in _as_dict_list(item.details.get("clusters", [])):
        cluster_name = str(cluster.get("cluster", ""))
        hpa = _as_dict(cluster.get("hpa", {}))
        util = _as_dict(cluster.get("utilization", {}))
        rows.append(
            [
                cluster_name,
                str(hpa.get("total", 0)),
                str(hpa.get("hpas_scaling_limited", 0)),
                str(hpa.get("hpa_failure_count", 0)),
                str(util.get("nodes_with_metrics", 0)),
                str(util.get("pods_with_metrics", 0)),
                _format_millicores(_as_int(util.get("total_node_cpu_millicores", 0))),
                _format_gib(_as_int(util.get("total_node_memory_bytes", 0))),
            ]
        )
        for hpa_detail in _as_dict_list(hpa.get("details", [])):
            hpa_rows.append(
                [
                    cluster_name,
                    str(hpa_detail.get("namespace", "")),
                    str(hpa_detail.get("name", "")),
                    _autoscaling_layer_label(str(hpa_detail.get("autoscaling_layer", ""))),
                    _format_policy_status(str(hpa_detail.get("policy_status", "assessed"))),
                    str(hpa_detail.get("current_replicas", 0)),
                    str(hpa_detail.get("desired_replicas", 0)),
                    str(hpa_detail.get("min_replicas", 0)),
                    str(hpa_detail.get("max_replicas", 0)),
                    "yes" if bool(hpa_detail.get("at_max_replicas", False)) else "no",
                    "yes" if bool(hpa_detail.get("scaling_limited", False)) else "no",
                    _condition_summary(_as_dict_list(hpa_detail.get("failure_conditions", [])))
                    or "none",
                ]
            )
    lines.extend(
        _markdown_table(
            headers=[
                "Cluster",
                "Autoscalers Found",
                "Autoscalers At Scaling Limit",
                "Autoscalers With Issues",
                "Nodes Included in Snapshot",
                "Pods Included in Snapshot",
                "CPU Used Now (cores)",
                "Memory Used Now (GiB)",
            ],
            rows=rows,
        )
    )
    lines.append("")
    lines.append("Autoscaling Scope And Conditions:")
    lines.append("")
    lines.extend(
        _markdown_table(
            headers=[
                "Cluster",
                "Namespace",
                "Autoscaler",
                "Autoscaling Level",
                "Policy Scope",
                "Current Replicas",
                "Desired Replicas",
                "Min Replicas",
                "Max Replicas",
                "At Max Replicas",
                "At Scaling Limit",
                "Scaling Issues",
            ],
            rows=hpa_rows,
        )
    )
    lines.append("")
    return lines


def _kubernetes_namespace_utilization(item: CheckResult | None) -> list[str]:
    if item is None:
        return _missing_collector("kubernetes_health")
    lines = [
        "### Current Kubernetes Namespace Usage",
        "",
        "- Source: current Kubernetes pod usage. CPU and memory are summed across "
        "pods in each namespace at collection time; this is not per-pod and not a 7-day trend.",
        "",
    ]
    records: list[dict[str, Any]] = []
    for cluster in _as_dict_list(item.details.get("clusters", [])):
        cluster_name = str(cluster.get("cluster", ""))
        for ns in _as_dict_list(cluster.get("namespace_utilization", []))[:15]:
            records.append(
                {
                    "cluster": cluster_name,
                    "namespace": str(ns.get("namespace", "")),
                    "pod_count": ns.get("pod_count", 0),
                    "cpu_millicores": _as_int(ns.get("cpu_millicores", 0)),
                    "memory_bytes": _as_int(ns.get("memory_bytes", 0)),
                }
            )
    top_cpu_indexes = _top_record_indexes(records, "cpu_millicores", _NAMESPACE_TOP_VALUE_COUNT)
    top_memory_indexes = _top_record_indexes(records, "memory_bytes", _NAMESPACE_TOP_VALUE_COUNT)
    rows = [
        [
            str(record["cluster"]),
            str(record["namespace"]),
            str(record["pod_count"]),
            _bold_if(
                _format_millicores(_as_int(record["cpu_millicores"])),
                index in top_cpu_indexes,
            ),
            _bold_if(
                _format_gib(_as_int(record["memory_bytes"])),
                index in top_memory_indexes,
            ),
        ]
        for index, record in enumerate(records)
    ]
    if records:
        lines.append(
            f"- Bold CPU/memory values mark the top {_NAMESPACE_TOP_VALUE_COUNT} "
            "observed namespace values in this report."
        )
        lines.append("")
    lines.extend(
        _markdown_table(
            headers=[
                "Cluster",
                "Namespace",
                "Pod Count",
                "Current CPU (cores)",
                "Current Memory (GiB)",
            ],
            rows=rows,
        )
    )
    lines.append("")
    return lines


def _services_summary(item: CheckResult | None) -> list[str]:
    if item is None:
        return _missing_collector("services")
    lines = [
        _collector_section_heading("Managed Service Inventory", item.status),
        f"- Summary: {item.summary}",
    ]
    lines.extend(_status_driver_lines(item))
    sql_rows_raw = _as_dict_list(item.details.get("cloud_sql", []))
    cloud_sql_enabled = not _service_rows_disabled_in_config(sql_rows_raw)
    if cloud_sql_enabled:
        lines.extend(
            [
                "",
                "Cloud SQL Inventory:",
                "",
            ]
        )
        sql_rows = [
            [
                str(row.get("project", "")),
                _format_status(str(row.get("status", "na"))),
                str(row.get("instance_count", 0)),
            ]
            for row in sql_rows_raw
        ]
        lines.extend(
            _markdown_table(
                headers=["Project", "Assessment", "Instances Found"],
                rows=sql_rows,
            )
        )
        lines.append("")

    redis_rows = _as_dict_list(item.details.get("redis", []))
    kafka_rows = _as_dict_list(item.details.get("managed_kafka", []))
    redis_enabled = not _service_rows_disabled_in_config(redis_rows)
    kafka_enabled = not _service_rows_disabled_in_config(kafka_rows)
    redis_status = redis_rows[0].get("status", "unknown") if redis_rows else "unknown"
    kafka_status = kafka_rows[0].get("status", "unknown") if kafka_rows else "unknown"
    if redis_enabled:
        lines.append(f"- Redis Assessment: {_format_status(str(redis_status))}")
    if kafka_enabled:
        lines.append(f"- Managed Kafka Assessment: {_format_status(str(kafka_status))}")
    if redis_enabled:
        lines.append("")
        lines.append("Redis Inventory:")
        lines.append("")
        redis_detail_rows: list[list[str]] = []
        for row in redis_rows:
            project_name = str(row.get("project", ""))
            instances = _as_dict_list(row.get("instances", []))
            if not instances:
                redis_detail_rows.append(
                    [
                        project_name,
                        _format_status(str(row.get("status", "na"))),
                        "n/a",
                        "n/a",
                        "n/a",
                        str(row.get("reason", row.get("error", ""))),
                    ]
                )
            for instance in instances:
                redis_detail_rows.append(
                    [
                        project_name,
                        _format_status(str(row.get("status", "na"))),
                        str(instance.get("name", "")),
                        str(instance.get("state", "")),
                        str(instance.get("redis_version", "")),
                        str(instance.get("memory_size_gb", "")),
                    ]
                )
        lines.extend(
            _markdown_table(
                headers=["Project", "Assessment", "Instance", "State", "Version", "Memory (GB)"],
                rows=redis_detail_rows,
            )
        )
    if kafka_enabled:
        lines.append("")
        lines.append("Managed Kafka Inventory:")
        lines.append("")
        kafka_detail_rows: list[list[str]] = []
        for row in kafka_rows:
            project_name = str(row.get("project", ""))
            clusters = _as_dict_list(row.get("clusters", []))
            if not clusters:
                kafka_detail_rows.append(
                    [
                        project_name,
                        _format_status(str(row.get("status", "na"))),
                        "n/a",
                        "n/a",
                        str(row.get("reason", row.get("error", ""))),
                    ]
                )
            for cluster in clusters:
                kafka_detail_rows.append(
                    [
                        project_name,
                        _format_status(str(row.get("status", "na"))),
                        str(cluster.get("name", "")),
                        str(cluster.get("state", "")),
                        _format_capacity_details(cluster.get("capacity", {})),
                    ]
                )
        lines.extend(
            _markdown_table(
                headers=["Project", "Assessment", "Cluster", "State", "Capacity Details"],
                rows=kafka_detail_rows,
            )
        )
    lines.append("")

    lines.append("Compute Engine Instance Inventory:")
    lines.append("")
    lines.append(
        "- Includes standalone VMs and GKE node VMs. Standalone VM CPU is summarized in "
        "Infrastructure Utilization Trends."
    )
    lines.append("")
    lines.extend(
        _markdown_table(
            headers=["Name", "Project", "Zone", "VM State", "Machine Type", "Note"],
            rows=[
                [
                    str(row.get("name", "")),
                    str(row.get("project", "")),
                    str(row.get("zone", "")),
                    str(row.get("instance_status", row.get("status", "unknown"))),
                    str(row.get("machine_type", "")),
                    str(row.get("reason", row.get("error", ""))),
                ]
                for row in _as_dict_list(item.details.get("compute_instances", []))
            ],
        )
    )
    lines.append("")

    lines.append("Load Balancer Backend Health:")
    lines.append("")
    load_balancer_rows = _as_dict_list(item.details.get("load_balancers", []))
    if not load_balancer_rows:
        lines.append("- No load balancer backend evidence was collected.")
        lines.append("")
        return lines
    lines.append(f"- Summary: {_load_balancer_backend_summary(load_balancer_rows)}")
    detail_rows = [
        row for row in load_balancer_rows if _load_balancer_backend_row_needs_detail(row)
    ]
    if detail_rows:
        lines.append("")
        lines.append("Backend Services Requiring Review:")
        lines.append("")
        lines.extend(
            _markdown_table(
                headers=[
                    "Backend Service",
                    "Project",
                    "Scope",
                    "Backends",
                    "Health Checks",
                    "Backend Health",
                    "Protocol",
                    "Traffic Type",
                    "Assessment",
                    "Note",
                ],
                rows=[
                    [
                        str(row.get("backend_service", "")),
                        str(row.get("project", "")),
                        str(row.get("scope", "")),
                        str(row.get("backends", "n/a")),
                        str(row.get("health_checks", "n/a")),
                        _format_backend_health(row),
                        str(row.get("protocol", "")),
                        str(row.get("scheme", "")),
                        _format_status(str(row.get("status", "na"))),
                        str(row.get("reason", row.get("error", ""))),
                    ]
                    for row in detail_rows
                ],
            )
        )
    else:
        lines.append(
            "- Detail: All reported backend services have no unhealthy or unknown backend health."
        )
    lines.append("")
    return lines


def _logging_summary(item: CheckResult | None) -> list[str]:
    if item is None:
        return _missing_collector("logging")
    lines = [
        _collector_section_heading("Cloud Logging Pipeline", item.status),
        f"- Summary: {item.summary}",
    ]
    lines.extend(_status_driver_lines(item))
    lines.append("")
    rows: list[list[str]] = []
    bucket_rows: list[list[str]] = []
    ingestion_rows: list[list[str]] = []
    bucket_ingestion_rows: list[list[str]] = []
    bucket_storage_rows: list[list[str]] = []
    pubsub_rows: list[list[str]] = []
    pubsub_scope_notes: list[str] = []
    metrics_notes: list[str] = []
    storage_metric_missing_projects: list[str] = []
    storage_metric_not_applicable_projects: list[str] = []
    for row in _as_dict_list(item.details.get("projects", [])):
        splunk = _as_dict(row.get("splunk_hints", {}))
        rows.append(
            [
                str(row.get("project", "")),
                _format_status(str(row.get("status", "na"))),
                str(row.get("sink_count", 0)),
                str(row.get("bucket_count", 0)),
                str(row.get("pubsub_topic_count", 0)),
                str(row.get("pubsub_subscription_total", 0)),
                "yes" if bool(splunk.get("matched", False)) else "no",
            ]
        )
        project_name = str(row.get("project", ""))
        for bucket in _as_dict_list(row.get("buckets", [])):
            bucket_rows.append(
                [
                    project_name,
                    _last_path_part(str(bucket.get("name", ""))),
                    str(bucket.get("location", "")) or "n/a",
                    str(bucket.get("retention_days", "n/a")),
                    "yes" if bool(bucket.get("locked", False)) else "no",
                ]
            )
        metrics = _as_dict(row.get("logging_metrics", {}))
        if metrics:
            stored_peak = metrics.get("bytes_stored_peak")
            stored_status = str(metrics.get("bytes_stored_metric_status", "")).strip()
            if stored_status == "not_applicable" or (
                stored_status == "metric_not_returned"
                and _has_log_bucket_metadata(row)
                and not _has_chargeable_extended_log_retention(row)
            ):
                stored_text = "not applicable"
                storage_metric_not_applicable_projects.append(project_name)
            elif stored_peak is None or stored_status == "metric_not_returned":
                stored_text = "metric not returned"
                storage_metric_missing_projects.append(project_name)
            else:
                stored_text = _format_bytes(stored_peak)
            ingestion_rows.append(
                [
                    project_name,
                    _format_bytes(metrics.get("bytes_ingested_total", 0.0)),
                    _format_bytes(metrics.get("bytes_ingested_peak_hour", 0.0)),
                    stored_text,
                ]
            )
            for bucket_metric in _as_dict_list(metrics.get("bucket_ingestion", [])):
                bucket_ingestion_rows.append(
                    [
                        project_name,
                        str(bucket_metric.get("bucket", "")),
                        str(bucket_metric.get("location", "")) or "n/a",
                        _format_bytes(bucket_metric.get("bytes_ingested_total", 0.0)),
                        _format_bytes(bucket_metric.get("bytes_ingested_peak_hour", 0.0)),
                    ]
                )
            for bucket_metric in _as_dict_list(metrics.get("bucket_storage", [])):
                bucket_storage_rows.append(
                    [
                        project_name,
                        str(bucket_metric.get("bucket", "")),
                        str(bucket_metric.get("location", "")) or "n/a",
                        _format_bytes(bucket_metric.get("bytes_stored_peak", 0.0)),
                        str(bucket_metric.get("data_type", "")) or "n/a",
                    ]
                )
        for subscription in _as_dict_list(row.get("pubsub_metrics", [])):
            pubsub_rows.append(
                [
                    project_name,
                    str(subscription.get("subscription_id", "")),
                    _format_float(subscription.get("num_unacked_messages_peak", 0.0), 0),
                    _format_duration_seconds(
                        subscription.get("oldest_unacked_message_age_peak_seconds", 0.0)
                    ),
                    _format_optional_float(
                        subscription.get("delivery_latency_health_score_min", 0.0),
                        2,
                    ),
                    _format_float(subscription.get("dead_letter_message_count_total", 0.0), 0),
                ]
            )
        checked_subscriptions, sample_limit, sampled = _pubsub_metric_scope(row)
        total_subscriptions = _as_int(row.get("pubsub_subscription_total", 0))
        if sampled or (total_subscriptions > 0 and checked_subscriptions < total_subscriptions):
            if sample_limit > 0:
                pubsub_scope_notes.append(
                    f"{project_name}: metrics checked {checked_subscriptions} of "
                    f"{total_subscriptions} subscription(s), limited to the first "
                    f"{sample_limit} subscription(s) returned per topic."
                )
            else:
                pubsub_scope_notes.append(
                    f"{project_name}: metrics checked {checked_subscriptions} of "
                    f"{total_subscriptions} subscription(s)."
                )
        metrics_error = str(row.get("metrics_error", "")).strip()
        if metrics_error:
            metrics_notes.append(f"{project_name}: {metrics_error}")
    lines.extend(
        _markdown_table(
            headers=[
                "Project",
                "Assessment",
                "Logging Sinks",
                "Log Buckets",
                "Pub/Sub Topics",
                "Pub/Sub Subscriptions",
                "Splunk Destination Detected",
            ],
            rows=rows,
        )
    )
    lines.append("")
    lines.append("Cloud Logging Buckets:")
    lines.append("")
    lines.extend(
        _markdown_table(
            headers=["Project", "Bucket", "Location", "Retention Days", "Retention Locked"],
            rows=bucket_rows,
        )
    )
    lines.append("")
    lines.append("Project-Level Log Ingestion (7-Day):")
    lines.append("")
    lines.append(
        "- Source: Cloud Monitoring log-volume data. Log ingestion is project-level because "
        "this table uses the project-level ingestion metric."
    )
    lines.append("")
    lines.extend(
        _markdown_table(
            headers=[
                "Project",
                "Total Logs Ingested (7-Day)",
                "Peak Hour Log Ingestion",
                "Stored Volume Metric",
            ],
            rows=ingestion_rows,
        )
    )
    lines.append("")
    lines.append("Log Bucket Ingestion (7-Day):")
    lines.append("")
    lines.append(
        "- Source: Cloud Monitoring `log_bucket_bytes_ingested`, grouped by log bucket. "
        "This is bucket-level ingestion volume and peak hourly insertion, not retained size."
    )
    lines.append("")
    lines.extend(
        _markdown_table(
            headers=[
                "Project",
                "Bucket",
                "Location",
                "Total Logs Ingested (7-Day)",
                "Peak Hour Log Insertion",
            ],
            rows=bucket_ingestion_rows,
        )
    )
    lines.append("")
    lines.append("Long-Retention Log Storage Metric:")
    lines.append("")
    lines.append(
        "- Source: Cloud Monitoring `bytes_stored`, grouped by `log_bucket_id` and "
        "`log_bucket_location`. Google defines this as logs retained past the default "
        "30 days; it is not total current bucket occupancy."
    )
    lines.append(
        "- Cloud Logging bucket metadata exposes retention settings, not current stored bytes. "
        "`_Required` is fixed at 400 days and is excluded from retention charges."
    )
    lines.append("")
    if bucket_storage_rows:
        lines.extend(
            _markdown_table(
                headers=[
                    "Project",
                    "Bucket",
                    "Location",
                    "Peak Stored Logs",
                    "Metric Scope",
                ],
                rows=bucket_storage_rows,
            )
        )
    else:
        if storage_metric_not_applicable_projects:
            projects = ", ".join(storage_metric_not_applicable_projects)
            lines.append(
                f"- Not applicable for {projects}: no non-`_Required` log bucket has "
                "retention above 30 days, so Cloud Monitoring is expected to return no "
                "`bytes_stored` series."
            )
        if storage_metric_missing_projects:
            projects = ", ".join(storage_metric_missing_projects)
            lines.append(
                f"- Metric was not returned for {projects}. If any non-`_Required` bucket "
                "retains logs longer than 30 days, confirm Cloud Monitoring metric "
                "availability or use an agreed external source such as Billing Export or "
                "Log Analytics."
            )
        if not storage_metric_not_applicable_projects and not storage_metric_missing_projects:
            lines.append("- No bucket-level stored-volume rows were returned.")
    if pubsub_rows or pubsub_scope_notes:
        lines.append("")
        lines.append(
            "Logging Sink Pub/Sub Delivery Health Sample (7-Day):"
            if pubsub_scope_notes
            else "Logging Sink Pub/Sub Delivery Health (7-Day):"
        )
        lines.append("")
        if pubsub_scope_notes:
            lines.append(
                "- Source: Cloud Monitoring Pub/Sub metrics for a bounded sample of discovered "
                "logging sink topic subscriptions. Subscription counts above still show the total "
                "subscriptions discovered."
            )
            for note in pubsub_scope_notes[:10]:
                lines.append(f"  - {note}")
            lines.append("")
        lines.extend(
            _markdown_table(
                headers=[
                    "Project",
                    "Subscription",
                    "Peak Unacked Messages",
                    "Oldest Message Age",
                    "Lowest Delivery Health Score",
                    "Dead Letter Messages (7-Day)",
                ],
                rows=pubsub_rows,
            )
        )
    if metrics_notes:
        lines.append("")
        lines.append("- Logging/Pub/Sub data notes:")
        for note in metrics_notes[:10]:
            lines.append(f"  - {note}")
    lines.append("")
    return lines


def _pubsub_metric_scope(row: dict[str, Any]) -> tuple[int, int, bool]:
    raw_checked = row.get("pubsub_metric_subscriptions_checked")
    raw_sampled = row.get("pubsub_metric_subscriptions_sampled")
    checked = (
        _as_int(raw_checked)
        if raw_checked is not None
        else _legacy_pubsub_metric_subscription_count(row)
    )
    sample_limit = _as_int(row.get("pubsub_metric_subscription_sample_limit", 0))
    if raw_sampled is not None:
        sampled = bool(raw_sampled)
    else:
        sampled = _legacy_pubsub_metrics_were_sampled(row, checked)
    return checked, sample_limit, sampled


def _has_chargeable_extended_log_retention(project_row: dict[str, Any]) -> bool:
    for bucket in _as_dict_list(project_row.get("buckets", [])):
        if _last_path_part(str(bucket.get("name", ""))) == "_Required":
            continue
        retention_days = _as_optional_int(bucket.get("retention_days"))
        if retention_days is not None and retention_days > 30:
            return True
    return False


def _has_log_bucket_metadata(project_row: dict[str, Any]) -> bool:
    return bool(_as_dict_list(project_row.get("buckets", [])))


def _as_optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _legacy_pubsub_metric_subscription_count(row: dict[str, Any]) -> int:
    subscriptions: list[str] = []
    for topic in _as_dict_list(row.get("pubsub_topics", [])):
        for subscription in _str_list(topic.get("sample_subscriptions", [])):
            if subscription not in subscriptions:
                subscriptions.append(subscription)
    if subscriptions:
        return len(subscriptions)
    return len(_as_dict_list(row.get("pubsub_metrics", [])))


def _legacy_pubsub_metrics_were_sampled(row: dict[str, Any], checked: int) -> bool:
    for topic in _as_dict_list(row.get("pubsub_topics", [])):
        sample_count = len(_str_list(topic.get("sample_subscriptions", [])))
        if sample_count and _as_int(topic.get("subscription_count", 0)) > sample_count:
            return True
    total = _as_int(row.get("pubsub_subscription_total", 0))
    return checked > 0 and total > checked


def _missing_collector(name: str) -> list[str]:
    title = _collector_display_name(name)
    return [
        f"### {title}",
        "- Assessment Result: `Not Assessed`",
        "- Summary: This section was not assessed for the configured scope.",
        "",
    ]


def _collector_gap_summary(collectors: list[CheckResult]) -> list[str]:
    lines: list[str] = []
    rows: list[list[str]] = []
    for item in collectors:
        if item.status not in (
            Status.SKIPPED_CONFIG,
            Status.SKIPPED_PERMISSION,
            Status.SKIPPED_NETWORK,
            Status.FAILED,
            Status.CRITICAL,
            Status.WARNING,
        ):
            continue
        signal = "Evidence was incomplete or unavailable."
        if item.status == Status.WARNING:
            signal = "Warning-level findings were observed."
        if item.status == Status.SKIPPED_CONFIG:
            signal = "Not assessed because scope or configuration is missing."
        if item.status == Status.SKIPPED_PERMISSION:
            signal = "Read-only permission is missing for this evidence."
        if item.status == Status.SKIPPED_NETWORK:
            signal = "The endpoint was unreachable during evidence collection."
        if item.status in (Status.FAILED, Status.CRITICAL):
            signal = "A critical finding was observed, or required evidence could not be collected."
        rows.append(
            [
                _collector_display_name(item.collector),
                _format_status(item.status.value),
                signal,
            ]
        )

    if not rows:
        return ["- No assessment gaps or action-required findings in this cycle."]

    lines.extend(
        _markdown_table(
            headers=["Area", "Assessment Result", "Reason"],
            rows=rows,
        )
    )
    return lines


def _collector_display_name(name: str) -> str:
    title_map = {
        "preflight": "Access And API Readiness",
        "gke_inventory": "GKE Cluster Inventory",
        "kubernetes_health": "Kubernetes Runtime Health",
        "monitoring": "GCP Monitoring Alerts",
        "prometheus_monitoring": "Monitoring Stack",
        "audit": "Audit Activity",
        "backup": "Backup",
        "network": "Network Inventory And DNS Controls",
        "mesh": "Service Mesh Health",
        "trend_metrics": "Infrastructure Utilization Trends",
        "services": "Services",
        "logging": "Logging",
    }
    return title_map.get(name, name.replace("_", " ").title())


def _format_prometheus_scope(labels: dict[str, Any]) -> str:
    parts: list[str] = []
    for label, raw_values in sorted(labels.items()):
        values = raw_values if isinstance(raw_values, list) else [raw_values]
        rendered = ", ".join(f"`{value}`" for value in values)
        parts.append(f"`{label}` in {rendered}")
    return "; ".join(parts)


def _format_down_jobs(jobs: list[dict[str, Any]]) -> str:
    if not jobs:
        return "none"
    rendered = [f"{row.get('job', 'unknown')}:{row.get('down_targets', 0)}" for row in jobs[:3]]
    if len(jobs) > 3:
        rendered.append(f"+{len(jobs) - 3} more")
    return ", ".join(rendered)


def _limited_table_heading(title: str, row_count: int, limit: int) -> str:
    if row_count > limit:
        return f"{title} (First {limit} Shown):"
    return f"{title}:"


def _per_project_limited_table_heading(
    title: str,
    *,
    total_count: int,
    shown_count: int,
    per_project_limit: int,
) -> str:
    if total_count > shown_count:
        return f"{title} (First {per_project_limit} Per Project Shown):"
    return f"{title}:"


def _cloud_monitoring_limited_heading(
    title: str,
    *,
    window_days: int,
    row_count: int,
    limit: int,
    limit_label: str,
) -> str:
    scope = f"{window_days}-Day Cloud Monitoring"
    if row_count > limit:
        scope = f"{scope}, {limit_label} {limit} Shown"
    return f"{title} ({scope}):"


def _markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    lines = [
        f"| {' | '.join(headers)} |",
        f"| {' | '.join(['---'] * len(headers))} |",
    ]
    if not rows:
        lines.append(f"| {' | '.join(['n/a'] * len(headers))} |")
        return lines
    for row in rows:
        padded = row + ([""] * (len(headers) - len(row)))
        lines.append(f"| {' | '.join(_sanitize_cell(value) for value in padded[: len(headers)])} |")
    return lines


def _sanitize_cell(value: str) -> str:
    return _escape_markdown_html(value).replace("|", "&#124;").replace("\n", "<br>")


def _format_status(status: str) -> str:
    normalized = status.strip().lower()
    if normalized == Status.OK.value:
        return "No Findings"
    if normalized == Status.WARNING.value:
        return "Needs Review"
    if normalized in (Status.FAILED.value, Status.CRITICAL.value):
        return "Action Required"
    if normalized in (
        Status.SKIPPED_CONFIG.value,
        Status.SKIPPED_PERMISSION.value,
        Status.SKIPPED_NETWORK.value,
        "na",
        "n/a",
    ):
        return "Not Assessed"
    return status.replace("_", " ").strip().title()


def _collector_section_heading(title: str, status: Status) -> str:
    if status == Status.OK:
        return f"### {title}"
    return f"### {title} ({_format_status(status.value)})"


def _format_cpu_spare_capacity(
    row: dict[str, Any],
    *,
    observed: bool,
    key: str = "idle_capacity_at_peak_percent",
) -> str:
    if not observed:
        return "n/a"
    if key in row:
        return _format_percent(row.get(key, 0.0))
    if key == "idle_capacity_at_peak_percent" and "cpu_peak_percent" in row:
        spare_capacity = _spare_capacity_from_active_percent(_as_float(row["cpu_peak_percent"]))
        return _format_percent(spare_capacity)
    if key == "max_node_cpu_idle_capacity_at_peak_percent" and (
        "max_node_cpu_allocatable_peak_percent" in row
    ):
        spare_capacity = _spare_capacity_from_active_percent(
            _as_float(row["max_node_cpu_allocatable_peak_percent"])
        )
        return _format_percent(spare_capacity)
    return "n/a"


def _cpu_usage_summary_rows(rows: list[list[str]]) -> list[list[str]]:
    counts: dict[tuple[str, str], dict[str, float]] = {}
    for row in rows:
        project = str(row[0]) if len(row) > 0 else ""
        resource_type = str(row[1]) if len(row) > 1 else ""
        active_peak = _percent_cell_value(str(row[4]) if len(row) > 4 else "")
        telemetry_available = str(row[6]).strip().lower() == "yes" if len(row) > 6 else False
        bucket = counts.setdefault(
            (project, resource_type),
            {
                "total": 0.0,
                "available": 0.0,
                "Missing": 0.0,
                "active_peak": 0.0,
            },
        )
        bucket["total"] += 1
        if telemetry_available:
            bucket["available"] += 1
            bucket["active_peak"] = max(bucket["active_peak"], active_peak)
        else:
            bucket["Missing"] += 1

    return [
        [
            project,
            resource_type,
            _format_float(bucket["total"], 0),
            _format_float(bucket["available"], 0),
            _format_float(bucket["Missing"], 0),
            _format_utilization_percent(bucket["active_peak"]) if bucket["available"] else "n/a",
            (
                _format_percent(_spare_capacity_from_active_percent(bucket["active_peak"]))
                if bucket["available"]
                else "n/a"
            ),
        ]
        for (project, resource_type), bucket in sorted(counts.items())
    ]


def _percent_cell_value(value: str) -> float:
    normalized = value.replace("**", "").replace("%", "").strip()
    return _as_float(normalized)


def _spare_capacity_from_active_percent(active_percent: float) -> float:
    return max(0.0, min(100.0, 100.0 - active_percent))


def _status_reason_phrase(status: str) -> str:
    result = _format_status(status)
    phrases = {
        "No Findings": "has no findings",
        "Needs Review": "needs review",
        "Action Required": "requires action",
        "Not Assessed": "was not assessed",
    }
    return phrases.get(result, f"assessment result {result}")


def _format_percent(value: Any) -> str:
    if isinstance(value, bool):
        return "0.0%"
    if isinstance(value, (int, float)):
        return f"{value:.1f}%"
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return "0.0%"
        return f"{parsed:.1f}%"
    return "0.0%"


def _format_utilization_percent(value: Any) -> str:
    numeric = _as_float(value)
    formatted = _format_percent(numeric)
    if numeric >= _UTILIZATION_WARNING_THRESHOLD:
        return f"**{formatted}**"
    return formatted


def _bold_if(value: str, condition: bool) -> str:
    if condition:
        return f"**{value}**"
    return value


def _format_rate(value: Any, unit: str = "/s") -> str:
    numeric = _as_float(value)
    if numeric >= 1_000_000:
        return f"{numeric / 1_000_000:.2f}M{unit}"
    if numeric >= 1_000:
        return f"{numeric / 1_000:.2f}K{unit}"
    return f"{numeric:.2f}{unit}"


def _format_bytes_per_second(value: Any) -> str:
    numeric = _as_float(value)
    if numeric >= 1024**3:
        return f"{numeric / (1024**3):.2f} GiB/s"
    if numeric >= 1024**2:
        return f"{numeric / (1024**2):.2f} MiB/s"
    if numeric >= 1024:
        return f"{numeric / 1024:.2f} KiB/s"
    return f"{numeric:.2f} B/s"


def _format_bytes(value: Any) -> str:
    numeric = _as_float(value)
    if numeric >= 1024**4:
        return f"{numeric / (1024**4):.2f} TiB"
    if numeric >= 1024**3:
        return f"{numeric / (1024**3):.2f} GiB"
    if numeric >= 1024**2:
        return f"{numeric / (1024**2):.2f} MiB"
    if numeric >= 1024:
        return f"{numeric / 1024:.2f} KiB"
    return f"{numeric:.0f} B"


def _format_duration_seconds(value: Any) -> str:
    numeric = _as_float(value)
    if numeric >= 3600:
        return f"{numeric / 3600:.2f} h"
    if numeric >= 60:
        return f"{numeric / 60:.2f} min"
    return f"{numeric:.0f} s"


def _format_millicores(value: int) -> str:
    return f"{value / 1000:.2f}"


def _format_gib(value: int) -> str:
    return f"{value / (1024**3):.2f}"


def _format_gib_from_float(value: Any) -> str:
    return f"{_as_float(value) / (1024**3):.2f} GiB"


def _format_float(value: Any, digits: int) -> str:
    return f"{_as_float(value):.{digits}f}"


def _format_chart_value(value: Any, unit: str) -> str:
    numeric = _as_float(value)
    normalized = unit.strip().lower()
    if normalized == "percent":
        return f"{numeric:.1f}%"
    if normalized == "count" and abs(numeric - round(numeric)) < 0.05:
        return str(int(round(numeric)))
    return _format_float(numeric, 2)


def _format_optional_float(value: Any, digits: int) -> str:
    if value is None:
        return "n/a"
    return _format_float(value, digits)


def _format_observed_float(value: Any, digits: int, observed: bool) -> str:
    if not observed or value is None:
        return "n/a"
    return _format_float(value, digits)


def _format_observed_gib(value: Any, observed: bool) -> str:
    if not observed or value is None:
        return "n/a"
    return _format_gib_from_float(value)


def _format_capacity_details(value: Any) -> str:
    capacity = _as_dict(value)
    if not capacity:
        text = str(value).strip()
        return _truncate(text, 140) if text else "n/a"

    parts: list[str] = []
    vcpu_count = capacity.get("vcpuCount")
    if vcpu_count not in (None, ""):
        numeric = _as_float(vcpu_count)
        parts.append(f"{numeric:g} vCPU" if numeric else f"{vcpu_count} vCPU")

    memory_bytes = capacity.get("memoryBytes")
    if memory_bytes not in (None, ""):
        parts.append(f"{_format_gib_from_float(memory_bytes)} memory")

    if parts:
        return ", ".join(parts)
    return _truncate(str(capacity), 140)


def _load_balancer_backend_summary(rows: list[dict[str, Any]]) -> str:
    total = len(rows)
    rows_requiring_review = sum(1 for row in rows if _load_balancer_backend_row_needs_detail(row))
    unhealthy = 0
    unknown = 0
    for row in rows:
        _healthy_count, unhealthy_count, unknown_count = _backend_health_counts(row)
        unhealthy += unhealthy_count
        unknown += unknown_count
    if rows_requiring_review:
        return (
            f"{_count_text(total, 'backend service')} reviewed; "
            f"{_count_text(rows_requiring_review, 'backend service')} requiring review; "
            f"{_count_text(unhealthy, 'unhealthy backend')}; "
            f"{_count_text(unknown, 'unknown backend')}"
        )
    return (
        f"{_count_text(total, 'backend service')} reviewed; "
        f"{_count_text(unhealthy, 'unhealthy backend')}; "
        f"{_count_text(unknown, 'unknown backend')}"
    )


def _load_balancer_backend_row_needs_detail(row: dict[str, Any]) -> bool:
    status = str(row.get("status", ""))
    if status and status != Status.OK.value:
        return True
    _healthy, unhealthy, unknown = _backend_health_counts(row)
    return unhealthy > 0 or unknown > 0


def _backend_health_counts(row: dict[str, Any]) -> tuple[int, int, int]:
    counts = _as_dict(row.get("backend_health_counts", {}))
    if not counts:
        parsed = _parse_backend_health_summary(str(row.get("backend_health", "")))
        return (
            parsed.get("healthy", 0),
            parsed.get("unhealthy", 0),
            parsed.get("unknown", 0),
        )

    healthy = _as_int(counts.get("HEALTHY", 0))
    unknown = _as_int(counts.get("UNKNOWN", 0))
    unhealthy = sum(
        _as_int(count) for state, count in counts.items() if state not in ("HEALTHY", "UNKNOWN")
    )
    return healthy, unhealthy, unknown


def _format_backend_health(row: dict[str, Any]) -> str:
    counts = _as_dict(row.get("backend_health_counts", {}))
    if not counts:
        return _format_backend_health_summary(str(row.get("backend_health", "n/a")))

    healthy, unhealthy, unknown = _backend_health_counts(row)
    return f"{healthy} healthy; {unhealthy} unhealthy; {unknown} unknown"


def _format_backend_health_summary(summary: str) -> str:
    parsed = _parse_backend_health_summary(summary)
    if parsed:
        return (
            f"{parsed.get('healthy', 0)} healthy; "
            f"{parsed.get('unhealthy', 0)} unhealthy; "
            f"{parsed.get('unknown', 0)} unknown"
        )
    return summary


def _parse_backend_health_summary(summary: str) -> dict[str, int]:
    parsed: dict[str, int] = {}
    for part in summary.split(","):
        key, separator, value = part.strip().partition("=")
        if not separator:
            continue
        normalized_key = key.strip().lower()
        if normalized_key in ("healthy", "unhealthy", "unknown"):
            parsed[normalized_key] = _as_int(value.strip())
    return parsed


def _condition_summary(conditions: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for condition in conditions[:5]:
        text = (
            f"{condition.get('type', '')}={condition.get('status', '')}"
            f" {condition.get('reason', '')}"
        ).strip()
        message = str(condition.get("message", "")).strip()
        if message:
            text = f"{text}: {_truncate(message, 120)}"
        if text:
            parts.append(text)
    return "; ".join(parts)


def _event_summary(events: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for event in events[:5]:
        reason = str(event.get("reason", "")).strip()
        message = str(event.get("message", "")).strip()
        text = reason
        if message:
            text = f"{text}: {_truncate(message, 120)}" if text else _truncate(message, 120)
        if text:
            parts.append(text)
    return "; ".join(parts)


def _last_path_part(value: str) -> str:
    stripped = value.rstrip("/")
    return stripped.rsplit("/", 1)[-1] if stripped else ""


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if isinstance(item, str)]
    return []


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _as_float(value: Any) -> float:
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        numeric = float(int(value))
    elif isinstance(value, (int, float)):
        numeric = float(value)
    elif isinstance(value, str):
        try:
            numeric = float(value)
        except ValueError:
            return None
    else:
        return None
    return numeric if math.isfinite(numeric) else None


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "item"


def _top_record_indexes(records: list[dict[str, Any]], key: str, limit: int) -> set[int]:
    ranked = sorted(
        ((index, _as_float(record.get(key, 0.0))) for index, record in enumerate(records)),
        key=lambda item: item[1],
        reverse=True,
    )
    return {index for index, value in ranked[:limit] if value > 0}


def _truncate(value: str, max_len: int = 400) -> str:
    if len(value) <= max_len:
        return value
    return f"{value[:max_len]} ...[truncated]"


def _safe_markdown_inline(value: str, max_len: int = 400) -> str:
    cleaned = " ".join(str(value).split())
    return _escape_markdown_html(_truncate(cleaned, max_len)).replace("`", "'")


def _escape_markdown_html(value: str) -> str:
    return escape(value, quote=False).replace("&amp;", "&")
