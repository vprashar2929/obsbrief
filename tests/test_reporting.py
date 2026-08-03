import json
from pathlib import Path

from opsbrief.branding import (
    BrandColors,
    BrandCornerMotif,
    BrandFontFace,
    BrandProfile,
    BrandReportTheme,
    BrandStatusColors,
    BrandThemeLines,
)
from opsbrief.html_report import PRINT_SECTION_BREAK_IDS, render_report_html
from opsbrief.models import CheckResult, Report, Status
from opsbrief.reporting import (
    ensure_report_directory,
    read_report_json,
    write_collector_evidence,
    write_preflight_evidence,
    write_report_html,
    write_report_json,
    write_report_markdown,
    write_report_pdf,
)


def test_reporting_writes_all_artifacts(tmp_path: Path) -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="preflight",
                status=Status.OK,
                summary="ok",
            ),
            CheckResult(
                collector="kubernetes_health",
                status=Status.OK,
                summary="ok",
                details={"clusters": []},
            ),
            CheckResult(
                collector="monitoring",
                status=Status.OK,
                summary="ok",
                details={
                    "projects": [
                        {
                            "project": "example-dev-project",
                            "status": "ok",
                            "alert_policy_enabled": 1,
                            "alerts": {"open_alerts": 0, "opened_last_7d": 0},
                            "notification_channels": {"missing_channels": 0},
                            "sample_policies": [
                                {
                                    "name": "projects/p/alertPolicies/p1",
                                    "display_name": "CPU High",
                                    "enabled": True,
                                }
                            ],
                        }
                    ]
                },
            ),
            CheckResult(
                collector="trend_metrics",
                status=Status.OK,
                summary="ok",
                details={"projects": []},
            ),
        ],
    )

    target = ensure_report_directory(tmp_path, "dev", 2026, 20)
    md = write_report_markdown(report, target)
    html = write_report_html(report, target)
    pdf = write_report_pdf(report, target)
    js = write_report_json(report, target)
    ev = write_collector_evidence(report, target)

    assert md.exists()
    assert html.exists()
    assert pdf.exists()
    assert js.exists()
    assert ev.exists()

    markdown_text = md.read_text(encoding="utf-8")
    assert "Assessment result definitions:" in markdown_text
    assert "## Infrastructure Health Overview" in markdown_text
    assert "### Critical Findings" in markdown_text
    assert "### Warning Findings" in markdown_text
    assert "### Healthy Areas" in markdown_text
    assert "### Not Assessed Areas" in markdown_text
    assert "### Immediate Actions Required" in markdown_text
    assert "## Executive Summary" in markdown_text
    assert markdown_text.index("## Executive Summary") < markdown_text.index(
        "## Infrastructure Health Overview"
    )
    assert "## Platform Inventory" in markdown_text
    assert "## Autoscaling Scope" in markdown_text
    assert "## System Summary" not in markdown_text
    assert "ISO Week" not in markdown_text
    assert "Overall Status Driver" not in markdown_text
    assert "## Operational Posture" not in markdown_text
    assert "## Operational Action Register" not in markdown_text
    assert "## Snapshot" not in markdown_text
    assert "| Area | Assessment Result | Reason | Observed Details |" not in markdown_text
    assert "HTML navigation" not in markdown_text
    assert "heading in the PDF" not in markdown_text
    assert "Action Details:" not in markdown_text
    assert "Where to look: [" not in markdown_text
    assert "## Evidence Gaps And Limits" not in markdown_text
    assert "Application Dependency Connectivity" not in markdown_text
    assert "## Technical Evidence Appendix" in markdown_text
    assert "Stakeholder-facing summary of the current operational evidence." not in markdown_text
    assert "Evidence Basis: read-only collector output captured" not in markdown_text
    assert "Interpretation Note: Not Assessed means" not in markdown_text
    assert "Full structured evidence is also written to" not in markdown_text
    assert markdown_text.index("## Infrastructure Health Overview") < markdown_text.index(
        "## Technical Evidence Appendix"
    )
    html_text = html.read_text(encoding="utf-8")
    assert "Stakeholder-facing summary of the current operational evidence." not in html_text
    assert "Full structured evidence is also written to" not in html_text
    assert "## Evidence Index" not in markdown_text
    assert "## Assessment Gaps And Action Items" not in markdown_text
    assert "## Network And DNS Posture" not in markdown_text
    assert "No material risk signal detected" not in markdown_text
    assert "Required Follow-Up" not in markdown_text
    assert "### Incident Resolutions" not in markdown_text
    assert "### Pod Restart Diagnostics" in markdown_text
    assert "Probable Cause" not in markdown_text
    assert "Recommended Next Action" not in markdown_text
    assert "## Issue Diagnostics" not in markdown_text
    assert "### Current Kubernetes Namespace Usage" in markdown_text
    assert "GCP Monitoring Alert Policies:" not in markdown_text
    assert "GKE monitoring and alerting are provided by Prometheus/Grafana." not in markdown_text
    assert "GCP Monitoring Alert Policies (Sample):" not in markdown_text
    assert "### Infrastructure Utilization Trends" in markdown_text
    assert "### Infrastructure Utilization Trends (No Findings)" not in markdown_text
    assert "Redis Throughput Trend (7-Day):" in markdown_text
    assert "Managed Kafka Throughput Trend (7-Day):" in markdown_text
    assert "_pending owner input_" not in markdown_text
    assert "Automated collectors executed:" not in markdown_text
    assert "Incident resolution narrative:" not in markdown_text
    assert "Risk narrative and planned changes:" not in markdown_text
    assert "## Priority Actions (This Week)" not in markdown_text
    assert "## Scope Coverage (v1)" not in markdown_text
    assert "### PII Logging Notes" not in markdown_text
    assert "## Open Risks / Recurring Issues" not in markdown_text
    assert "### Recurring Issues" not in markdown_text
    assert "## ServiceNow Ticket Summary" not in markdown_text
    assert "## Planned Changes" not in markdown_text
    assert "## Operational Activities" not in markdown_text
    assert "## Notes" not in markdown_text

    html_text = html.read_text(encoding="utf-8")
    assert "<html" in html_text.lower()
    assert "OpsBrief Weekly Operational Report (dev)" in html_text
    assert 'class="report-toc"' in html_text
    assert 'class="report-hero"' in html_text
    assert 'class="status-tag status-ok"' in html_text
    assert 'class="status-label' not in html_text
    assert 'class="status-badge' not in html_text
    assert 'href="#infrastructure-health-overview"' in html_text
    assert "<p>GCP Monitoring Alert Policies:</p>" not in html_text
    assert "Prometheus/Grafana" not in html_text
    assert "<p>Cloud SQL Utilization Summary (7-Day):</p>" in html_text
    assert "| Project | Instance | CPU Peak |" not in html_text

    assert pdf.read_bytes().startswith(b"%PDF")

    round_tripped = read_report_json(js)
    assert round_tripped.environment == "dev"
    assert round_tripped.iso_year == 2026
    assert round_tripped.iso_week == 20
    assert [item.collector for item in round_tripped.collectors] == [
        "preflight",
        "kubernetes_health",
        "monitoring",
        "trend_metrics",
    ]
    assert round_tripped.collectors[2].status == Status.OK


def test_reporting_suppresses_disabled_redis_and_kafka_trend_sections(tmp_path: Path) -> None:
    report = Report(
        environment="shared-mon",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="trend_metrics",
                status=Status.OK,
                summary="Collected 7d trend metrics for 1 project(s)",
                details={
                    "window_days": 7,
                    "projects": [
                        {
                            "project": "example-shared-mon-project",
                            "status": "ok",
                            "cloud_sql": [],
                            "non_gke_vm_cpu": [],
                            "non_gke_vm_memory": [],
                            "gke_cluster_utilization": [],
                            "gke_node_utilization": [],
                            "gke_pod_utilization": [],
                            "gke_namespace_utilization": [],
                            "redis_throughput": [],
                            "kafka_throughput": [],
                            "redis_utilization": [],
                            "kafka_utilization": [],
                        }
                    ],
                },
            ),
            CheckResult(
                collector="services",
                status=Status.OK,
                summary="Cloud SQL instances discovered=0",
                details={
                    "cloud_sql": [],
                    "redis": [
                        {
                            "status": "skipped_config",
                            "reason": "redis disabled in config",
                        }
                    ],
                    "managed_kafka": [
                        {
                            "status": "skipped_config",
                            "reason": "managed_kafka disabled in config",
                        }
                    ],
                },
            ),
        ],
    )

    target = ensure_report_directory(tmp_path, "shared-mon", 2026, 20)
    markdown_path = write_report_markdown(report, target)

    markdown_text = markdown_path.read_text(encoding="utf-8")
    assert "Redis Throughput Trend (7-Day):" not in markdown_text
    assert "Redis Utilization Details (7-Day):" not in markdown_text
    assert "Managed Kafka Throughput Trend (7-Day):" not in markdown_text
    assert "Managed Kafka Utilization Details (7-Day):" not in markdown_text
    assert "Redis instances" not in markdown_text
    assert "Managed Kafka clusters" not in markdown_text
    assert "Redis Inventory:" not in markdown_text
    assert "Managed Kafka Inventory:" not in markdown_text
    assert "redis disabled in config" not in markdown_text
    assert "managed_kafka disabled in config" not in markdown_text


