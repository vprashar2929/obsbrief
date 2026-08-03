from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
)

MANUAL_INPUT_KEYS = {
    "open_risks",
    "recurring_issues",
    "servicenow_summary",
    "planned_changes",
    "incident_resolutions",
    "pii_logging_notes",
    "actions_completed",
    "pending_blockers",
}


def _default_platform_namespaces() -> list[str]:
    return [
        "gmp-system",
        "istio-system",
        "kube-system",
        "monitoring",
        "nginx-system",
        "prometheus",
    ]


@dataclass(slots=True)
class ClusterConfig:
    name: str
    project: str
    region: str
    context: str = ""

    def resolved_context(self) -> str:
        if self.context:
            return self.context
        return f"gke_{self.project}_{self.region}_{self.name}"


@dataclass(slots=True)
class ServicesConfig:
    cloud_sql: bool = True
    redis: bool = False
    managed_kafka: bool = False
    utilization_thresholds: dict[str, float] = field(default_factory=dict)
    elasticsearch_backup_checks: list[dict[str, Any]] = field(default_factory=list)
    mesh_api_proxies: list[dict[str, Any]] = field(default_factory=list)
    squid_proxies: list[dict[str, Any]] = field(default_factory=list)
    compute_instances: list[dict[str, Any]] = field(default_factory=list)
    load_balancers: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class NetworkConfig:
    required_service_fqdns: list[str] = field(default_factory=list)
    required_internal_zones: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DiscoveryConfig:
    auto_discover_clusters: bool = True
    include_discovered_clusters: bool = False
    auto_discover_compute_instances: bool = True
    auto_discover_load_balancers: bool = True


@dataclass(slots=True)
class TimeWindowsConfig:
    trend_days: int = 7


@dataclass(slots=True)
class ReportingConfig:
    include_evidence_index: bool = False
    report_mode: str = "full"
    include_technical_appendix: bool = False
    include_collector_warnings_and_gaps: bool = False


@dataclass(slots=True)
class PolicyExpectation:
    status: str = "assessed"
    reason: str = ""


@dataclass(slots=True)
class AutoscalingExpectations:
    workload_hpa: PolicyExpectation = field(default_factory=PolicyExpectation)
    platform_hpa: PolicyExpectation = field(default_factory=PolicyExpectation)
    node_pool_autoscaling: PolicyExpectation = field(default_factory=PolicyExpectation)
    platform_namespaces: list[str] = field(default_factory=_default_platform_namespaces)


@dataclass(slots=True)
class ReportExpectationsConfig:
    backup_policy: PolicyExpectation = field(default_factory=PolicyExpectation)
    autoscaling: AutoscalingExpectations = field(default_factory=AutoscalingExpectations)


@dataclass(slots=True)
class PrometheusConfig:
    url: str = ""
    token_env: str = ""
    labels: dict[str, list[str]] = field(default_factory=dict)


