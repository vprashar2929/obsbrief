from pathlib import Path

import pytest

from opsbrief.config import load_config, load_manual_input


def test_load_config_reads_clusters(tmp_path: Path) -> None:
    config_path = tmp_path / "dev.yaml"
    config_path.write_text(
        "\n".join(
            [
                "environment: dev",
                "default_region: us-central1",
                "timeout_seconds: 120",
                "projects:",
                "  component: example-dev-project",
                "collectors:",
                "  preflight: true",
                "  prometheus_monitoring: true",
                "services:",
                "  mesh_api_proxies:",
                "    - name: istio-proxy",
                "      type: nginx",
                "      namespace: istio-proxy",
                "      service_port: 443",
                "      endpoint_port: 8443",
                "clusters:",
                "  - name: dev-cluster",
                "    project: example-dev-project",
                "    region: us-central1",
                "time_windows:",
                "  trend_days: 14",
                "reporting:",
                "  include_evidence_index: true",
                "  report_mode: concise",
                "  include_technical_appendix: true",
                "  include_collector_warnings_and_gaps: true",
                "report_expectations:",
                "  backup_policy:",
                "    status: out_of_scope",
                "    reason: Backup policy is out of scope for dev.",
                "  autoscaling:",
                "    workload_hpa:",
                "      status: informational",
                "    platform_hpa:",
                "      status: assessed",
                "    node_pool_autoscaling:",
                "      status: assessed",
                "    platform_namespaces:",
                "      - istio-system",
                "      - kube-system",
                "prometheus:",
                "  url: https://prometheus.example",
                "  token_env: PROMETHEUS_TOKEN",
                "  labels:",
                "    environment: dev",
                "    cluster:",
                "      - dev-cluster",
            ]
        ),
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert config.environment == "dev"
    assert config.timeout_seconds == 120
    assert len(config.clusters) == 1
    assert (
        config.clusters[0].resolved_context() == "gke_example-dev-project_us-central1_dev-cluster"
    )
    assert config.time_windows.trend_days == 14
    assert config.reporting.include_evidence_index is True
    assert config.reporting.report_mode == "concise"
    assert config.reporting.include_technical_appendix is True
    assert config.reporting.include_collector_warnings_and_gaps is True
    assert config.report_expectations.backup_policy.status == "out_of_scope"
    assert config.report_expectations.backup_policy.reason == (
        "Backup policy is out of scope for dev."
    )
    assert config.report_expectations.autoscaling.workload_hpa.status == "informational"
    assert config.report_expectations.autoscaling.platform_hpa.status == "assessed"
    assert config.report_expectations.autoscaling.node_pool_autoscaling.status == "assessed"
    assert config.report_expectations.autoscaling.platform_namespaces == [
        "istio-system",
        "kube-system",
    ]
    assert config.prometheus.url == "https://prometheus.example"
    assert config.prometheus.token_env == "PROMETHEUS_TOKEN"
    assert config.prometheus.labels == {
        "environment": ["dev"],
        "cluster": ["dev-cluster"],
    }
    assert config.services.mesh_api_proxies == [
        {
            "name": "istio-proxy",
            "type": "nginx",
            "namespace": "istio-proxy",
            "service_port": 443,
            "endpoint_port": 8443,
        }
    ]


def test_load_config_rejects_invalid_nested_runtime_types(tmp_path: Path) -> None:
    config_path = tmp_path / "bad.yaml"
    config_path.write_text(
        "\n".join(
            [
                "environment: dev",
                "default_region: us-central1",
                "collectors:",
                "  preflight: yes",
                "time_windows:",
                "  trend_days: fourteen",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="trend_days"):
        load_config(config_path)


def test_load_config_rejects_invalid_timeout_seconds(tmp_path: Path) -> None:
    config_path = tmp_path / "bad-timeout.yaml"
    config_path.write_text(
        "\n".join(
            [
                "environment: dev",
                "default_region: us-central1",
                "collectors: {}",
                "timeout_seconds: 0",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="timeout_seconds"):
        load_config(config_path)


def test_load_config_rejects_invalid_report_mode(tmp_path: Path) -> None:
    config_path = tmp_path / "bad-reporting.yaml"
    config_path.write_text(
        "\n".join(
            [
                "environment: dev",
                "default_region: us-central1",
                "collectors: {}",
                "reporting:",
                "  report_mode: executive",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="report_mode"):
        load_config(config_path)


def test_load_config_rejects_invalid_report_expectation_status(tmp_path: Path) -> None:
    config_path = tmp_path / "bad-expectation.yaml"
    config_path.write_text(
        "\n".join(
            [
                "environment: dev",
                "default_region: us-central1",
                "collectors: {}",
                "report_expectations:",
                "  backup_policy:",
                "    status: required",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="out_of_scope"):
        load_config(config_path)


def test_load_config_rejects_invalid_prometheus_label(tmp_path: Path) -> None:
    config_path = tmp_path / "bad-prometheus.yaml"
    config_path.write_text(
        "\n".join(
            [
                "environment: dev",
                "default_region: us-central1",
                "collectors: {}",
                "prometheus:",
                "  url: https://prometheus.example",
                "  labels:",
                "    bad-label: dev",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Prometheus label-name"):
        load_config(config_path)


def test_load_config_rejects_empty_prometheus_label_value(tmp_path: Path) -> None:
    config_path = tmp_path / "empty-prometheus-label.yaml"
    config_path.write_text(
        "\n".join(
            [
                "environment: dev",
                "default_region: us-central1",
                "collectors: {}",
                "prometheus:",
                "  url: https://prometheus.example",
                "  labels:",
                '    environment: ""',
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="non-empty string"):
        load_config(config_path)


def test_load_manual_input_missing_file_returns_warning(tmp_path: Path) -> None:
    payload = load_manual_input(tmp_path / "missing.yaml")
    assert "_warning" in payload


def test_load_manual_input_normalizes_known_and_unknown_keys(tmp_path: Path) -> None:
    manual = tmp_path / "manual.yaml"
    manual.write_text(
        "\n".join(
            [
                "open_risks:",
                "  - risk-a",
                "incident_resolutions: resolved item",
                "unknown_field: value",
            ]
        ),
        encoding="utf-8",
    )
    payload = load_manual_input(manual)
    assert payload["incident_resolutions"] == "resolved item"
    assert payload["unknown_field"] == "value"
    assert "_warning_unknown_keys" in payload