def test_report_markdown_escapes_preflight_error_text(tmp_path: Path) -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="preflight",
                status=Status.FAILED,
                summary="failed <script>alert(1)</script>",
                details={
                    "checks": [
                        {
                            "name": "api`<bad>",
                            "status": "failed",
                            "message": "<script>alert(1)</script> raw & issue | pipe",
                        }
                    ]
                },
            )
        ],
    )
    target = ensure_report_directory(tmp_path, "dev", 2026, 20)

    markdown_path = write_report_markdown(report, target)
    markdown_text = markdown_path.read_text(encoding="utf-8")

    assert "<script>" not in markdown_text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in markdown_text
    assert "raw & issue | pipe" in markdown_text
    assert "`api'&lt;bad&gt;`" in markdown_text


def test_platform_inventory_splits_compute_instances_from_standalone_vms(
    tmp_path: Path,
) -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="services",
                status=Status.OK,
                summary="Collected managed service inventory",
                details={
                    "compute_instances": [
                        {"name": "gke-example-node-a", "project": "example-dev-project"},
                        {"name": "gke-example-node-b", "project": "example-dev-project"},
                        {"name": "example-dev-linux-vm-01", "project": "example-dev-project"},
                    ]
                },
            )
        ],
    )

    target = ensure_report_directory(tmp_path, "dev", 2026, 20)
    markdown_path = write_report_markdown(report, target)

    markdown_text = markdown_path.read_text(encoding="utf-8")

    assert "| Compute Engine instances | 3 instances |" in markdown_text
    assert "| Standalone Compute VMs | 1 VM |" in markdown_text
    assert "| Compute VMs | 3 VMs |" not in markdown_text
    assert "Compute Engine Instance Inventory:" in markdown_text
    assert "Includes standalone VMs and GKE node VMs." in markdown_text


def test_reporting_autoscaling_scope_summarizes_cluster_policy(tmp_path: Path) -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="kubernetes_health",
                status=Status.OK,
                summary="ok",
                details={
                    "clusters": [
                        {
                            "cluster": "dev-cluster",
                            "hpa": {
                                "workload_hpa_total": 0,
                                "workload_hpas_at_max": 0,
                                "workload_hpa_failure_count": 0,
                                "platform_hpa_total": 2,
                                "platform_hpas_at_max": 0,
                                "platform_hpa_failure_count": 1,
                                "policy": {
                                    "workload_hpa": {"status": "informational"},
                                    "platform_hpa": {"status": "informational"},
                                },
                            },
                        }
                    ]
                },
            ),
            CheckResult(
                collector="gke_inventory",
                status=Status.OK,
                summary="ok",
                details={
                    "autoscaling_policy": {
                        "node_pool_autoscaling": {"status": "assessed"},
                    },
                    "clusters": [
                        {
                            "name": "dev-cluster",
                            "node_pools": [
                                {
                                    "name": "apps",
                                    "autoscaling_enabled": True,
                                    "autoscaling_min": 0,
                                    "autoscaling_max": 3,
                                },
                                {
                                    "name": "platform",
                                    "autoscaling_enabled": True,
                                    "autoscaling_min": 1,
                                    "autoscaling_max": 1,
                                },
                            ],
                        }
                    ],
                },
            ),
        ],
    )

    target = ensure_report_directory(tmp_path, "dev", 2026, 20)
    markdown_path = write_report_markdown(report, target)

    markdown_text = markdown_path.read_text(encoding="utf-8")
    assert "## Autoscaling Scope" in markdown_text
    assert (
        "| Cluster | Workload HPA Policy | Workload HPA Observed | Platform / Istio HPA | "
        "Node Pool Autoscaling | Assessment |"
    ) in markdown_text
    assert (
        "| dev-cluster | Not required for this environment | 0 observed; expected "
        "for environment policy | 2 observed; 0 at max replicas; 1 with issues; "
        "visibility only, not scored | 2/2 node pools enabled; total range 1-4 nodes | "
        "Matches environment policy; review 1 informational autoscaling signal(s). |"
    ) in markdown_text
    assert "Application workload | Kubernetes HPA" not in markdown_text
    assert "ranges:" not in markdown_text


def test_reporting_can_include_collector_warnings_and_gaps(tmp_path: Path) -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="preflight",
                status=Status.WARNING,
                summary="partial evidence",
                details={"checks": []},
            )
        ],
    )

    target = ensure_report_directory(tmp_path, "dev", 2026, 20)
    markdown_path = write_report_markdown(
        report,
        target,
        include_collector_warnings_and_gaps=True,
    )

    markdown_text = markdown_path.read_text(encoding="utf-8")
    assert "## Assessment Gaps And Action Items" not in markdown_text
    assert "Warning-level findings were observed." not in markdown_text


def test_reporting_renders_prometheus_monitoring_summary(tmp_path: Path) -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="prometheus_monitoring",
                status=Status.WARNING,
                summary=(
                    "Collected Prometheus monitoring for 1 cluster/environment scope(s); "
                    "hpa_failure_condition_series_7d=2; down_targets=1"
                ),
                details={
                    "url": "https://prometheus.example",
                    "build": {"version": "3.7.2"},
                    "scope": {
                        "labels": {
                            "environment": ["dev"],
                            "cluster": ["dev-cluster"],
                        },
                        "window_days": 7,
                    },
                    "clusters": [
                        {
                            "environment": "dev",
                            "cluster": "dev-cluster",
                            "status": "warning",
                            "hpa_total": 4,
                            "hpa_current_failure_condition_series": 0,
                            "hpa_failure_condition_series_7d": 2,
                            "hpa_current_scaling_limited": 1,
                            "hpa_scaling_limited_7d": 3,
                            "target_total": 12,
                            "target_down": 1,
                            "down_jobs": [{"job": "coredns", "down_targets": 1}],
                        }
                    ],
                    "hpa_failure_conditions_7d": [
                        {
                            "environment": "dev",
                            "cluster": "dev-cluster",
                            "condition": "ScalingActive",
                            "status": "false",
                            "condition_series_7d": 2,
                        }
                    ],
                    "time_series": [
                        {
                            "name": "cluster_cpu_utilization_percent",
                            "title": "Cluster CPU Utilization (%)",
                            "unit": "percent",
                            "query": "100 * example_query",
                            "start": 1779880000.0,
                            "end": 1779890000.0,
                            "step_seconds": 3600,
                            "series": [
                                {
                                    "label": "dev/dev-cluster",
                                    "metric": {
                                        "cluster": "dev-cluster",
                                    },
                                    "values": [
                                        [1779880000.0, 12.5],
                                        [1779890000.0, 18.25],
                                    ],
                                }
                            ],
                        }
                    ],
                },
            )
        ],
    )

    target = ensure_report_directory(tmp_path, "dev", 2026, 20)
    markdown_path = write_report_markdown(report, target)
    html_path = write_report_html(report, target)
    pdf_path = write_report_pdf(report, target)

    markdown_text = markdown_path.read_text(encoding="utf-8")
    assert "### Monitoring Stack (Needs Review)" in markdown_text
    assert "Summary: Collected monitoring" not in markdown_text
    assert (
        "- Reason: 2 HPA failure condition series in 7 days; "
        "1 currently scaling-limited HPA series; 1 down monitoring target"
    ) in markdown_text
    assert "Prometheus Version: `3.7.2`" not in markdown_text
    assert "Monitoring Stack Metric Summary:" in markdown_text
    assert "| Cluster CPU Utilization (%) | dev/dev-cluster | 18.2% |" in markdown_text
    assert "Historical HPA Failure Condition Series (7-Day)" in markdown_text
    assert "Historical Scaling-Limited Series (7-Day)" in markdown_text
    assert "### Monitoring Stack Trends" in markdown_text
    assert "monitoring stack stack" not in markdown_text
    assert "![Cluster CPU Utilization (%)]" in markdown_text
    assert "Prometheus/Grafana" in markdown_text
    assert (
        target / "evidence" / "charts" / "monitoring-stack-cluster-cpu-utilization-percent.png"
    ).exists()
    assert 'src="evidence/charts/monitoring-stack-cluster-cpu-utilization-percent.png"' in (
        html_path.read_text(encoding="utf-8")
    )
    assert pdf_path.read_bytes().startswith(b"%PDF")
    assert "Autoscaler Failure Conditions (7-Day):" in markdown_text
    assert (
        "- Informational historical monitoring signal. Current autoscaling policy scope "
        "is summarized in Autoscaling Scope."
    ) in markdown_text
    assert "coredns:1" in markdown_text


def test_reporting_surfaces_network_warning_driver_next_to_status(tmp_path: Path) -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="network",
                status=Status.WARNING,
                summary="Collected network posture for 1 project(s)",
                details={
                    "projects": [
                        {
                            "project": "example-dev-project",
                            "status": "warning",
                            "network_count": 1,
                            "firewall_rule_count": 4,
                            "firewall_disabled_count": 0,
                            "peering_count": 2,
                            "peering_inactive_count": 1,
                            "forwarding_rule_count": 1,
                            "dns_zone_count": 1,
                            "response_policy_count": 0,
                        }
                    ]
                },
            )
        ],
    )

    target = ensure_report_directory(tmp_path, "dev", 2026, 20)
    markdown_path = write_report_markdown(report, target)

    markdown_text = markdown_path.read_text(encoding="utf-8")
    assert "Overall Status Driver" not in markdown_text
    assert (
        "- Network: example-dev-project: 1 inactive VPC peering. Evidence: "
        "1 project; 1 VPC network; 4 firewall rules; 1 inactive VPC peering; "
        "1 DNS zone; 0 missing required DNS zones. Where to look: "
        "[Network And DNS Evidence](#network-and-dns-posture)."
    ) in markdown_text
    assert "| Area | Assessment Result | Immediate Action | Evidence Link |" in markdown_text
    assert (
        "| Network | Needs Review | Review peering, DNS, firewall, and in-cluster DNS evidence "
        "for the affected scope. | [Network And DNS Evidence](#network-and-dns-posture) |"
    ) in markdown_text
    assert "Where to look: [Network And DNS Evidence](#network-and-dns-posture)." in markdown_text
    assert "- Service Mesh: No evidence was collected." not in markdown_text
    assert "- Reason: example-dev-project: 1 inactive VPC peering" in markdown_text
    assert "Cloud DNS Policy Findings" not in markdown_text