@dataclass(slots=True)
class EnvConfig:
    environment: str
    default_region: str
    projects: dict[str, str]
    collectors: dict[str, bool]
    timeout_seconds: int = 45
    clusters: list[ClusterConfig] = field(default_factory=list)
    services: ServicesConfig = field(default_factory=ServicesConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    time_windows: TimeWindowsConfig = field(default_factory=TimeWindowsConfig)
    reporting: ReportingConfig = field(default_factory=ReportingConfig)
    report_expectations: ReportExpectationsConfig = field(default_factory=ReportExpectationsConfig)
    prometheus: PrometheusConfig = field(default_factory=PrometheusConfig)


class _ConfigSchema(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _ClusterSchema(_ConfigSchema):
    name: StrictStr
    project: StrictStr
    region: StrictStr | None = None
    context: StrictStr = ""

    @field_validator("context", mode="before")
    @classmethod
    def _default_context(cls, value: Any) -> Any:
        return "" if value is None else value


class _ServicesSchema(_ConfigSchema):
    cloud_sql: StrictBool = True
    redis: StrictBool = False
    managed_kafka: StrictBool = False
    utilization_thresholds: dict[str, float] = Field(default_factory=dict)
    elasticsearch_backup_checks: list[dict[str, Any]] = Field(default_factory=list)
    mesh_api_proxies: list[dict[str, Any]] = Field(default_factory=list)
    squid_proxies: list[dict[str, Any]] = Field(default_factory=list)
    compute_instances: list[dict[str, Any]] = Field(default_factory=list)
    load_balancers: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("utilization_thresholds", mode="before")
    @classmethod
    def _validate_thresholds(cls, value: Any) -> dict[str, float]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("Expected dict for services:utilization_thresholds")
        out: dict[str, float] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("Expected string keys for services:utilization_thresholds")
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise ValueError(
                    f"Expected numeric value for services:utilization_thresholds:{key}"
                )
            out[key] = float(item)
        return out

    @field_validator(
        "elasticsearch_backup_checks",
        "mesh_api_proxies",
        "squid_proxies",
        "compute_instances",
        "load_balancers",
        mode="before",
    )
    @classmethod
    def _validate_dict_rows(cls, value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("Expected list of objects")
        rows: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                raise ValueError("Expected list of objects")
            rows.append(item)
        return rows


class _NetworkSchema(_ConfigSchema):
    required_service_fqdns: list[str] = Field(default_factory=list)
    required_internal_zones: list[str] = Field(default_factory=list)

    @field_validator("required_service_fqdns", "required_internal_zones", mode="before")
    @classmethod
    def _validate_string_list(cls, value: Any) -> list[str]:
        return _validate_str_list(value, default=[])


class _DiscoverySchema(_ConfigSchema):
    auto_discover_clusters: StrictBool = True
    include_discovered_clusters: StrictBool = False
    auto_discover_compute_instances: StrictBool = True
    auto_discover_load_balancers: StrictBool = True


class _TimeWindowsSchema(_ConfigSchema):
    trend_days: StrictInt = 7


class _ReportingSchema(_ConfigSchema):
    include_evidence_index: StrictBool = False
    report_mode: StrictStr = "full"
    include_technical_appendix: StrictBool = False
    include_collector_warnings_and_gaps: StrictBool = False

    @field_validator("report_mode", mode="before")
    @classmethod
    def _default_report_mode(cls, value: Any) -> Any:
        return "full" if value is None else value

    @field_validator("report_mode")
    @classmethod
    def _normalize_report_mode(cls, value: str) -> str:
        normalized = value.strip().lower() or "full"
        if normalized not in ("full", "concise"):
            raise ValueError(
                f"Expected one of full, concise for reporting:report_mode, got {value}"
            )
        return normalized


class _PolicyExpectationSchema(_ConfigSchema):
    status: StrictStr = "assessed"
    reason: StrictStr = ""

    @field_validator("status", mode="before")
    @classmethod
    def _default_status(cls, value: Any) -> Any:
        return "assessed" if value is None else value

    @field_validator("reason", mode="before")
    @classmethod
    def _default_reason(cls, value: Any) -> Any:
        return "" if value is None else value

    @field_validator("status")
    @classmethod
    def _normalize_status(cls, value: str) -> str:
        normalized = value.strip().lower() or "assessed"
        if normalized not in ("assessed", "informational", "out_of_scope"):
            raise ValueError(
                "Expected one of assessed, informational, out_of_scope for policy status"
            )
        return normalized


class _AutoscalingExpectationsSchema(_ConfigSchema):
    workload_hpa: _PolicyExpectationSchema = Field(default_factory=_PolicyExpectationSchema)
    platform_hpa: _PolicyExpectationSchema = Field(default_factory=_PolicyExpectationSchema)
    node_pool_autoscaling: _PolicyExpectationSchema = Field(
        default_factory=_PolicyExpectationSchema
    )
    platform_namespaces: list[str] = Field(default_factory=_default_platform_namespaces)

    @field_validator("workload_hpa", "platform_hpa", "node_pool_autoscaling", mode="before")
    @classmethod
    def _default_policy(cls, value: Any) -> Any:
        return {} if value is None else value

    @field_validator("platform_namespaces", mode="before")
    @classmethod
    def _validate_platform_namespaces(cls, value: Any) -> list[str]:
        return _validate_str_list(value, default=_default_platform_namespaces())


class _ReportExpectationsSchema(_ConfigSchema):
    backup_policy: _PolicyExpectationSchema = Field(default_factory=_PolicyExpectationSchema)
    autoscaling: _AutoscalingExpectationsSchema = Field(
        default_factory=_AutoscalingExpectationsSchema
    )

    @field_validator("backup_policy", "autoscaling", mode="before")
    @classmethod
    def _default_sections(cls, value: Any) -> Any:
        return {} if value is None else value


class _PrometheusSchema(_ConfigSchema):
    url: StrictStr = ""
    token_env: StrictStr = ""
    labels: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("url", "token_env", mode="before")
    @classmethod
    def _default_string(cls, value: Any) -> Any:
        return "" if value is None else value

    @field_validator("labels", mode="before")
    @classmethod
    def _validate_labels(cls, value: Any) -> dict[str, list[str]]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("Expected dict for prometheus:labels")
        out: dict[str, list[str]] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not _is_prometheus_label_name(key):
                raise ValueError("Expected Prometheus label-name keys for prometheus:labels")
            if isinstance(item, str):
                if not item:
                    raise ValueError(f"Expected non-empty string for prometheus:labels:{key}")
                values = [item]
            elif isinstance(item, list):
                if not item:
                    raise ValueError(f"Expected at least one value for prometheus:labels:{key}")
                values = []
                for index, label_value in enumerate(item):
                    if not isinstance(label_value, str) or not label_value:
                        raise ValueError(
                            f"Expected non-empty string for prometheus:labels:{key}[{index}]"
                        )
                    values.append(label_value)
            else:
                raise ValueError(f"Expected string or list for prometheus:labels:{key}")
            out[key] = _dedupe_strings(values)
        return out


class _EnvConfigSchema(_ConfigSchema):
    environment: StrictStr
    default_region: StrictStr
    projects: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    collectors: dict[StrictStr, StrictBool] = Field(default_factory=dict)
    timeout_seconds: StrictInt = 45
    clusters: list[_ClusterSchema] = Field(default_factory=list)
    services: _ServicesSchema = Field(default_factory=_ServicesSchema)
    network: _NetworkSchema = Field(default_factory=_NetworkSchema)
    discovery: _DiscoverySchema = Field(default_factory=_DiscoverySchema)
    time_windows: _TimeWindowsSchema = Field(default_factory=_TimeWindowsSchema)
    reporting: _ReportingSchema = Field(default_factory=_ReportingSchema)
    report_expectations: _ReportExpectationsSchema = Field(
        default_factory=_ReportExpectationsSchema
    )
    prometheus: _PrometheusSchema = Field(default_factory=_PrometheusSchema)

    @field_validator("projects", "collectors", mode="before")
    @classmethod
    def _default_dict(cls, value: Any) -> Any:
        return {} if value is None else value

    @field_validator("clusters", mode="before")
    @classmethod
    def _default_clusters(cls, value: Any) -> Any:
        return [] if value is None else value

    @field_validator("timeout_seconds")
    @classmethod
    def _validate_timeout_seconds(cls, value: int) -> int:
        if value < 1:
            raise ValueError("Expected timeout_seconds to be at least 1")
        return value

    @field_validator(
        "services",
        "network",
        "discovery",
        "time_windows",
        "reporting",
        "report_expectations",
        "prometheus",
        mode="before",
    )
    @classmethod
    def _default_section(cls, value: Any) -> Any:
        return {} if value is None else value


def load_config(path: str | Path, env_override: str | None = None) -> EnvConfig:
    config_path = Path(path)
    raw = _load_yaml(config_path)
    schema = _EnvConfigSchema.model_validate(raw)
    env = schema.environment
    if env_override:
        env = env_override

    return EnvConfig(
        environment=env,
        default_region=schema.default_region,
        projects=dict(schema.projects),
        collectors=dict(schema.collectors),
        timeout_seconds=schema.timeout_seconds,
        clusters=[
            ClusterConfig(
                name=cluster.name,
                project=cluster.project,
                region=cluster.region or schema.default_region,
                context=cluster.context,
            )
            for cluster in schema.clusters
        ],
        services=ServicesConfig(
            cloud_sql=schema.services.cloud_sql,
            redis=schema.services.redis,
            managed_kafka=schema.services.managed_kafka,
            utilization_thresholds=dict(schema.services.utilization_thresholds),
            elasticsearch_backup_checks=list(schema.services.elasticsearch_backup_checks),
            mesh_api_proxies=list(schema.services.mesh_api_proxies),
            squid_proxies=list(schema.services.squid_proxies),
            compute_instances=list(schema.services.compute_instances),
            load_balancers=list(schema.services.load_balancers),
        ),
        network=NetworkConfig(
            required_service_fqdns=list(schema.network.required_service_fqdns),
            required_internal_zones=list(schema.network.required_internal_zones),
        ),
        discovery=DiscoveryConfig(
            auto_discover_clusters=schema.discovery.auto_discover_clusters,
            include_discovered_clusters=schema.discovery.include_discovered_clusters,
            auto_discover_compute_instances=schema.discovery.auto_discover_compute_instances,
            auto_discover_load_balancers=schema.discovery.auto_discover_load_balancers,
        ),
        time_windows=TimeWindowsConfig(trend_days=schema.time_windows.trend_days),
        reporting=ReportingConfig(
            include_evidence_index=schema.reporting.include_evidence_index,
            report_mode=schema.reporting.report_mode,
            include_technical_appendix=schema.reporting.include_technical_appendix,
            include_collector_warnings_and_gaps=(
                schema.reporting.include_collector_warnings_and_gaps
            ),
        ),
        report_expectations=ReportExpectationsConfig(
            backup_policy=_to_policy_expectation(schema.report_expectations.backup_policy),
            autoscaling=AutoscalingExpectations(
                workload_hpa=_to_policy_expectation(
                    schema.report_expectations.autoscaling.workload_hpa
                ),
                platform_hpa=_to_policy_expectation(
                    schema.report_expectations.autoscaling.platform_hpa
                ),
                node_pool_autoscaling=_to_policy_expectation(
                    schema.report_expectations.autoscaling.node_pool_autoscaling
                ),
                platform_namespaces=list(
                    schema.report_expectations.autoscaling.platform_namespaces
                ),
            ),
        ),
        prometheus=PrometheusConfig(
            url=schema.prometheus.url,
            token_env=schema.prometheus.token_env,
            labels={key: list(value) for key, value in schema.prometheus.labels.items()},
        ),
    )


def _to_policy_expectation(schema: _PolicyExpectationSchema) -> PolicyExpectation:
    return PolicyExpectation(status=schema.status, reason=schema.reason)


def _validate_str_list(value: Any, *, default: list[str]) -> list[str]:
    if value is None:
        return list(default)
    if not isinstance(value, list):
        raise ValueError("Expected list")
    out: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"Expected non-empty string at index {index}")
        rendered = item.strip()
        if rendered not in out:
            out.append(rendered)
    return out


def load_manual_input(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    manual_path = Path(path)
    if not manual_path.exists():
        return {"_warning": f"manual input not found: {manual_path}"}
    raw = _load_yaml(manual_path)
    return _normalize_manual_input(raw)


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected dict YAML at {path}, got {type(data).__name__}")
    return data


def _is_prometheus_label_name(value: str) -> bool:
    return re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is not None


def _dedupe_strings(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        if value not in out:
            out.append(value)
    return out


def _normalize_manual_input(raw: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    unknown_keys: list[str] = []
    for key, value in raw.items():
        if key.startswith("_"):
            normalized[key] = value
            continue
        if key in MANUAL_INPUT_KEYS:
            normalized[key] = value
            continue
        unknown_keys.append(key)
        normalized[key] = value
    if unknown_keys:
        normalized["_warning_unknown_keys"] = (
            "manual input contains keys not rendered by default sections: "
            + ", ".join(sorted(unknown_keys))
        )
    return normalized