def test_reporting_labels_internal_dns_checks_without_claiming_app_connectivity(
    tmp_path: Path,
) -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="network",
                status=Status.OK,
                summary=(
                    "Collected network posture for 1 project(s); in-cluster DNS checks=2, failed=0"
                ),
                details={
                    "projects": [
                        {
                            "project": "example-dev-project",
                            "status": "ok",
                            "network_count": 1,
                            "firewall_rule_count": 4,
                            "firewall_disabled_count": 0,
                            "peering_count": 2,
                            "peering_inactive_count": 0,
                            "forwarding_rule_count": 1,
                            "dns_zone_count": 1,
                        }
                    ],
                    "internal_dns": {
                        "status": "ok",
                        "required_service_fqdns": [
                            "redis.default.svc.cluster.local",
                            "kafka.default.svc.cluster.local",
                        ],
                        "checked_fqdn_total": 2,
                        "failed_fqdn_total": 0,
                        "clusters": [
                            {
                                "cluster": "dev-cluster",
                                "project": "example-dev-project",
                                "region": "us-central1",
                                "status": "ok",
                                "checked_fqdn_count": 2,
                                "failed_fqdns": [],
                            }
                        ],
                    },
                },
            )
        ],
    )

    target = ensure_report_directory(tmp_path, "dev", 2026, 20)
    markdown_path = write_report_markdown(report, target)

    markdown_text = markdown_path.read_text(encoding="utf-8")
    assert "In-Cluster DNS Resolution Checks:" in markdown_text
    assert (
        "- Source: Kubernetes API service inventory check for required service FQDNs "
        "in each cluster."
    ) in markdown_text
    assert "Application Dependency Connectivity" not in markdown_text
    assert "can connect to SQL" not in markdown_text
    assert "can connect to Redis" not in markdown_text
    assert "can connect to Kafka" not in markdown_text


def test_reporting_only_marks_firewall_rules_truncated_when_limited(tmp_path: Path) -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="network",
                status=Status.OK,
                summary="Collected network posture for 1 project(s)",
                details={
                    "projects": [
                        {
                            "project": "example-dev-project",
                            "status": "ok",
                            "firewall_rule_count": 1,
                            "firewall_rules": [
                                {
                                    "name": "allow-health-checks",
                                    "network": "example-vpc",
                                    "direction": "INGRESS",
                                    "priority": "1000",
                                }
                            ],
                        }
                    ]
                },
            )
        ],
    )

    target = ensure_report_directory(tmp_path, "dev", 2026, 20)
    markdown_path = write_report_markdown(report, target)

    markdown_text = markdown_path.read_text(encoding="utf-8")
    assert "Firewall Rules:" in markdown_text
    assert "Firewall Rules (Sample):" not in markdown_text
    assert "Firewall Rules (First 50 Shown):" not in markdown_text


def test_reporting_marks_network_tables_when_truncated(tmp_path: Path) -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="network",
                status=Status.OK,
                summary="Collected network posture for 1 project(s)",
                details={
                    "projects": [
                        {
                            "project": "example-dev-project",
                            "status": "ok",
                            "firewall_rule_count": 51,
                            "firewall_rules": [
                                {
                                    "name": f"rule-{index}",
                                    "network": "example-vpc",
                                    "direction": "INGRESS",
                                    "priority": "1000",
                                }
                                for index in range(51)
                            ],
                        }
                    ]
                },
            )
        ],
    )

    target = ensure_report_directory(tmp_path, "dev", 2026, 20)
    markdown_path = write_report_markdown(report, target)

    markdown_text = markdown_path.read_text(encoding="utf-8")
    assert "Firewall Rules (First 50 Shown):" in markdown_text
    assert "rule-49" in markdown_text
    assert "rule-50" not in markdown_text


def test_reporting_marks_backup_not_applicable_when_no_backup_scope(tmp_path: Path) -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="backup",
                status=Status.OK,
                summary="GKE backup plans=0, Cloud SQL instances with backup enabled=0",
                details={
                    "gke_backup": [
                        {
                            "project": "example-dev-project",
                            "region": "us-central1",
                            "status": "ok",
                            "plan_count": 0,
                            "plans": [],
                        }
                    ],
                    "cloud_sql_backup": [
                        {
                            "project": "example-dev-project",
                            "status": "ok",
                            "instance_count": 0,
                            "instances": [],
                        }
                    ],
                },
            )
        ],
    )

    target = ensure_report_directory(tmp_path, "dev", 2026, 20)
    markdown_path = write_report_markdown(report, target)

    markdown_text = markdown_path.read_text(encoding="utf-8")
    assert (
        "- Backup Coverage: No backup policy applies to this component. "
        "Where to look: [Backup Evidence](#backup-and-recovery-posture)."
    ) in markdown_text
    assert "| Backup Coverage | No Findings | None detected |" not in markdown_text
    assert "| Area | Assessment Result | Reason | Observed Details |" not in markdown_text


def test_reporting_collapses_out_of_scope_backup_detail(tmp_path: Path) -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="backup",
                status=Status.OK,
                summary="Backup policy assessment is out of scope for this environment",
                details={
                    "policy": {
                        "status": "out_of_scope",
                        "reason": "Backup policy assessment is out of scope for dev.",
                    },
                    "gke_backup": [
                        {
                            "status": "skipped_config",
                            "reason": "Backup policy assessment is out of scope for dev.",
                            "plan_count": 0,
                        }
                    ],
                    "cloud_sql_backup": [
                        {
                            "status": "skipped_config",
                            "reason": "Backup policy assessment is out of scope for dev.",
                            "instance_count": 0,
                            "instances": [],
                        }
                    ],
                    "elasticsearch_backup": [
                        {
                            "name": "not configured",
                            "status": "skipped_config",
                            "reason": "Backup policy assessment is out of scope for dev.",
                        }
                    ],
                },
            )
        ],
    )

    target = ensure_report_directory(tmp_path, "dev", 2026, 20)
    markdown_path = write_report_markdown(report, target)

    markdown_text = markdown_path.read_text(encoding="utf-8")
    assert "Backup Coverage" not in markdown_text
    assert "Backup policy assessment is out of scope for dev." not in markdown_text
    assert "## Backup And Recovery Posture" not in markdown_text
    assert "GKE Backup Plans:" not in markdown_text
    assert "Cloud SQL Backup Configuration:" not in markdown_text
    assert "Elasticsearch Backup Checks:" not in markdown_text


def test_reporting_collapses_all_healthy_load_balancer_backend_rows(tmp_path: Path) -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="services",
                status=Status.OK,
                summary="Collected managed service inventory",
                details={
                    "load_balancers": [
                        {
                            "backend_service": "healthy-backend-a",
                            "project": "example-dev-project",
                            "scope": "us-central1",
                            "backends": 2,
                            "health_checks": 1,
                            "backend_health_counts": {"HEALTHY": 2},
                            "status": "ok",
                        },
                        {
                            "backend_service": "healthy-backend-b",
                            "project": "example-dev-project",
                            "scope": "us-central1",
                            "backends": 1,
                            "health_checks": 1,
                            "backend_health_counts": {"HEALTHY": 1},
                            "status": "ok",
                        },
                    ]
                },
            )
        ],
    )

    target = ensure_report_directory(tmp_path, "dev", 2026, 20)
    markdown_path = write_report_markdown(report, target)

    markdown_text = markdown_path.read_text(encoding="utf-8")
    assert "Load Balancer Backend Health:" in markdown_text
    assert (
        "- Summary: 2 backend services reviewed; 0 unhealthy backends; 0 unknown backends"
        in markdown_text
    )
    assert (
        "- Detail: All reported backend services have no unhealthy or unknown backend health."
        in markdown_text
    )
    assert "Backend Services Requiring Review:" not in markdown_text
    assert "healthy-backend-a" not in markdown_text
    assert "healthy-backend-b" not in markdown_text


def test_reporting_keeps_load_balancer_backend_rows_requiring_review(tmp_path: Path) -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="services",
                status=Status.WARNING,
                summary="Collected managed service inventory",
                details={
                    "load_balancers": [
                        {
                            "backend_service": "healthy-backend",
                            "project": "example-dev-project",
                            "scope": "us-central1",
                            "backends": 2,
                            "health_checks": 1,
                            "backend_health_counts": {"HEALTHY": 2},
                            "status": "ok",
                        },
                        {
                            "backend_service": "unhealthy-backend",
                            "project": "example-dev-project",
                            "scope": "us-central1",
                            "backends": 2,
                            "health_checks": 1,
                            "backend_health_counts": {"HEALTHY": 1, "UNHEALTHY": 1},
                            "protocol": "HTTP",
                            "scheme": "INTERNAL",
                            "status": "warning",
                            "reason": "one backend unhealthy",
                        },
                    ]
                },
            )
        ],
    )

    target = ensure_report_directory(tmp_path, "dev", 2026, 20)
    markdown_path = write_report_markdown(report, target)

    markdown_text = markdown_path.read_text(encoding="utf-8")
    assert (
        "- Summary: 2 backend services reviewed; 1 backend service requiring review; "
        "1 unhealthy backend; 0 unknown backends"
    ) in markdown_text
    assert "Backend Services Requiring Review:" in markdown_text
    assert "unhealthy-backend" in markdown_text
    assert "| healthy-backend |" not in markdown_text


def test_reporting_leads_audit_with_named_configuration_changes(tmp_path: Path) -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="audit",
                status=Status.OK,
                summary="Collected audit activity for 1 project(s)",
                details={
                    "projects": [
                        {
                            "project": "example-dev-project",
                            "status": "ok",
                            "audit_change_count": 4,
                            "audit_entries_limited": False,
                            "review_candidate_entry_count": 2,
                            "review_candidate_entries_limited": False,
                            "high_risk_events": 0,
                            "high_risk_entries_limited": False,
                            "new_secret_events": 1,
                            "noisy_events_filtered": 1,
                            "meaningful_change_events": 2,
                            "meaningful_configmap_events": 1,
                            "meaningful_secret_events": 1,
                            "top_meaningful_methods": [
                                {"method": "io.k8s.core.v1.configmaps.update", "count": 1}
                            ],
                            "meaningful_recent_events": [
                                {
                                    "timestamp": "2026-05-14T00:00:00Z",
                                    "action": "update",
                                    "method": "io.k8s.core.v1.configmaps.update",
                                    "principal": "system:cluster-autoscaler",
                                    "resource": (
                                        "core/v1/namespaces/kube-system/configmaps/"
                                        "cluster-autoscaler-status"
                                    ),
                                },
                                {
                                    "timestamp": "2026-05-14T00:01:00Z",
                                    "action": "update",
                                    "method": "io.k8s.core.v1.configmaps.update",
                                    "principal": "user@example.com",
                                    "resource": "core/v1/namespaces/apps/configmaps/app-config",
                                },
                                {
                                    "timestamp": "2026-05-14T00:02:00Z",
                                    "action": "create",
                                    "method": (
                                        "google.cloud.secretmanager.v1.SecretManagerService."
                                        "CreateSecret"
                                    ),
                                    "principal": "user@example.com",
                                    "resource": "projects/example-dev-project/secrets/db-password",
                                },
                            ],
                        }
                    ]
                },
            )
        ],
    )

    target = ensure_report_directory(tmp_path, "dev", 2026, 20)
    markdown_path = write_report_markdown(report, target)

    markdown_text = markdown_path.read_text(encoding="utf-8")
    assert "### Configuration Change Audit" in markdown_text
    assert "### Configuration Change Audit (No Findings)" not in markdown_text
    assert "Secret values are never collected or displayed." in markdown_text
    assert "Configuration And Security Changes Requiring Review:" in markdown_text
    assert (
        "Configuration And Security Changes Requiring Review (Recent Sample):" not in markdown_text
    )
    assert (
        "| example-dev-project | 2026-05-14T00:01:00Z | update | ConfigMap | apps | "
        "app-config | user@example.com |"
    ) in markdown_text
    assert (
        "| example-dev-project | 2026-05-14T00:02:00Z | create | Secret | "
        "example-dev-project | db-password | user@example.com |"
    ) in markdown_text
    assert "cluster-autoscaler-status" not in markdown_text
    assert ("| example-dev-project | 4 | 2 | 2 | 1 | 1 | 0 | 1 |") in markdown_text
    assert "Most Common Configuration Change Activity:" not in markdown_text
    assert "| example-dev-project | Kubernetes ConfigMap update | 1 |" not in markdown_text


def test_reporting_keeps_audit_section_status_free_for_warnings(tmp_path: Path) -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="audit",
                status=Status.WARNING,
                summary="1 high-risk audit event found.",
                details={
                    "projects": [
                        {
                            "project": "example-dev-project",
                            "status": "warning",
                            "targeted_events": 3,
                            "review_candidate_entry_count": 2,
                            "high_risk_events": 1,
                            "new_secret_events": 1,
                            "meaningful_change_events": 2,
                            "meaningful_configmap_events": 1,
                            "meaningful_secret_events": 1,
                            "top_meaningful_methods": [
                                {"method": "Kubernetes Secret change", "count": 1}
                            ],
                            "meaningful_recent_events": [
                                {
                                    "timestamp": "2026-05-14T00:02:00Z",
                                    "action": "create",
                                    "method": (
                                        "google.cloud.secretmanager.v1.SecretManagerService."
                                        "CreateSecret"
                                    ),
                                    "principal": "user@example.com",
                                    "resource": "projects/example-dev-project/secrets/db-password",
                                }
                            ],
                        }
                    ]
                },
            )
        ],
    )

    target = ensure_report_directory(tmp_path, "dev", 2026, 20)
    markdown_path = write_report_markdown(report, target)

    markdown_text = markdown_path.read_text(encoding="utf-8")
    assert "### Configuration Change Audit" in markdown_text
    assert "Review Activity" not in markdown_text
    assert "| example-dev-project | 0 | 2 | 2 | 1 | 1 | 1 | 0 |" in markdown_text
    assert "### Configuration Change Audit (Needs Review)" not in markdown_text


def test_html_tables_use_content_sizing_without_cell_wrapping(tmp_path: Path) -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="monitoring",
                status=Status.OK,
                summary="ok",
                details={
                    "projects": [
                        {
                            "project": "example-dev-project",
                            "status": "ok",
                            "alert_policy_total": 1,
                            "alert_policy_enabled": 1,
                            "alert_policy_disabled": 0,
                            "alerts": {"open_alerts": 0},
                            "notification_channels": {},
                            "sample_policies": [
                                {
                                    "display_name": "Very long policy name that should scroll",
                                    "enabled": True,
                                    "condition_count": 1,
                                }
                            ],
                        }
                    ]
                },
            )
        ],
    )

    target = ensure_report_directory(tmp_path, "dev", 2026, 20)
    html_path = write_report_html(report, target)
    html_text = html_path.read_text(encoding="utf-8")

    assert '<div class="table-wrap table-wrap-cols-' in html_text
    assert '<table class="report-table report-table-cols-' in html_text
    assert "width: max-content;" in html_text
    assert "table-layout: auto;" in html_text
    assert "white-space: nowrap;" in html_text
    assert "@media print" in html_text
    assert "table.report-table-extra-wide { font-size: 5.95pt; }" in html_text
    assert "overflow-wrap: anywhere;" in html_text
    assert "overflow-wrap: normal;" in html_text
    assert "word-break: normal;" in html_text
    assert "hyphens: none;" in html_text
    assert "word-break: break-word;" not in html_text


def test_html_print_tables_are_classified_by_column_count() -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[],
    )
    brand = BrandProfile()
    markdown_text = "\n".join(
        [
            "# Example",
            "",
            (
                "| Cluster | Proxy Name | Proxy Type | Namespace | Assessment | "
                "Deployment Ready/Desired | Pods Ready/Total | Service Mode | "
                "Service Ports | Endpoint Ports | Ready Endpoints | Configured Resources |"
            ),
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
            (
                "| dev-cluster | squid-proxy | squid | istio-proxy | No Findings | "
                "1/1 | 1/1 | LoadBalancer | 3128 | 3128 | 1 | configmap present |"
            ),
        ]
    )

    html_text = render_report_html(report, markdown_text, brand)

    assert '<div class="table-wrap table-wrap-cols-12 table-wrap-ultra-wide">' in html_text
    assert (
        '<table class="report-table report-table-cols-12 report-table-ultra-wide" '
        'data-column-count="12">'
    ) in html_text


def test_replay_artifacts_include_prometheus_evidence_and_charts(tmp_path: Path) -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="prometheus_monitoring",
                status=Status.WARNING,
                summary="Prometheus reachable with HPA warning series.",
                details={
                    "url": "https://prometheus.example/query",
                    "build": {"version": "2.52.0"},
                    "scope": {"labels": {"cluster": ["dev-cluster"]}},
                    "clusters": [
                        {
                            "environment": "dev",
                            "cluster": "dev-cluster",
                            "status": "warning",
                            "hpa_total": 2,
                            "hpa_current_failure_condition_series": 1,
                            "hpa_failure_condition_series_7d": 1,
                            "hpa_current_scaling_limited": 0,
                            "hpa_scaling_limited_7d": 0,
                            "target_total": 12,
                            "target_down": 1,
                            "down_jobs": [{"job": "kubernetes-pods", "down_targets": 1}],
                        }
                    ],
                    "hpa_failure_conditions_7d": [
                        {
                            "environment": "dev",
                            "cluster": "dev-cluster",
                            "condition": "AbleToScale",
                            "status": "false",
                            "condition_series_7d": 1,
                        }
                    ],
                    "time_series": [
                        {
                            "name": "hpa_failure_conditions_7d",
                            "title": "HPA Failure Conditions (7-Day)",
                            "unit": "count",
                            "series": [
                                {
                                    "label": "dev/dev-cluster/AbleToScale=false",
                                    "values": [
                                        [1779475200.0, 1.0],
                                        [1779561600.0, 2.0],
                                        [1779388800.0, 0.0],
                                    ],
                                }
                            ],
                        }
                    ],
                },
            )
        ],
    )

    target = ensure_report_directory(tmp_path, "dev", 2026, 20)
    markdown_path = write_report_markdown(report, target, include_evidence_index=True)
    html_path = write_report_html(report, target)
    pdf_path = write_report_pdf(report, target)
    evidence_path = write_collector_evidence(report, target)

    chart_path = target / "evidence" / "charts" / "monitoring-stack-hpa-failure-conditions-7d.png"
    collector_evidence_path = target / "evidence" / "collectors" / "prometheus-monitoring.json"
    markdown_text = markdown_path.read_text(encoding="utf-8")
    html_text = html_path.read_text(encoding="utf-8")
    collector_payload = json.loads(collector_evidence_path.read_text(encoding="utf-8"))

    assert evidence_path.exists()
    assert chart_path.exists()
    assert chart_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert collector_evidence_path.exists()
    assert collector_payload["collector"] == "prometheus_monitoring"
    assert collector_payload["details"]["time_series"][0]["name"] == "hpa_failure_conditions_7d"
    assert "### Monitoring Stack (Needs Review)" in markdown_text
    assert "Monitoring Stack Metric Summary:" in markdown_text
    assert (
        "| HPA Failure Conditions (7-Day) | dev/dev-cluster/AbleToScale=false | 2 | 0 | 2 | 1 |"
    ) in markdown_text
    assert "### Monitoring Stack Trends" in markdown_text
    assert "![HPA Failure Conditions (7-Day)](evidence/charts/" in markdown_text
    assert "Prometheus/Grafana" in markdown_text
    assert "prometheus" not in markdown_text
    assert "Samples plotted:" not in markdown_text
    assert "- Chart evidence: `evidence/charts/*.png`" in markdown_text
    assert '<img alt="HPA Failure Conditions (7-Day)" src="evidence/charts/' in html_text
    assert ".report-content img:not(.brand-logo)" in html_text
    assert pdf_path.read_bytes().startswith(b"%PDF")


def test_reporting_evidence_index_is_opt_in(tmp_path: Path) -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[CheckResult(collector="preflight", status=Status.OK, summary="ok")],
    )

    target = ensure_report_directory(tmp_path, "dev", 2026, 20)
    markdown_path = write_report_markdown(
        report,
        target,
        include_evidence_index=True,
    )

    markdown_text = markdown_path.read_text(encoding="utf-8")
    assert "## Evidence Index" in markdown_text
    assert "Report HTML" not in markdown_text
    assert "Report PDF" not in markdown_text
    assert "- Assessment run evidence: `evidence/collector-status.json`" in markdown_text
    assert "- Detailed evidence by check: `evidence/collectors/*.json`" in markdown_text
    assert "- Chart evidence: none generated for this report" in markdown_text


def test_reporting_concise_mode_excludes_full_technical_sections(tmp_path: Path) -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="preflight", status=Status.OK, summary="ok", details={"checks": []}
            ),
            CheckResult(
                collector="kubernetes_health",
                status=Status.OK,
                summary="ok",
                details={"clusters": []},
            ),
        ],
    )

    target = ensure_report_directory(tmp_path, "dev", 2026, 20)
    markdown_path = write_report_markdown(report, target, report_mode="concise")

    markdown_text = markdown_path.read_text(encoding="utf-8")
    assert "## Operational Summary (Concise)" in markdown_text
    assert "### Operational Checks" in markdown_text
    assert "## Technical Evidence Appendix" not in markdown_text
    assert "## Platform Operations Appendix" not in markdown_text
    assert "## Assessment Gaps And Action Items" not in markdown_text


def test_reporting_concise_mode_with_appendix_includes_full_sections(tmp_path: Path) -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="preflight", status=Status.OK, summary="ok", details={"checks": []}
            ),
            CheckResult(
                collector="kubernetes_health",
                status=Status.OK,
                summary="ok",
                details={"clusters": []},
            ),
        ],
    )

    target = ensure_report_directory(tmp_path, "dev", 2026, 20)
    markdown_path = write_report_markdown(
        report,
        target,
        report_mode="concise",
        include_technical_appendix=True,
    )

    markdown_text = markdown_path.read_text(encoding="utf-8")
    assert "## Operational Summary (Concise)" in markdown_text
    assert "## Platform Operations Appendix" not in markdown_text
    assert "## Technical Evidence Appendix" in markdown_text
    assert "## Assessment Gaps And Action Items" not in markdown_text


def test_reporting_concise_mode_can_include_collector_warnings_and_gaps(
    tmp_path: Path,
) -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="preflight", status=Status.OK, summary="ok", details={"checks": []}
            ),
            CheckResult(
                collector="kubernetes_health",
                status=Status.OK,
                summary="ok",
                details={"clusters": []},
            ),
        ],
    )

    target = ensure_report_directory(tmp_path, "dev", 2026, 20)
    markdown_path = write_report_markdown(
        report,
        target,
        report_mode="concise",
        include_technical_appendix=True,
        include_collector_warnings_and_gaps=True,
    )

    markdown_text = markdown_path.read_text(encoding="utf-8")
    assert "## Assessment Gaps And Action Items" not in markdown_text


def test_reporting_renders_log_bucket_stored_volume(tmp_path: Path) -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="logging",
                status=Status.OK,
                summary="ok",
                details={
                    "projects": [
                        {
                            "project": "example-dev-project",
                            "status": "ok",
                            "sink_count": 1,
                            "bucket_count": 1,
                            "pubsub_topic_count": 0,
                            "pubsub_subscription_total": 0,
                            "splunk_hints": {"matched": False},
                            "buckets": [
                                {
                                    "name": (
                                        "projects/example-dev-project/locations/"
                                        "us-central1/buckets/app"
                                    ),
                                    "location": "us-central1",
                                    "retention_days": 30,
                                    "locked": False,
                                }
                            ],
                            "logging_metrics": {
                                "bytes_ingested_total": 2048.0,
                                "bytes_ingested_peak_hour": 1024.0,
                                "bytes_stored_peak": 4096.0,
                                "bytes_stored_metric_status": "available",
                                "bucket_ingestion": [
                                    {
                                        "bucket": "app",
                                        "location": "us-central1",
                                        "bytes_ingested_total": 2048.0,
                                        "bytes_ingested_peak_hour": 1024.0,
                                    }
                                ],
                                "bucket_storage": [
                                    {
                                        "bucket": "app",
                                        "location": "us-central1",
                                        "data_type": "CHARGED",
                                        "bytes_stored_peak": 4096.0,
                                    }
                                ],
                            },
                        }
                    ]
                },
            )
        ],
    )

    target = ensure_report_directory(tmp_path, "dev", 2026, 20)
    markdown_path = write_report_markdown(report, target)

    markdown_text = markdown_path.read_text(encoding="utf-8")
    assert "Log Bucket Ingestion (7-Day):" in markdown_text
    assert "| example-dev-project | app | us-central1 | 2.00 KiB | 1.00 KiB |" in markdown_text
    assert "Long-Retention Log Storage Metric:" in markdown_text
    assert "| example-dev-project | app | us-central1 | 4.00 KiB | CHARGED |" in markdown_text
    assert "bucket-level ingestion volume" in markdown_text


def test_reporting_marks_missing_log_bucket_stored_metric(tmp_path: Path) -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="logging",
                status=Status.OK,
                summary="ok",
                details={
                    "projects": [
                        {
                            "project": "example-dev-project",
                            "status": "ok",
                            "sink_count": 1,
                            "bucket_count": 1,
                            "pubsub_topic_count": 0,
                            "pubsub_subscription_total": 0,
                            "splunk_hints": {"matched": False},
                            "buckets": [],
                            "logging_metrics": {
                                "bytes_ingested_total": 2048.0,
                                "bytes_ingested_peak_hour": 1024.0,
                                "bytes_stored_peak": None,
                                "bytes_stored_metric_status": "metric_not_returned",
                                "bucket_ingestion": [],
                                "bucket_storage": [],
                            },
                        }
                    ]
                },
            )
        ],
    )

    target = ensure_report_directory(tmp_path, "dev", 2026, 20)
    markdown_path = write_report_markdown(report, target)

    markdown_text = markdown_path.read_text(encoding="utf-8")
    assert "| example-dev-project | 2.00 KiB | 1.00 KiB | metric not returned |" in markdown_text
    assert "Metric was not returned for example-dev-project." in markdown_text


def test_reporting_marks_log_bucket_stored_metric_not_applicable(tmp_path: Path) -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="logging",
                status=Status.OK,
                summary="ok",
                details={
                    "projects": [
                        {
                            "project": "example-dev-project",
                            "status": "ok",
                            "sink_count": 1,
                            "bucket_count": 3,
                            "pubsub_topic_count": 0,
                            "pubsub_subscription_total": 0,
                            "splunk_hints": {"matched": False},
                            "buckets": [
                                {
                                    "name": (
                                        "projects/example-dev-project/locations/"
                                        "global/buckets/_Required"
                                    ),
                                    "location": "global",
                                    "retention_days": 400,
                                    "locked": True,
                                },
                                {
                                    "name": (
                                        "projects/example-dev-project/locations/"
                                        "global/buckets/_Default"
                                    ),
                                    "location": "global",
                                    "retention_days": 30,
                                    "locked": False,
                                },
                                {
                                    "name": (
                                        "projects/example-dev-project/locations/"
                                        "us-central1/buckets/app"
                                    ),
                                    "location": "us-central1",
                                    "retention_days": 7,
                                    "locked": False,
                                },
                            ],
                            "logging_metrics": {
                                "bytes_ingested_total": 2048.0,
                                "bytes_ingested_peak_hour": 1024.0,
                                "bytes_stored_peak": None,
                                "bytes_stored_metric_status": "not_applicable",
                                "bucket_ingestion": [],
                                "bucket_storage": [],
                            },
                        }
                    ]
                },
            )
        ],
    )

    target = ensure_report_directory(tmp_path, "dev", 2026, 20)
    markdown_path = write_report_markdown(report, target)

    markdown_text = markdown_path.read_text(encoding="utf-8")
    assert "| example-dev-project | 2.00 KiB | 1.00 KiB | not applicable |" in markdown_text
    assert "it is not total current bucket occupancy" in markdown_text
    assert (
        "Not applicable for example-dev-project: no non-`_Required` log bucket has "
        "retention above 30 days"
    ) in markdown_text


def test_write_logging_evidence_labels_sampled_pubsub_metrics(tmp_path: Path) -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="logging",
                status=Status.OK,
                summary="ok",
                details={
                    "projects": [
                        {
                            "project": "example-dev-project",
                            "status": "ok",
                            "sink_count": 1,
                            "bucket_count": 1,
                            "pubsub_topic_count": 1,
                            "pubsub_subscription_total": 12,
                            "pubsub_metric_subscriptions_checked": 10,
                            "pubsub_metric_subscription_sample_limit": 10,
                            "pubsub_metric_subscriptions_sampled": True,
                            "splunk_hints": {"matched": True},
                            "buckets": [],
                            "logging_metrics": {
                                "bytes_ingested_total": 0.0,
                                "bytes_ingested_peak_hour": 0.0,
                                "bytes_stored_peak": None,
                                "bytes_stored_metric_status": "metric_not_returned",
                                "bucket_ingestion": [],
                                "bucket_storage": [],
                            },
                            "pubsub_metrics": [
                                {
                                    "subscription_id": "splunk-subscription-0",
                                    "num_unacked_messages_peak": 2.0,
                                    "oldest_unacked_message_age_peak_seconds": 10.0,
                                    "delivery_latency_health_score_min": 1.0,
                                    "dead_letter_message_count_total": 0.0,
                                }
                            ],
                        }
                    ]
                },
            )
        ],
    )

    target = ensure_report_directory(tmp_path, "dev", 2026, 20)
    markdown_path = write_report_markdown(report, target)

    markdown_text = markdown_path.read_text(encoding="utf-8")
    assert "Logging Sink Pub/Sub Delivery Health Sample (7-Day):" in markdown_text
    assert "bounded sample of discovered logging sink topic subscriptions" in markdown_text
    assert "metrics checked 10 of 12 subscription(s)" in markdown_text
    assert "| example-dev-project | splunk-subscription-0 | 2 | 10 s | 1.00 | 0 |" in markdown_text


def test_write_logging_evidence_infers_pubsub_scope_for_legacy_json(tmp_path: Path) -> None:
    report = Report(
        environment="prod",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="logging",
                status=Status.OK,
                summary="ok",
                details={
                    "projects": [
                        {
                            "project": "example-prod-project",
                            "status": "ok",
                            "sink_count": 1,
                            "bucket_count": 1,
                            "pubsub_topic_count": 1,
                            "pubsub_subscription_total": 1,
                            "splunk_hints": {"matched": True},
                            "buckets": [],
                            "logging_metrics": {
                                "bytes_ingested_total": 0.0,
                                "bytes_ingested_peak_hour": 0.0,
                                "bytes_stored_peak": None,
                                "bytes_stored_metric_status": "metric_not_returned",
                                "bucket_ingestion": [],
                                "bucket_storage": [],
                            },
                            "pubsub_topics": [
                                {
                                    "subscription_count": 1,
                                    "sample_subscriptions": [
                                        "projects/example-prod-project/subscriptions/splunk-sub"
                                    ],
                                }
                            ],
                            "pubsub_metrics": [
                                {
                                    "subscription_id": "splunk-sub",
                                    "num_unacked_messages_peak": 2.0,
                                    "oldest_unacked_message_age_peak_seconds": 10.0,
                                    "delivery_latency_health_score_min": 1.0,
                                    "dead_letter_message_count_total": 0.0,
                                }
                            ],
                        }
                    ]
                },
            )
        ],
    )

    target = ensure_report_directory(tmp_path, "prod", 2026, 20)
    markdown_path = write_report_markdown(report, target)

    markdown_text = markdown_path.read_text(encoding="utf-8")
    assert "Logging Sink Pub/Sub Delivery Health (7-Day):" in markdown_text
    assert "Logging Sink Pub/Sub Delivery Health Sample (7-Day):" not in markdown_text
    assert "metrics checked 0 of 1 subscription(s)" not in markdown_text


def test_reporting_renders_mesh_api_proxy_status(tmp_path: Path) -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="mesh",
                status=Status.OK,
                summary="ok",
                details={
                    "clusters": [
                        {
                            "cluster": "dev-cluster",
                            "status": "ok",
                            "endpoint": "https://10.0.0.1",
                            "ingress_gateway": {
                                "ready_pods": 1,
                                "total_pods": 1,
                                "pod_names": ["istio-ingressgateway-abc"],
                                "ready_pod_names": ["istio-ingressgateway-abc"],
                            },
                            "east_west_gateway": {"ready_pods": 1, "total_pods": 1},
                            "istiod": {
                                "ready_pods": 2,
                                "total_pods": 2,
                                "pod_names": ["istiod-abc"],
                                "ready_pod_names": ["istiod-abc"],
                                "version": "1.29.1",
                            },
                            "remote_secret_count": 1,
                            "remote_secret_names": ["remote-test"],
                            "remote_clusters": {
                                "status": "ok",
                                "rows": [
                                    {
                                        "name": "test",
                                        "sync_status": "synced",
                                        "istiod": "istiod-123",
                                    }
                                ],
                            },
                            "proxy_status": {
                                "status": "ok",
                                "rows": [
                                    {
                                        "name": "istio-ingressgateway-abc",
                                        "cluster": "Kubernetes",
                                        "istiod": "istiod-123",
                                        "version": "1.29.1",
                                        "sync_state": "HTTP_ROUTE LISTENER",
                                    }
                                ],
                            },
                            "multicluster_sync": {
                                "status": "ok",
                                "expected_remote_links": 1,
                                "synced_remote_clusters": 1,
                                "missing_remote_links": 0,
                            },
                            "envoy_proxy_samples": [
                                {
                                    "pod": "istio-ingressgateway-abc",
                                    "status": "ok",
                                    "state": "LIVE",
                                    "istio_version": "1.29.1",
                                    "cluster_id": "dev-cluster",
                                    "network": "dev-network",
                                    "discovery_address": "istiod.istio-system.svc:15012",
                                }
                            ],
                            "mesh_api_proxies": [
                                {
                                    "name": "istio-proxy",
                                    "type": "nginx",
                                    "namespace": "istio-proxy",
                                    "status": "ok",
                                    "desired_replicas": 2,
                                    "ready_replicas": 2,
                                    "pod_total": 2,
                                    "pod_ready": 2,
                                    "service_type": "LoadBalancer",
                                    "service_ports": ["443"],
                                    "endpoint_ports": ["8443"],
                                    "endpoint_address_count": 2,
                                    "load_balancer_ingress": ["10.115.2.44"],
                                    "configmap": "istio-nginx-template",
                                    "certificate_secret": "istio-proxy-cert",
                                    "token_secret": "istio-proxy-token",
                                    "configmap_found": True,
                                    "certificate_secret_found": True,
                                    "token_secret_found": True,
                                    "control_plane_tunnel_logs": {
                                        "status": "ok",
                                        "tunnel_success_count": 3,
                                        "tunnel_failure_count": 1,
                                        "latest_success": {
                                            "target": "gke-test.us-central1.gke.goog:443"
                                        },
                                        "latest_failure": {
                                            "code": "503",
                                            "target": "gke-test.us-central1.gke.goog:443",
                                        },
                                    },
                                    "note": "",
                                }
                            ],
                        }
                    ]
                },
            )
        ],
    )

    target = ensure_report_directory(tmp_path, "dev", 2026, 20)
    markdown_path = write_report_markdown(report, target)

    markdown_text = markdown_path.read_text(encoding="utf-8")
    assert "Mesh API Proxy Readiness:" in markdown_text
    assert "Squid Proxy Status:" not in markdown_text
    assert (
        "| dev-cluster | istio-proxy | nginx | istio-proxy | No Findings | 2/2 | 2/2 | "
        "LoadBalancer | 443 | 8443 | 2 | 10.115.2.44 | "
        "configmap present; cert present; token present |  |"
    ) in markdown_text
    assert "Sampled Envoy Proxy State:" not in markdown_text
    assert "Remote Cluster Sync:" in markdown_text
    assert "Proxy Sync Status:" in markdown_text
    assert "Mesh API Proxy Control Plane Tunnel Evidence:" in markdown_text
    assert (
        "| Cluster | Mesh Component | Ready Pods | Pod Names | Ready Pod Names | Version | "
        "Proxy Envoy State | Proxy Cluster ID | Proxy Network | Proxy Discovery Address | "
        "Notes |"
    ) in markdown_text
    assert (
        "Proxy Envoy columns apply to Envoy-based gateway components. Istiod is shown "
        "with readiness and version because it is the mesh control plane."
    ) in markdown_text
    assert (
        "| dev-cluster | Ingress Gateway | 1/1 | istio-ingressgateway-abc | "
        "istio-ingressgateway-abc | 1.29.1 | LIVE | dev-cluster | dev-network | "
        "istiod.istio-system.svc:15012 |  |"
    ) in markdown_text
    assert (
        "| dev-cluster | Istiod | 2/2 | istiod-abc | istiod-abc | 1.29.1 | "
        "Not applicable | Not applicable | Not applicable | Not applicable | "
        "Istiod is the mesh control plane; proxy Envoy metadata is not applicable. |"
    ) in markdown_text
    assert (
        "| dev-cluster | istio-proxy | istio-proxy | No Findings | 3 | 1 | "
        "gke-test.us-central1.gke.goog:443 | latest failed tunnel 503 to "
        "gke-test.us-central1.gke.goog:443 |"
    ) in markdown_text
    assert "1 successful Envoy proxy sample" in markdown_text


def test_reporting_mesh_api_proxy_absent_state_is_explicit(tmp_path: Path) -> None:
    report = Report(
        environment="test",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="mesh",
                status=Status.OK,
                summary="ok",
                details={
                    "clusters": [
                        {
                            "cluster": "example-test-apps-gke",
                            "status": "ok",
                            "ingress_gateway": {},
                            "east_west_gateway": {},
                            "istiod": {},
                            "remote_secret_count": 0,
                        }
                    ]
                },
            )
        ],
    )

    target = ensure_report_directory(tmp_path, "test", 2026, 20)
    markdown_path = write_report_markdown(report, target)

    markdown_text = markdown_path.read_text(encoding="utf-8")
    assert "- No mesh API proxy checks configured for this environment." in markdown_text
    assert "| example-test-apps-gke |  | n/a |  |  | 0/0 |" not in markdown_text


def test_reporting_legacy_squid_proxy_rows_keep_proxy_type(tmp_path: Path) -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="mesh",
                status=Status.OK,
                summary="ok",
                details={
                    "clusters": [
                        {
                            "cluster": "dev-cluster",
                            "status": "ok",
                            "ingress_gateway": {},
                            "east_west_gateway": {},
                            "istiod": {},
                            "remote_secret_count": 0,
                            "squid_proxies": [
                                {
                                    "name": "squid-proxy",
                                    "namespace": "istio-proxy",
                                    "status": "ok",
                                    "desired_replicas": 1,
                                    "ready_replicas": 1,
                                    "pod_total": 1,
                                    "pod_ready": 1,
                                    "service_type": "LoadBalancer",
                                    "service_ports": ["3128"],
                                    "endpoint_ports": ["3128"],
                                    "endpoint_address_count": 1,
                                    "load_balancer_ingress": ["10.115.2.43"],
                                    "note": "",
                                }
                            ],
                        }
                    ]
                },
            )
        ],
    )

    target = ensure_report_directory(tmp_path, "dev", 2026, 20)
    markdown_path = write_report_markdown(report, target)

    markdown_text = markdown_path.read_text(encoding="utf-8")
    assert "| dev-cluster | squid-proxy | squid | istio-proxy | No Findings |" in markdown_text


def test_html_and_pdf_ignore_stale_markdown_artifact(tmp_path: Path) -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[CheckResult(collector="preflight", status=Status.OK, summary="fresh ok")],
    )
    brand = BrandProfile(report_title="Fresh Runtime Report")
    target = ensure_report_directory(tmp_path, "dev", 2026, 20)
    stale_markdown = target / "opsbrief-dev-weekly-report.md"
    stale_markdown.write_text(
        "# Stale File Title\n\n## Evidence Index\n\n- stale evidence",
        encoding="utf-8",
    )

    html_path = write_report_html(report, target, brand)
    pdf_path = write_report_pdf(report, target, brand)

    html_text = html_path.read_text(encoding="utf-8")
    assert "Fresh Runtime Report (dev)" in html_text
    assert "Stale File Title" not in html_text
    assert "stale evidence" not in html_text
    assert pdf_path.read_bytes().startswith(b"%PDF")


def test_write_preflight_evidence(tmp_path: Path) -> None:
    result = CheckResult(
        collector="preflight",
        status=Status.OK,
        summary="ok",
        details={"checks": []},
    )
    evidence_path = write_preflight_evidence(
        result=result,
        output_dir=tmp_path,
        environment="dev",
    )

    assert evidence_path.exists()
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert payload["collector"] == "preflight"
    assert payload["status"] == "ok"


def test_reporting_applies_brand_profile(tmp_path: Path) -> None:
    logo_path = tmp_path / "logo.png"
    logo_path.write_bytes(_tiny_png())
    font_path = tmp_path / "brand-font.ttf"
    font_path.write_bytes(b"font")
    report = Report(
        environment="prod",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[CheckResult(collector="preflight", status=Status.OK, summary="ok")],
    )
    brand = BrandProfile(
        organization_name="Example Org",
        report_title="Example Org Operations Report",
        logo_path=str(logo_path),
        logo_alt="Example logo",
        logo_width_mm=30,
        font_family="Arial, sans-serif",
        font_faces=(
            BrandFontFace(
                family="Arial",
                path=str(font_path),
                weight="700",
                style="normal",
            ),
        ),
        colors=BrandColors(
            primary="#005f73",
            secondary="#0a9396",
            accent="#ca6702",
            background="#eef6f6",
            header_background="#d8eeee",
        ),
        report_theme=BrandReportTheme(
            theme_lines=BrandThemeLines(enabled=True, count=3, color="#ca6702"),
            corner_motif=BrandCornerMotif(
                enabled=True,
                fill="#005f73",
                border="#ca6702",
            ),
            status_colors=BrandStatusColors(
                ok="#005f73",
                warning="#8A6B00",
                critical="#9F2A2A",
                scoped="#475569",
            ),
        ),
    )

    target = ensure_report_directory(tmp_path, "prod", 2026, 20)
    markdown_path = write_report_markdown(report, target, brand)
    html_path = write_report_html(report, target, brand)
    pdf_path = write_report_pdf(report, target, brand)

    markdown_text = markdown_path.read_text(encoding="utf-8")
    html_text = html_path.read_text(encoding="utf-8")

    assert "# Example Org Operations Report (prod)" in markdown_text
    assert "- Organization: Example Org" in markdown_text
    assert 'class="brand-banner brand-corner-motif"' in html_text
    assert 'class="brand-logo"' in html_text
    assert 'class="report-toc"' in html_text
    assert 'class="report-hero"' in html_text
    assert 'class="brand-theme-lines"' in html_text
    assert "--theme-line-color: #ca6702;" in html_text
    assert "--corner-motif-fill: #005f73;" in html_text
    assert "--status-warning: #8A6B00;" in html_text
    assert "linear-gradient(135deg" not in html_text
    assert "background: transparent;" in html_text
    assert ".status-tag {" in html_text
    assert ".status-label" not in html_text
    assert ".status-badge" not in html_text
    assert "data:image/png;base64," in html_text
    assert "<title>Example Org Operations Report (prod)</title>" in html_text
    assert "--primary: #005f73;" in html_text
    assert "--font-family: Arial, sans-serif;" in html_text
    assert "@font-face" in html_text
    assert 'font-family: "Arial";' in html_text
    assert "data:font/ttf;base64," in html_text
    assert "@page" in html_text
    assert "size: A4 landscape;" in html_text
    assert 'content: "Page " counter(page) " of " counter(pages);' in html_text
    assert pdf_path.read_bytes().startswith(b"%PDF")


def test_default_brand_profile_does_not_render_optional_brand_motifs() -> None:
    report = Report(
        environment="prod",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[CheckResult(collector="preflight", status=Status.OK, summary="ok")],
    )

    html_text = render_report_html(report, "# Example", BrandProfile())

    assert '<header class="brand-banner">' in html_text
    assert '<span class="brand-theme-lines"' not in html_text
    assert '<header class="brand-banner brand-corner-motif">' not in html_text


def test_status_badges_do_not_split_section_headings() -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[CheckResult(collector="preflight", status=Status.OK, summary="ok")],
    )
    markdown_text = "\n".join(
        [
            "# Example",
            "",
            "### Not Assessed Areas",
            "",
            "| Assessment Result | Meaning |",
            "| --- | --- |",
            "| Not Assessed | Out of scope. |",
            "",
            "### Backup Discovery (No Findings)",
        ]
    )

    html_text = render_report_html(report, markdown_text, BrandProfile())

    assert '<h3 id="not-assessed-areas">Not Assessed Areas</h3>' in html_text
    assert '<h3 id="backup-discovery">Backup Discovery (No Findings)</h3>' in html_text
    assert '<td><strong class="status-tag status-scoped">Not Assessed</strong></td>' in html_text


def test_print_css_major_sections_start_new_pages() -> None:
    assert "technical-evidence-appendix" in PRINT_SECTION_BREAK_IDS
    assert "runtime-and-control-plane-health" not in PRINT_SECTION_BREAK_IDS
    assert "monitoring-and-alerting" in PRINT_SECTION_BREAK_IDS
    assert "backup-and-recovery-posture" in PRINT_SECTION_BREAK_IDS
    assert "monitoring" not in PRINT_SECTION_BREAK_IDS


def _tiny_png() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00"
        b"\x00\x00\x0cIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02"
        b"\xfeA\xe2!\xbc\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def test_reporting_bolds_high_utilization_without_chart_images(tmp_path: Path) -> None:
    report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="kubernetes_health",
                status=Status.OK,
                summary="ok",
                details={
                    "clusters": [
                        {
                            "cluster": "dev-cluster",
                            "node_inventory": [
                                {
                                    "name": "node-1",
                                    "ready": True,
                                    "status": "Ready",
                                    "created_at": "2026-05-14T00:00:00+00:00",
                                }
                            ],
                            "namespace_utilization": [
                                {
                                    "namespace": "core",
                                    "pod_count": 10,
                                    "cpu_millicores": 1800,
                                    "memory_bytes": 2147483648,
                                }
                            ],
                        }
                    ]
                },
            ),
            CheckResult(
                collector="trend_metrics",
                status=Status.OK,
                summary="ok",
                details={
                    "window_days": 7,
                    "projects": [
                        {
                            "project": "example-dev-project",
                            "cloud_sql": [
                                {
                                    "instance": "example-dev-sql",
                                    "cpu_peak_percent": 42.5,
                                    "memory_peak_percent": 91.0,
                                    "disk_peak_percent": 88.0,
                                }
                            ],
                            "non_gke_vm_cpu": [
                                {
                                    "instance": "example-dev-linux-vm-01",
                                    "cpu_min_percent": 2.0,
                                    "cpu_peak_percent": 28.4,
                                    "idle_capacity_at_peak_percent": 71.6,
                                    "activity_state": "active",
                                },
                                {
                                    "instance": "example-dev-linux-vm-02",
                                    "telemetry_status": "ok",
                                    "cpu_min_percent": 0.1,
                                    "cpu_peak_percent": 0.2,
                                    "idle_capacity_at_peak_percent": 99.8,
                                    "activity_state": "idle",
                                },
                            ],
                            "non_gke_vm_memory": [
                                {
                                    "instance": "example-dev-linux-vm-01",
                                    "telemetry_status": "ok",
                                    "memory_avg_percent": 41.0,
                                    "memory_p95_percent": 66.0,
                                    "memory_peak_percent": 73.5,
                                    "note": "",
                                },
                                {
                                    "instance": "example-dev-linux-vm-02",
                                    "telemetry_status": "missing",
                                    "memory_avg_percent": 0.0,
                                    "memory_p95_percent": 0.0,
                                    "memory_peak_percent": 0.0,
                                    "note": "Ops Agent memory metric not found for this VM.",
                                },
                            ],
                            "gke_cluster_utilization": [
                                {
                                    "cluster": "dev-cluster",
                                    "cpu_peak_cores": 2.25,
                                    "memory_peak_bytes": 3221225472.0,
                                    "max_node_cpu_allocatable_peak_percent": 61.0,
                                    "max_node_cpu_idle_capacity_at_peak_percent": 39.0,
                                    "max_node_memory_allocatable_peak_percent": 72.0,
                                    "pod_series_count": 12,
                                    "node_series_count": 3,
                                    "activity_state": "active",
                                }
                            ],
                            "gke_node_utilization": [
                                {
                                    "cluster": "dev-cluster",
                                    "node": "node-1",
                                    "cpu_allocatable_peak_percent": 61.0,
                                    "memory_allocatable_peak_percent": 72.0,
                                },
                                {
                                    "cluster": "dev-cluster",
                                    "node": "node-old",
                                    "cpu_allocatable_peak_percent": 40.0,
                                    "memory_allocatable_peak_percent": 35.0,
                                },
                            ],
                            "gke_namespace_utilization": [
                                {
                                    "cluster": "dev-cluster",
                                    "namespace": "apps",
                                    "cpu_avg_cores": 0.25,
                                    "cpu_p95_cores": 0.55,
                                    "cpu_peak_cores": 0.80,
                                    "memory_avg_bytes": 268435456.0,
                                    "memory_p95_bytes": 402653184.0,
                                    "memory_peak_bytes": 536870912.0,
                                }
                            ],
                            "gke_pod_utilization": [
                                {
                                    "cluster": "dev-cluster",
                                    "namespace": "apps",
                                    "pod": "api-123",
                                    "cpu_peak_cores": 0.75,
                                    "memory_peak_bytes": 536870912.0,
                                },
                                {
                                    "cluster": "dev-cluster",
                                    "namespace": "apps",
                                    "pod": "cpu-only",
                                    "cpu_peak_cores": 0.25,
                                    "memory_peak_bytes": 0.0,
                                },
                            ],
                            "redis_throughput": [
                                {
                                    "instance": "example-dev-redis",
                                    "direction": "in",
                                    "bytes_per_second_avg": 1048576.0,
                                    "bytes_per_second_peak": 2097152.0,
                                }
                            ],
                            "kafka_throughput": [
                                {
                                    "cluster": "example-dev-kafka",
                                    "bytes_in_avg": 524288.0,
                                    "bytes_out_avg": 262144.0,
                                    "messages_in_avg": 30.0,
                                    "bytes_in_peak": 1048576.0,
                                    "bytes_out_peak": 524288.0,
                                    "messages_in_peak": 60.0,
                                }
                            ],
                        }
                    ],
                },
            ),
        ],
    )

    target = ensure_report_directory(tmp_path, "dev", 2026, 20)
    legacy_chart_dir = target / "evidence" / "charts"
    legacy_chart_dir.mkdir(parents=True)
    (legacy_chart_dir / "old-chart.png").write_bytes(b"old")

    markdown_path = write_report_markdown(report, target)
    html_path = write_report_html(report, target)
    pdf_path = write_report_pdf(report, target)

    markdown_text = markdown_path.read_text(encoding="utf-8")
    assert not legacy_chart_dir.exists()
    assert "evidence/charts/" not in markdown_text
    assert "Namespace CPU (cores): Top Namespaces" not in markdown_text
    assert "Cloud SQL CPU Peak (%): Top Instances" not in markdown_text
    assert "**1.80**" in markdown_text
    assert "**2.00**" in markdown_text
    assert "**91.0%**" in markdown_text
    assert "**88.0%**" in markdown_text
    assert "**42.5%**" not in markdown_text
    assert "Infrastructure CPU Utilization Summary (7-Day):" in markdown_text
    assert "Infrastructure CPU Activity Summary" not in markdown_text
    assert "CPU Usage And Spare Capacity Summary:" in markdown_text
    assert "Spare CPU Capacity At Peak is derived as 100% minus" in markdown_text
    assert "not separately collected CPU idle-time telemetry" in markdown_text
    assert "Unknown CPU" not in markdown_text
    assert "CPU Activity Classification" not in markdown_text
    assert "Active Resources" not in markdown_text
    assert "Low-Activity Resources" not in markdown_text
    assert "All resource types" not in markdown_text
    assert "| example-dev-project | Compute VM | 2 | 2 | 0 | 28.4% | 71.6% |" in markdown_text
    assert "| example-dev-project | GKE cluster | 1 | 1 | 0 | 61.0% | 39.0% |" in markdown_text
    assert (
        "| example-dev-project | GKE cluster | dev-cluster | "
        "Highest node CPU allocatable utilization | 61.0% | 39.0% | Yes |  |"
    ) in markdown_text
    assert "| example-dev-project | Compute VM | example-dev-linux-vm-02 |" in markdown_text
    assert "Low Activity" not in markdown_text
    assert (
        "| example-dev-project | Compute VM | example-dev-linux-vm-01 | "
        "Compute Engine CPU utilization | 28.4% | 71.6% | Yes |  |"
    ) in markdown_text
    assert (
        "| example-dev-project | Compute VM | example-dev-linux-vm-02 | "
        "Compute Engine CPU utilization | 0.2% | 99.8% | Yes |  |"
    ) in markdown_text
    assert "Non-GKE VM Memory Peaks (Ops Agent):" not in markdown_text
    assert "VM CPU & Memory Utilization Summary (7-Day):" not in markdown_text
    assert "VM Telemetry Source And Memory Summary (7-Day):" in markdown_text
    assert (
        "| Project | Instance | CPU Source | Highest Memory Observed (%) | Note |" in markdown_text
    )
    assert "| Project | Instance | CPU Source | Highest CPU Observed (%) |" not in markdown_text
    assert "example-dev-linux-vm-02" in markdown_text
    assert "Ops Agent memory metric not found for this VM." in markdown_text
    assert "GKE Workload Usage Summary (7-Day):" in markdown_text
    assert "Workload CPU is summed container CPU usage for the cluster" in markdown_text
    assert "This is workload demand, not a cluster saturation percentage." in markdown_text
    assert "Node saturation is reported separately" in markdown_text
    assert (
        "| Project | Cluster | Highest Workload CPU Observed (cores) | "
        "Highest Workload Memory Observed (GiB) |"
    ) in markdown_text
    assert "Pods Included in 7-Day Trend" not in markdown_text
    assert "Highest Single Node CPU Observed (%)" not in markdown_text
    assert (
        "GKE Node Highest Observed Allocatable Utilization (7-Day Cloud Monitoring):"
        in markdown_text
    )
    assert "Historical means the node was seen in the trend window" in markdown_text
    assert (
        "| example-dev-project | dev-cluster | **node-1** | **Active** | **yes** |" in markdown_text
    )
    assert "| example-dev-project | dev-cluster | node-old | Historical | n/a |" in markdown_text
    assert "GKE Namespace 7-Day Usage Trends (7-Day Cloud Monitoring):" in markdown_text
    assert "This is namespace-level, not per-pod." in markdown_text
    assert "| example-dev-project | dev-cluster | apps | 0.25 | 0.55 | 0.80 |" in markdown_text
    assert "GKE Pod Highest Observed Usage (7-Day Cloud Monitoring):" in markdown_text
    assert "Pod CPU `Highest Observed` is the highest point-in-time CPU rate" in markdown_text
    assert "pod memory `Highest Observed` is the highest memory value returned" in markdown_text
    assert "| example-dev-project | dev-cluster | apps | cpu-only | 0.25 | n/a |" in markdown_text
    assert "api-123" in markdown_text

    html_text = html_path.read_text(encoding="utf-8")
    assert "<img" not in html_text
    assert "<strong>91.0%</strong>" in html_text
    assert "<strong>88.0%</strong>" in html_text
    assert pdf_path.read_bytes().startswith(b"%PDF")
