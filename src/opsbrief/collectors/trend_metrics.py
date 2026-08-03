from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from googleapiclient.errors import HttpError

from opsbrief.cluster_discovery import candidate_projects, resolve_clusters
from opsbrief.config import EnvConfig
from opsbrief.gcp_api import (
    build_cloud_sql_admin_service,
    build_cluster_manager_client,
    build_compute_instances_client,
    build_managed_kafka_client,
    build_metric_service_client,
    build_query_service_client,
    build_service,
    lazy_service,
    list_cloud_sql_instances,
    list_managed_kafka_clusters,
    list_monitoring_metric_descriptors,
    list_monitoring_time_series,
    protobuf_to_dict,
    query_monitoring_time_series,
    resolve_fallback_service,
)
from opsbrief.gcp_auth import AuthMode, get_auth_bundle
from opsbrief.models import CheckResult, Status, now_utc_iso
from opsbrief.status import classify_http_error, max_status

_UTILIZATION_WARNING_THRESHOLD = 85.0
_UTILIZATION_CRITICAL_THRESHOLD = 95.0
_DEFAULT_TREND_DAYS = 7
_TIME_SERIES_PAGE_SIZE = 200
_GKE_TIME_SERIES_PAGE_SIZE = 1000
_TIME_SERIES_MAX_SERIES = 300
_TIME_SERIES_MAX_PAGES = 5
_GKE_CLUSTER_ALIGNMENT_PERIOD = "3600s"
_GKE_CLUSTER_MAX_SERIES = 100
_GKE_NODE_MAX_SERIES = 1000
_GKE_NODE_MAX_PAGES = 5
_GKE_NAMESPACE_MAX_SERIES = 1000
_GKE_NAMESPACE_MAX_PAGES = 5
_GKE_POD_TOP_LIMIT = 50
_GKE_POD_MEMORY_SAMPLE_PERIOD = "1h"
_REDIS_EVICTIONS_WARNING = 1.0
_REDIS_EVICTIONS_CRITICAL = 5.0
_REDIS_REPLICATION_LAG_WARNING_SECONDS = 30.0
_REDIS_REPLICATION_LAG_CRITICAL_SECONDS = 120.0
_KAFKA_CONSUMER_LAG_WARNING = 10_000.0
_KAFKA_CONSUMER_LAG_CRITICAL = 50_000.0
_IDLE_CPU_THRESHOLD_PERCENT = 1.0


@dataclass(frozen=True)
class _MonitoringClients:
    service: Any | Callable[[], Any]
    metric_client: Any | None
    query_client: Any | None
    timeout_seconds: int


def collect(
    config: EnvConfig,
    timeout_seconds: int = 45,
    output_dir: str | None = None,
    auth_mode: AuthMode = "auto",
    impersonate_service_account: str = "",
) -> CheckResult:
    started_at = now_utc_iso()
    _ = output_dir
    status = Status.OK
    errors: list[str] = []
    project_rows: list[dict[str, Any]] = []

    projects = candidate_projects(config)
    if not projects:
        return CheckResult(
            collector="trend_metrics",
            status=Status.SKIPPED_CONFIG,
            summary="No projects configured for trend metrics collector",
            details={"projects": []},
            errors=[],
            started_at=started_at,
            finished_at=now_utc_iso(),
        )

    try:
        auth = get_auth_bundle(
            auth_mode=auth_mode,
            impersonate_service_account=impersonate_service_account,
        )
        monitoring_service = None

        def monitoring_service_fallback() -> Any:
            nonlocal monitoring_service
            if monitoring_service is None:
                monitoring_service = build_service(auth, "monitoring", "v3", timeout_seconds)
            return monitoring_service

        metric_service_client = None
        try:
            metric_service_client = build_metric_service_client(auth)
        except Exception:  # noqa: BLE001
            metric_service_client = None
        query_service_client = None
        try:
            query_service_client = build_query_service_client(auth)
        except Exception:  # noqa: BLE001
            query_service_client = None
        monitoring = _MonitoringClients(
            service=monitoring_service_fallback,
            metric_client=metric_service_client,
            query_client=query_service_client,
            timeout_seconds=timeout_seconds,
        )
        sqladmin = build_cloud_sql_admin_service(auth, timeout_seconds)
        compute = lazy_service(auth, "compute", "v1", timeout_seconds)
        managed_kafka_service: Callable[[], Any] | None = None
        managed_kafka_client = None
        if config.services.managed_kafka:
            managed_kafka_service = lazy_service(auth, "managedkafka", "v1", timeout_seconds)
            try:
                managed_kafka_client = build_managed_kafka_client(auth)
            except Exception:  # noqa: BLE001
                managed_kafka_client = None
        compute_instances_client = None
        try:
            compute_instances_client = build_compute_instances_client(auth)
        except Exception:  # noqa: BLE001
            compute_instances_client = None
        container_error = ""
        try:
            container = build_service(auth, "container", "v1", timeout_seconds)
            cluster_client = None
            try:
                cluster_client = build_cluster_manager_client(auth)
            except Exception:  # noqa: BLE001
                cluster_client = None
        except Exception as exc:  # noqa: BLE001
            container = None
            cluster_client = None
            container_error = str(exc)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            collector="trend_metrics",
            status=Status.FAILED,
            summary="Unable to initialize trend metrics collector dependencies",
            details={},
            errors=[str(exc)],
            started_at=started_at,
            finished_at=now_utc_iso(),
        )

    window_days = _trend_window_days(config)
    end = datetime.now(tz=UTC)
    start = end - timedelta(days=window_days)
    start_text = start.isoformat().replace("+00:00", "Z")
    end_text = end.isoformat().replace("+00:00", "Z")
    try:
        clusters = (
            resolve_clusters(
                config,
                container,
                cluster_client=cluster_client,
                timeout_seconds=timeout_seconds,
            )
            if container is not None
            else config.clusters
        )
        cluster_resolution_error = container_error
    except Exception as exc:  # noqa: BLE001
        clusters = config.clusters
        cluster_resolution_error = str(exc)

    for project in projects:
        try:
            sql_instances = _list_sql_instances(sqladmin, project)
            non_gke_instances = _list_non_gke_instances(
                compute,
                project,
                instances_client=compute_instances_client,
                timeout_seconds=timeout_seconds,
            )
            sql_trends = _collect_sql_trends(
                monitoring=monitoring,
                project=project,
                known_instances={str(item.get("name", "")) for item in sql_instances},
                start_text=start_text,
                end_text=end_text,
            )
            vm_trends = _collect_vm_trends(
                monitoring=monitoring,
                project=project,
                instances=non_gke_instances,
                start_text=start_text,
                end_text=end_text,
            )
            vm_cpu_error = ""
            vm_memory_trends, vm_memory_error = _collect_vm_memory_trends(
                monitoring=monitoring,
                project=project,
                instances=non_gke_instances,
                start_text=start_text,
                end_text=end_text,
            )
            (
                gke_cluster_peaks,
                gke_node_peaks,
                gke_pod_peaks,
                gke_namespace_peaks,
                gke_error,
            ) = _collect_gke_trends(
                monitoring=monitoring,
                project=project,
                clusters=[
                    cluster for cluster in clusters if getattr(cluster, "project", "") == project
                ],
                start_text=start_text,
                end_text=end_text,
            )
            if cluster_resolution_error:
                gke_error = (
                    f"{gke_error}; {cluster_resolution_error}"
                    if gke_error
                    else cluster_resolution_error
                )
            redis_throughput: list[dict[str, Any]] = []
            redis_error = ""
            redis_utilization: list[dict[str, Any]] = []
            redis_utilization_error = ""
            if config.services.redis:
                redis_throughput, redis_error = _collect_redis_throughput(
                    monitoring=monitoring,
                    project=project,
                    start_text=start_text,
                    end_text=end_text,
                )
                redis_utilization, redis_utilization_error = _collect_redis_resource_utilization(
                    config=config,
                    monitoring=monitoring,
                    project=project,
                    start_text=start_text,
                    end_text=end_text,
                )

            kafka_throughput: list[dict[str, Any]] = []
            kafka_error = ""
            kafka_utilization: list[dict[str, Any]] = []
            kafka_utilization_error = ""
            if config.services.managed_kafka:
                kafka_throughput, kafka_error = _collect_kafka_throughput(
                    monitoring=monitoring,
                    project=project,
                    start_text=start_text,
                    end_text=end_text,
                )
                known_kafka_clusters_by_location = _list_managed_kafka_clusters_by_location(
                    managed_kafka_service,
                    project,
                    kafka_client=managed_kafka_client,
                    timeout_seconds=timeout_seconds,
                )
                kafka_utilization, kafka_utilization_error = _collect_kafka_resource_utilization(
                    config=config,
                    monitoring=monitoring,
                    project=project,
                    start_text=start_text,
                    end_text=end_text,
                    known_clusters_by_location=known_kafka_clusters_by_location,
                )
        except HttpError as exc:
            failure_status = classify_http_error(exc)
            project_rows.append(
                {
                    "project": project,
                    "status": failure_status.value,
                    "error": str(exc),
                }
            )
            errors.append(f"{project}: {exc}")
            status = max_status(status, failure_status)
            continue
        except Exception as exc:  # noqa: BLE001
            project_rows.append(
                {
                    "project": project,
                    "status": Status.FAILED.value,
                    "error": str(exc),
                }
            )
            errors.append(f"{project}: {exc}")
            status = max_status(status, Status.FAILED)
            continue

        row_status = Status.OK
        warning_threshold = _threshold(
            config, "utilization_warning_percent", _UTILIZATION_WARNING_THRESHOLD
        )
        critical_threshold = _threshold(
            config, "utilization_critical_percent", _UTILIZATION_CRITICAL_THRESHOLD
        )
        sql_risk_count = sum(
            1
            for item in sql_trends
            if max(
                item.get("cpu_peak_percent", 0.0),
                item.get("memory_peak_percent", 0.0),
                item.get("disk_peak_percent", 0.0),
            )
            >= warning_threshold
        )
        sql_critical_count = sum(
            1
            for item in sql_trends
            if max(
                item.get("cpu_peak_percent", 0.0),
                item.get("memory_peak_percent", 0.0),
                item.get("disk_peak_percent", 0.0),
            )
            >= critical_threshold
        )
        vm_risk_count = sum(
            1
            for item in vm_trends
            if str(item.get("telemetry_status", "")) == "ok"
            and item.get("cpu_peak_percent", 0.0) >= warning_threshold
        )
        vm_critical_count = sum(
            1
            for item in vm_trends
            if str(item.get("telemetry_status", "")) == "ok"
            and item.get("cpu_peak_percent", 0.0) >= critical_threshold
        )
        vm_memory_risk_count = sum(
            1
            for item in vm_memory_trends
            if str(item.get("telemetry_status", "")) == "ok"
            and item.get("memory_peak_percent", 0.0) >= warning_threshold
        )
        vm_memory_critical_count = sum(
            1
            for item in vm_memory_trends
            if str(item.get("telemetry_status", "")) == "ok"
            and item.get("memory_peak_percent", 0.0) >= critical_threshold
        )
        gke_node_risk_count = sum(
            1
            for item in gke_node_peaks
            if max(
                item.get("cpu_allocatable_peak_percent", 0.0),
                item.get("memory_allocatable_peak_percent", 0.0),
            )
            >= warning_threshold
        )
        gke_node_critical_count = sum(
            1
            for item in gke_node_peaks
            if max(
                item.get("cpu_allocatable_peak_percent", 0.0),
                item.get("memory_allocatable_peak_percent", 0.0),
            )
            >= critical_threshold
        )
        redis_warning_count = sum(
            1 for item in redis_utilization if str(item.get("status", "")) == Status.WARNING.value
        )
        redis_critical_count = sum(
            1 for item in redis_utilization if str(item.get("status", "")) == Status.CRITICAL.value
        )
        kafka_warning_count = sum(
            1 for item in kafka_utilization if str(item.get("status", "")) == Status.WARNING.value
        )
        kafka_critical_count = sum(
            1 for item in kafka_utilization if str(item.get("status", "")) == Status.CRITICAL.value
        )

        if (
            sql_critical_count > 0
            or vm_critical_count > 0
            or vm_memory_critical_count > 0
            or gke_node_critical_count > 0
            or redis_critical_count > 0
            or kafka_critical_count > 0
        ):
            row_status = Status.CRITICAL
            status = max_status(status, Status.CRITICAL)
        elif (
            sql_risk_count > 0
            or vm_risk_count > 0
            or vm_memory_risk_count > 0
            or gke_node_risk_count > 0
            or redis_warning_count > 0
            or kafka_warning_count > 0
        ):
            row_status = Status.WARNING
            status = max_status(status, Status.WARNING)

        project_rows.append(
            {
                "project": project,
                "status": row_status.value,
                "window_days": window_days,
                "window_start": start_text,
                "window_end": end_text,
                "cloud_sql": sql_trends,
                "non_gke_vm_cpu": vm_trends,
                "non_gke_vm_memory": vm_memory_trends,
                "gke_cluster_utilization": gke_cluster_peaks,
                "gke_node_utilization": gke_node_peaks,
                "gke_pod_utilization": gke_pod_peaks,
                "gke_namespace_utilization": gke_namespace_peaks,
                "redis_throughput": redis_throughput,
                "kafka_throughput": kafka_throughput,
                "redis_utilization": redis_utilization,
                "kafka_utilization": kafka_utilization,
                "vm_cpu_error": vm_cpu_error,
                "vm_memory_error": vm_memory_error,
                "gke_utilization_error": gke_error,
                "redis_throughput_error": redis_error,
                "kafka_throughput_error": kafka_error,
                "redis_utilization_error": redis_utilization_error,
                "kafka_utilization_error": kafka_utilization_error,
                "sql_high_utilization_count": sql_risk_count,
                "sql_critical_utilization_count": sql_critical_count,
                "vm_high_cpu_count": vm_risk_count,
                "vm_critical_cpu_count": vm_critical_count,
                "vm_high_memory_count": vm_memory_risk_count,
                "vm_critical_memory_count": vm_memory_critical_count,
                "gke_node_high_utilization_count": gke_node_risk_count,
                "gke_node_critical_utilization_count": gke_node_critical_count,
                "redis_warning_count": redis_warning_count,
                "redis_critical_count": redis_critical_count,
                "kafka_warning_count": kafka_warning_count,
                "kafka_critical_count": kafka_critical_count,
            }
        )

    summary = f"Collected {window_days}d trend metrics for {len(project_rows)} project(s)"
    return CheckResult(
        collector="trend_metrics",
        status=status,
        summary=summary,
        details={
            "window_days": window_days,
            "projects": project_rows,
        },
        errors=errors,
        started_at=started_at,
        finished_at=now_utc_iso(),
    )


def _collect_sql_trends(
    monitoring: Any,
    project: str,
    known_instances: set[str],
    start_text: str,
    end_text: str,
) -> list[dict[str, Any]]:
    metric_map: dict[str, dict[str, list[float]]] = {}

    metrics = {
        "cpu": "cloudsql.googleapis.com/database/cpu/utilization",
        "memory": "cloudsql.googleapis.com/database/memory/utilization",
        "disk": "cloudsql.googleapis.com/database/disk/utilization",
    }
    for key, metric_type in metrics.items():
        series = _fetch_time_series(
            monitoring=monitoring,
            project=project,
            metric_type=metric_type,
            start_text=start_text,
            end_text=end_text,
        )
        for item in series:
            labels = _as_dict(_as_dict(item.get("resource")).get("labels"))
            db_id = str(labels.get("database_id", ""))
            instance_name = db_id.split(":")[-1] if ":" in db_id else db_id
            if not instance_name:
                continue
            if known_instances and instance_name not in known_instances:
                continue
            metric_map.setdefault(instance_name, {"cpu": [], "memory": [], "disk": []})
            metric_map[instance_name][key].extend(_extract_point_values(item))

    rows: list[dict[str, Any]] = []
    for instance in sorted(metric_map):
        cpu_stats = _summary_stats(metric_map[instance]["cpu"])
        mem_stats = _summary_stats(metric_map[instance]["memory"])
        disk_stats = _summary_stats(metric_map[instance]["disk"])
        rows.append(
            {
                "instance": instance,
                "cpu_avg_percent": cpu_stats["avg"],
                "cpu_p95_percent": cpu_stats["p95"],
                "cpu_peak_percent": cpu_stats["peak"],
                "memory_avg_percent": mem_stats["avg"],
                "memory_p95_percent": mem_stats["p95"],
                "memory_peak_percent": mem_stats["peak"],
                "disk_avg_percent": disk_stats["avg"],
                "disk_p95_percent": disk_stats["p95"],
                "disk_peak_percent": disk_stats["peak"],
            }
        )
    return rows


def _collect_vm_trends(
    monitoring: Any,
    project: str,
    instances: list[dict[str, str]],
    start_text: str,
    end_text: str,
) -> list[dict[str, Any]]:
    id_to_name = {
        item["id"]: item["name"] for item in instances if item.get("id") and item.get("name")
    }
    if not id_to_name:
        return []

    rows: list[dict[str, Any]] = []
    for instance_id, name in sorted(id_to_name.items(), key=lambda item: item[1]):
        series = _fetch_time_series(
            monitoring=monitoring,
            project=project,
            metric_type="compute.googleapis.com/instance/cpu/utilization",
            start_text=start_text,
            end_text=end_text,
            filter_suffix=f'resource.labels.instance_id="{instance_id}"',
            max_series=5,
            max_pages=1,
        )
        values: list[float] = []
        for item in series:
            values.extend(_extract_point_values(item))
        if values:
            stats = _summary_stats(values)
            rows.append(
                {
                    "instance": name,
                    "telemetry_status": "ok",
                    "cpu_source": "gcp_native",
                    "cpu_avg_percent": stats["avg"],
                    "cpu_min_percent": stats["min"],
                    "cpu_p95_percent": stats["p95"],
                    "cpu_peak_percent": stats["peak"],
                    "idle_capacity_at_peak_percent": _idle_capacity_percent(stats["peak"]),
                    "activity_state": _cpu_activity_state(stats["peak"], observed=True),
                    "note": "",
                }
            )
            continue
        rows.append(
            {
                "instance": name,
                "telemetry_status": "missing",
                "cpu_source": "gcp_native",
                "cpu_avg_percent": 0.0,
                "cpu_min_percent": 0.0,
                "cpu_p95_percent": 0.0,
                "cpu_peak_percent": 0.0,
                "idle_capacity_at_peak_percent": 0.0,
                "activity_state": "unknown",
                "note": "GCP native VM CPU metric not found for this VM.",
            }
        )
    return rows


def _collect_vm_cpu_ops_agent_trends(
    monitoring: Any,
    project: str,
    instances: list[dict[str, str]],
    start_text: str,
    end_text: str,
) -> tuple[list[dict[str, Any]], str]:
    id_to_name = {
        item["id"]: item["name"] for item in instances if item.get("id") and item.get("name")
    }
    if not id_to_name:
        return [], ""

    try:
        series = _fetch_time_series(
            monitoring=monitoring,
            project=project,
            metric_type="agent.googleapis.com/cpu/utilization",
            start_text=start_text,
            end_text=end_text,
        )
    except HttpError as exc:
        return [], str(exc)
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)

    value_map: dict[str, list[float]] = {}
    for item in series:
        labels = _as_dict(_as_dict(item.get("resource")).get("labels"))
        instance_id = str(labels.get("instance_id", ""))
        name = id_to_name.get(instance_id, "")
        if not name:
            continue
        value_map.setdefault(name, [])
        value_map[name].extend(_extract_point_values(item))

    rows: list[dict[str, Any]] = []
    for name in sorted(value_map):
        stats = _summary_stats(value_map[name])
        rows.append(
            {
                "instance": name,
                "telemetry_status": "ok",
                "cpu_avg_percent": stats["avg"],
                "cpu_min_percent": stats["min"],
                "cpu_p95_percent": stats["p95"],
                "cpu_peak_percent": stats["peak"],
                "idle_capacity_at_peak_percent": _idle_capacity_percent(stats["peak"]),
                "activity_state": _cpu_activity_state(stats["peak"], observed=True),
                "note": "",
            }
        )

    missing_names = sorted(name for name in id_to_name.values() if name not in value_map)
    for name in missing_names:
        rows.append(
            {
                "instance": name,
                "telemetry_status": "missing",
                "cpu_avg_percent": 0.0,
                "cpu_min_percent": 0.0,
                "cpu_p95_percent": 0.0,
                "cpu_peak_percent": 0.0,
                "idle_capacity_at_peak_percent": 0.0,
                "activity_state": "unknown",
                "note": "Ops Agent CPU metric not found for this VM.",
            }
        )

    if missing_names:
        return rows, (
            f"ops agent CPU metric missing for {len(missing_names)} of "
            f"{len(id_to_name)} non-GKE VM(s)"
        )
    return rows, ""


def _collect_vm_memory_trends(
    monitoring: Any,
    project: str,
    instances: list[dict[str, str]],
    start_text: str,
    end_text: str,
) -> tuple[list[dict[str, Any]], str]:
    id_to_name = {
        item["id"]: item["name"] for item in instances if item.get("id") and item.get("name")
    }
    if not id_to_name:
        return [], ""

    try:
        series = _fetch_time_series(
            monitoring=monitoring,
            project=project,
            metric_type="agent.googleapis.com/memory/percent_used",
            start_text=start_text,
            end_text=end_text,
            filter_suffix='metric.labels.state="used"',
        )
    except HttpError as exc:
        return [], str(exc)
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)

    value_map: dict[str, list[float]] = {}
    for item in series:
        labels = _as_dict(_as_dict(item.get("resource")).get("labels"))
        instance_id = str(labels.get("instance_id", ""))
        name = id_to_name.get(instance_id, "")
        if not name:
            continue
        value_map.setdefault(name, [])
        value_map[name].extend(_extract_point_values(item))

    rows: list[dict[str, Any]] = []
    for name in sorted(value_map):
        stats = _summary_stats_raw(value_map[name])
        rows.append(
            {
                "instance": name,
                "telemetry_status": "ok",
                "memory_avg_percent": stats["avg"],
                "memory_p95_percent": stats["p95"],
                "memory_peak_percent": stats["peak"],
                "note": "",
            }
        )

    missing_names = sorted(name for name in id_to_name.values() if name not in value_map)
    for name in missing_names:
        rows.append(
            {
                "instance": name,
                "telemetry_status": "missing",
                "memory_avg_percent": 0.0,
                "memory_p95_percent": 0.0,
                "memory_peak_percent": 0.0,
                "note": "Ops Agent memory metric not found for this VM.",
            }
        )

    if missing_names:
        return rows, (
            f"ops agent memory metric missing for {len(missing_names)} of "
            f"{len(id_to_name)} non-GKE VM(s)"
        )
    return rows, ""


def _collect_gke_trends(
    monitoring: Any,
    project: str,
    clusters: list[Any],
    start_text: str,
    end_text: str,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    str,
]:
    cluster_names = sorted(
        {
            str(getattr(cluster, "name", "") or "").strip()
            for cluster in clusters
            if str(getattr(cluster, "name", "") or "").strip()
        }
    )
    if not cluster_names:
        return [], [], [], [], ""

    try:
        cluster_rows, node_rows, pod_rows, namespace_rows = _collect_gke_project_trends(
            monitoring=monitoring,
            project=project,
            cluster_names=cluster_names,
            start_text=start_text,
            end_text=end_text,
        )
    except HttpError as exc:
        return [], [], [], [], str(exc)
    except Exception as exc:  # noqa: BLE001
        return [], [], [], [], str(exc)

    node_rows.sort(
        key=lambda item: max(
            _as_float(item.get("cpu_allocatable_peak_percent", 0.0)),
            _as_float(item.get("memory_allocatable_peak_percent", 0.0)),
        ),
        reverse=True,
    )
    pod_rows.sort(
        key=lambda item: (
            _as_float(item.get("cpu_peak_cores", 0.0)),
            _as_float(item.get("memory_peak_bytes", 0.0)),
        ),
        reverse=True,
    )
    namespace_rows.sort(
        key=lambda item: (
            _as_float(item.get("cpu_peak_cores", 0.0)),
            _as_float(item.get("memory_peak_bytes", 0.0)),
        ),
        reverse=True,
    )
    return cluster_rows, node_rows[:50], pod_rows[:50], namespace_rows[:50], ""


def _collect_gke_cluster_trends(
    monitoring: Any,
    project: str,
    cluster_name: str,
    start_text: str,
    end_text: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    cluster_rows, node_rows, pod_rows, namespace_rows = _collect_gke_project_trends(
        monitoring=monitoring,
        project=project,
        cluster_names=[cluster_name],
        start_text=start_text,
        end_text=end_text,
    )
    cluster_summary = cluster_rows[0] if cluster_rows else _empty_gke_cluster_row(cluster_name)
    return cluster_summary, node_rows, pod_rows, namespace_rows


def _collect_gke_project_trends(
    monitoring: Any,
    project: str,
    cluster_names: list[str],
    start_text: str,
    end_text: str,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    cluster_set = set(cluster_names)
    full_window_alignment_period = _window_alignment_period(start_text, end_text)
    full_window_duration = _window_duration(start_text, end_text)
    cluster_cpu_series: list[dict[str, Any]] = []
    cluster_memory_series: list[dict[str, Any]] = []
    for cluster_name in cluster_names:
        per_cluster_filter = _single_cluster_filter([cluster_name])
        cluster_cpu_series.extend(
            _fetch_time_series(
                monitoring=monitoring,
                project=project,
                metric_type="kubernetes.io/container/cpu/core_usage_time",
                start_text=start_text,
                end_text=end_text,
                aligner="ALIGN_RATE",
                alignment_period=_GKE_CLUSTER_ALIGNMENT_PERIOD,
                filter_suffix=per_cluster_filter,
                reducer="REDUCE_SUM",
                group_by_fields=["resource.labels.cluster_name"],
                max_series=_GKE_CLUSTER_MAX_SERIES,
                max_pages=1,
            )
        )
        cluster_memory_series.extend(
            _fetch_time_series(
                monitoring=monitoring,
                project=project,
                metric_type="kubernetes.io/container/memory/used_bytes",
                start_text=start_text,
                end_text=end_text,
                aligner="ALIGN_MAX",
                alignment_period=_GKE_CLUSTER_ALIGNMENT_PERIOD,
                filter_suffix=per_cluster_filter,
                reducer="REDUCE_SUM",
                group_by_fields=["resource.labels.cluster_name"],
                max_series=_GKE_CLUSTER_MAX_SERIES,
                max_pages=1,
            )
        )
    project_cluster_filter = _single_cluster_filter(cluster_names)
    pod_rows = _collect_gke_pod_top_trends(
        monitoring=monitoring,
        project=project,
        cluster_names=cluster_names,
        alignment_period=full_window_alignment_period,
        duration=full_window_duration,
    )
    namespace_rows = _collect_gke_namespace_trends(
        monitoring=monitoring,
        project=project,
        cluster_names=cluster_names,
        start_text=start_text,
        end_text=end_text,
        alignment_period=_GKE_CLUSTER_ALIGNMENT_PERIOD,
        duration=full_window_duration,
    )
    node_cpu_series = _fetch_time_series(
        monitoring=monitoring,
        project=project,
        metric_type="kubernetes.io/node/cpu/allocatable_utilization",
        start_text=start_text,
        end_text=end_text,
        aligner="ALIGN_MAX",
        alignment_period=full_window_alignment_period,
        filter_suffix=project_cluster_filter,
        reducer="REDUCE_MAX",
        group_by_fields=["resource.labels.cluster_name", "resource.labels.node_name"],
        max_series=_GKE_NODE_MAX_SERIES,
        max_pages=_GKE_NODE_MAX_PAGES,
        page_size=_GKE_TIME_SERIES_PAGE_SIZE,
    )
    node_memory_series = _fetch_time_series(
        monitoring=monitoring,
        project=project,
        metric_type="kubernetes.io/node/memory/allocatable_utilization",
        start_text=start_text,
        end_text=end_text,
        aligner="ALIGN_MAX",
        alignment_period=full_window_alignment_period,
        filter_suffix=project_cluster_filter,
        reducer="REDUCE_MAX",
        group_by_fields=["resource.labels.cluster_name", "resource.labels.node_name"],
        max_series=_GKE_NODE_MAX_SERIES,
        max_pages=_GKE_NODE_MAX_PAGES,
        page_size=_GKE_TIME_SERIES_PAGE_SIZE,
    )

    pod_keys = {
        (str(row.get("cluster", "")), str(row.get("namespace", "")), str(row.get("pod", "")))
        for row in pod_rows
    }

    node_cpu = _node_utilization_series(node_cpu_series)
    node_memory = _node_utilization_series(node_memory_series)
    node_names = sorted(set(node_cpu) | set(node_memory))
    node_rows = [
        {
            "cluster": key[0],
            "node": key[1],
            "cpu_allocatable_peak_percent": _peak_aggregate(node_cpu.get(key, {})) * 100.0,
            "cpu_idle_capacity_at_peak_percent": _idle_capacity_percent(
                _peak_aggregate(node_cpu.get(key, {})) * 100.0
            ),
            "memory_allocatable_peak_percent": _peak_aggregate(node_memory.get(key, {})) * 100.0,
            "alignment_period": full_window_alignment_period,
        }
        for key in node_names
        if key[0] in cluster_set
    ]

    cluster_cpu_peaks = _peaks_by_cluster(cluster_cpu_series, cluster_names)
    cluster_memory_peaks = _peaks_by_cluster(cluster_memory_series, cluster_names)
    cluster_rows: list[dict[str, Any]] = []
    for name in cluster_names:
        max_node_cpu_percent = max(
            (
                _as_float(row["cpu_allocatable_peak_percent"])
                for row in node_rows
                if row.get("cluster") == name
            ),
            default=0.0,
        )
        max_node_memory_percent = max(
            (
                _as_float(row["memory_allocatable_peak_percent"])
                for row in node_rows
                if row.get("cluster") == name
            ),
            default=0.0,
        )
        cluster_rows.append(
            {
                "cluster": name,
                "pod_series_count": sum(1 for key in pod_keys if key[0] == name),
                "node_series_count": sum(1 for key in node_names if key[0] == name),
                "pod_alignment_period": full_window_alignment_period,
                "pod_cpu_aggregation": "max_daily_rate",
                "pod_memory_aggregation": "max_hourly_sample",
                "node_alignment_period": full_window_alignment_period,
                "cpu_peak_cores": cluster_cpu_peaks.get(name, 0.0),
                "memory_peak_bytes": cluster_memory_peaks.get(name, 0.0),
                "max_node_cpu_allocatable_peak_percent": max_node_cpu_percent,
                "max_node_cpu_idle_capacity_at_peak_percent": _idle_capacity_percent(
                    max_node_cpu_percent
                ),
                "max_node_memory_allocatable_peak_percent": max_node_memory_percent,
                "activity_state": _cpu_activity_state(
                    max_node_cpu_percent,
                    observed=bool(sum(1 for key in node_names if key[0] == name)),
                ),
            }
        )
    return cluster_rows, node_rows, pod_rows, namespace_rows


def _single_cluster_filter(cluster_names: list[str]) -> str:
    if len(cluster_names) != 1:
        return ""
    return f'resource.labels.cluster_name="{cluster_names[0]}"'


def _empty_gke_cluster_row(cluster_name: str) -> dict[str, Any]:
    return {
        "cluster": cluster_name,
        "pod_series_count": 0,
        "node_series_count": 0,
        "pod_alignment_period": "",
        "pod_cpu_aggregation": "max_daily_rate",
        "pod_memory_aggregation": "max_hourly_sample",
        "node_alignment_period": "",
        "cpu_peak_cores": 0.0,
        "memory_peak_bytes": 0.0,
        "max_node_cpu_allocatable_peak_percent": 0.0,
        "max_node_cpu_idle_capacity_at_peak_percent": 0.0,
        "max_node_memory_allocatable_peak_percent": 0.0,
        "activity_state": "unknown",
    }


def _cpu_activity_state(cpu_peak_percent: float, *, observed: bool) -> str:
    if not observed:
        return "unknown"
    if cpu_peak_percent < _IDLE_CPU_THRESHOLD_PERCENT:
        return "idle"
    return "active"


def _collect_gke_namespace_trends(
    monitoring: Any,
    project: str,
    cluster_names: list[str],
    start_text: str,
    end_text: str,
    alignment_period: str,
    duration: str,
) -> list[dict[str, Any]]:
    cpu_series: list[dict[str, Any]] = []
    memory_series: list[dict[str, Any]] = []
    for cluster_name in cluster_names:
        per_cluster_filter = _single_cluster_filter([cluster_name])
        cpu_series.extend(
            _fetch_time_series(
                monitoring=monitoring,
                project=project,
                metric_type="kubernetes.io/container/cpu/core_usage_time",
                start_text=start_text,
                end_text=end_text,
                aligner="ALIGN_RATE",
                alignment_period=alignment_period,
                filter_suffix=per_cluster_filter,
                reducer="REDUCE_SUM",
                group_by_fields=[
                    "resource.labels.cluster_name",
                    "resource.labels.namespace_name",
                ],
                max_series=_GKE_NAMESPACE_MAX_SERIES,
                max_pages=_GKE_NAMESPACE_MAX_PAGES,
                page_size=_GKE_TIME_SERIES_PAGE_SIZE,
            )
        )
        memory_series.extend(
            _fetch_time_series(
                monitoring=monitoring,
                project=project,
                metric_type="kubernetes.io/container/memory/used_bytes",
                start_text=start_text,
                end_text=end_text,
                aligner="ALIGN_MAX",
                alignment_period=alignment_period,
                filter_suffix=per_cluster_filter,
                reducer="REDUCE_SUM",
                group_by_fields=[
                    "resource.labels.cluster_name",
                    "resource.labels.namespace_name",
                ],
                max_series=_GKE_NAMESPACE_MAX_SERIES,
                max_pages=_GKE_NAMESPACE_MAX_PAGES,
                page_size=_GKE_TIME_SERIES_PAGE_SIZE,
            )
        )

    cluster_set = set(cluster_names)
    fallback_cluster = cluster_names[0] if len(cluster_names) == 1 else ""
    cpu_values = _namespace_metric_values(cpu_series, fallback_cluster)
    memory_values = _namespace_metric_values(memory_series, fallback_cluster)
    rows: list[dict[str, Any]] = []
    for key in sorted(set(cpu_values) | set(memory_values)):
        cluster, namespace = key
        if cluster not in cluster_set:
            continue
        cpu_stats = _summary_stats_raw(cpu_values.get(key, []))
        memory_stats = _summary_stats_raw(memory_values.get(key, []))
        rows.append(
            {
                "cluster": cluster,
                "namespace": namespace,
                "cpu_avg_cores": cpu_stats["avg"],
                "cpu_p95_cores": cpu_stats["p95"],
                "cpu_peak_cores": cpu_stats["peak"],
                "memory_avg_bytes": memory_stats["avg"],
                "memory_p95_bytes": memory_stats["p95"],
                "memory_peak_bytes": memory_stats["peak"],
                "window_duration": duration,
                "alignment_period": alignment_period,
                "cpu_aggregation": "hourly_rate_sum",
                "memory_aggregation": "hourly_max_sum",
            }
        )
    return rows


def _collect_gke_pod_top_trends(
    monitoring: Any,
    project: str,
    cluster_names: list[str],
    alignment_period: str,
    duration: str,
) -> list[dict[str, Any]]:
    cpu_values = _query_gke_pod_top_mql(
        monitoring=monitoring,
        project=project,
        query=_gke_pod_cpu_peak_query(duration=duration),
    )
    memory_values = _query_gke_pod_top_mql(
        monitoring=monitoring,
        project=project,
        query=_gke_pod_memory_peak_query(duration=duration),
    )
    cluster_set = set(cluster_names)
    pod_keys = sorted(key for key in set(cpu_values) | set(memory_values) if key[0] in cluster_set)
    return [
        {
            "cluster": key[0],
            "namespace": key[1],
            "pod": key[2],
            "cpu_peak_cores": cpu_values.get(key),
            "memory_peak_bytes": memory_values.get(key),
            "cpu_peak_observed": key in cpu_values,
            "memory_peak_observed": key in memory_values,
            "window_duration": duration,
            "alignment_period": alignment_period,
            "cpu_aggregation": "max_daily_rate",
            "memory_aggregation": "max_hourly_sample",
            "memory_sample_period": _GKE_POD_MEMORY_SAMPLE_PERIOD,
        }
        for key in pod_keys
    ]


def _query_gke_pod_top_mql(
    monitoring: Any,
    project: str,
    query: str,
) -> dict[tuple[str, str, str], float]:
    query_client = _monitoring_query_client(monitoring)
    if query_client is not None:
        try:
            response = query_monitoring_time_series(
                query_client,
                project=project,
                query=query,
                timeout_seconds=_monitoring_timeout_seconds(monitoring),
            )
            return _mql_pod_values(response)
        except Exception:  # noqa: BLE001
            pass

    service = _monitoring_service(monitoring)
    response = (
        service.projects()
        .timeSeries()
        .query(name=f"projects/{project}", body={"query": query})
        .execute()
    )
    return _mql_pod_values(_as_dict(response))


def _gke_pod_cpu_peak_query(duration: str) -> str:
    return "\n".join(
        [
            "fetch k8s_container",
            "| metric 'kubernetes.io/container/cpu/core_usage_time'",
            "| align rate(1d)",
            "| every 1d",
            (
                "| group_by [resource.cluster_name, resource.namespace_name, resource.pod_name], "
                "[cpu: sum(value.core_usage_time)]"
            ),
            f"| group_by {duration}, [cpu_peak: max(cpu)]",
            f"| top {_GKE_POD_TOP_LIMIT}, cpu_peak",
            f"| within {duration}",
        ]
    )


def _gke_pod_memory_peak_query(duration: str) -> str:
    return "\n".join(
        [
            "fetch k8s_container",
            "| metric 'kubernetes.io/container/memory/used_bytes'",
            f"| within {duration}",
            f"| every {_GKE_POD_MEMORY_SAMPLE_PERIOD}",
            (
                "| group_by [resource.cluster_name, resource.namespace_name, resource.pod_name], "
                "[memory: sum(value.used_bytes)]"
            ),
            f"| group_by {duration}, [memory_peak: max(memory)]",
            f"| top {_GKE_POD_TOP_LIMIT}, memory_peak",
        ]
    )


def _mql_pod_values(response: dict[str, Any]) -> dict[tuple[str, str, str], float]:
    descriptor = _as_dict(response.get("timeSeriesDescriptor"))
    label_keys = [
        str(item.get("key", "")).strip()
        for item in _as_dict_list(descriptor.get("labelDescriptors"))
    ]
    cluster_index = _mql_label_index(label_keys, "resource.cluster_name", "cluster_name")
    namespace_index = _mql_label_index(
        label_keys,
        "resource.namespace_name",
        "namespace_name",
    )
    pod_index = _mql_label_index(label_keys, "resource.pod_name", "pod_name")

    values_by_pod: dict[tuple[str, str, str], float] = {}
    for series in _as_dict_list(response.get("timeSeriesData")):
        label_values = _as_dict_list(series.get("labelValues"))
        cluster = _mql_label_value(label_values, cluster_index)
        namespace = _mql_label_value(label_values, namespace_index) or "default"
        pod = _mql_label_value(label_values, pod_index)
        if not cluster or not pod:
            continue
        point_values: list[float] = []
        for point in _as_dict_list(series.get("pointData")):
            values = _as_dict_list(point.get("values"))
            if values:
                point_values.append(_mql_value(values[0]))
        if not point_values:
            continue
        key = (cluster, namespace, pod)
        values_by_pod[key] = max(values_by_pod.get(key, 0.0), max(point_values))
    return values_by_pod


def _mql_label_index(label_keys: list[str], *candidates: str) -> int | None:
    candidate_set = set(candidates)
    candidate_suffixes = {f".{candidate}" for candidate in candidates}
    for index, key in enumerate(label_keys):
        if key in candidate_set or any(key.endswith(suffix) for suffix in candidate_suffixes):
            return index
    return None


def _mql_label_value(label_values: list[dict[str, Any]], index: int | None) -> str:
    if index is None or index >= len(label_values):
        return ""
    raw = label_values[index]
    for value_key in ("stringValue", "int64Value", "doubleValue", "boolValue"):
        value = raw.get(value_key)
        if value is not None:
            return str(value).strip()
    return ""


def _mql_value(value: dict[str, Any]) -> float:
    for value_key in ("doubleValue", "int64Value"):
        candidate = value.get(value_key)
        if isinstance(candidate, (int, float)):
            return float(candidate)
        if isinstance(candidate, str):
            try:
                return float(candidate)
            except ValueError:
                return 0.0
    return 0.0


def _window_alignment_period(start_text: str, end_text: str) -> str:
    return f"{_window_duration_seconds(start_text, end_text)}s"


def _window_duration(start_text: str, end_text: str) -> str:
    seconds = _window_duration_seconds(start_text, end_text)
    if seconds % 86_400 == 0:
        return f"{max(1, seconds // 86_400)}d"
    return f"{seconds}s"


def _window_duration_seconds(start_text: str, end_text: str) -> int:
    try:
        start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_text.replace("Z", "+00:00"))
    except ValueError:
        return _DEFAULT_TREND_DAYS * 24 * 60 * 60
    return max(60, int((end - start).total_seconds()))


def _peaks_by_cluster(series: list[dict[str, Any]], cluster_names: list[str]) -> dict[str, float]:
    fallback_cluster = cluster_names[0] if len(cluster_names) == 1 else ""
    peaks: dict[str, float] = {}
    for item in series:
        cluster = _cluster_label(item) or fallback_cluster
        if not cluster:
            continue
        peak = max(_extract_point_values(item), default=0.0)
        peaks[cluster] = max(peaks.get(cluster, 0.0), peak)
    return peaks


def _namespace_metric_values(
    series: list[dict[str, Any]], fallback_cluster: str = ""
) -> dict[tuple[str, str], list[float]]:
    values_by_namespace: dict[tuple[str, str], list[float]] = {}
    for item in series:
        labels = _as_dict(_as_dict(item.get("resource")).get("labels"))
        cluster = str(labels.get("cluster_name", "")).strip() or fallback_cluster
        namespace = str(labels.get("namespace_name", "")).strip() or "default"
        if not cluster:
            continue
        values_by_namespace.setdefault((cluster, namespace), [])
        values_by_namespace[(cluster, namespace)].extend(_extract_point_values(item))
    return values_by_namespace


def _aggregate_container_series(
    series: list[dict[str, Any]],
) -> dict[tuple[str, str, str], dict[str, float]]:
    buckets: dict[tuple[str, str, str], dict[str, float]] = {}
    for item in series:
        labels = _as_dict(_as_dict(item.get("resource")).get("labels"))
        cluster = str(labels.get("cluster_name", "")).strip()
        namespace = str(labels.get("namespace_name", "")).strip() or "default"
        pod = str(labels.get("pod_name", "")).strip()
        if not cluster or not pod:
            continue
        key = (cluster, namespace, pod)
        bucket = buckets.setdefault(key, {})
        for timestamp, value in _extract_timed_point_values(item):
            bucket[timestamp] = bucket.get(timestamp, 0.0) + value
    return buckets


def _node_utilization_series(
    series: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, float]]:
    buckets: dict[tuple[str, str], dict[str, float]] = {}
    for item in series:
        labels = _as_dict(_as_dict(item.get("resource")).get("labels"))
        cluster = str(labels.get("cluster_name", "")).strip()
        node = (
            str(labels.get("node_name", "")).strip()
            or str(labels.get("instance_id", "")).strip()
            or "unknown-node"
        )
        if not cluster:
            continue
        key = (cluster, node)
        bucket = buckets.setdefault(key, {})
        for timestamp, value in _extract_timed_point_values(item):
            bucket[timestamp] = max(bucket.get(timestamp, 0.0), value)
    return buckets


def _cluster_label(series: dict[str, Any]) -> str:
    labels = _as_dict(_as_dict(series.get("resource")).get("labels"))
    return str(labels.get("cluster_name", "")).strip()


def _collect_redis_throughput(
    monitoring: Any,
    project: str,
    start_text: str,
    end_text: str,
) -> tuple[list[dict[str, Any]], str]:
    metric_type = _resolve_metric_type(
        monitoring=monitoring,
        project=project,
        prefix="redis.googleapis.com/",
        suffix="stats/network_traffic",
    )
    if not metric_type:
        return [], "redis throughput metric not found"

    try:
        series = _fetch_time_series(
            monitoring=monitoring,
            project=project,
            metric_type=metric_type,
            start_text=start_text,
            end_text=end_text,
            aligner="ALIGN_RATE",
            alignment_period="300s",
        )
    except HttpError as exc:
        return [], str(exc)

    bucket: dict[tuple[str, str], list[float]] = {}
    for item in series:
        resource_labels = _as_dict(_as_dict(item.get("resource")).get("labels"))
        metric_labels = _as_dict(_as_dict(item.get("metric")).get("labels"))
        instance = (
            str(resource_labels.get("instance_id", "")).strip()
            or str(resource_labels.get("instance_name", "")).strip()
            or str(resource_labels.get("node_id", "")).strip()
            or "unknown-instance"
        )
        direction = str(metric_labels.get("direction", "")).strip().lower() or "total"
        key = (instance, direction)
        bucket.setdefault(key, [])
        bucket[key].extend(_extract_point_values(item))

    rows: list[dict[str, Any]] = []
    for (instance, direction), values in sorted(bucket.items()):
        stats = _summary_stats_raw(values)
        rows.append(
            {
                "instance": instance,
                "direction": direction,
                "bytes_per_second_avg": stats["avg"],
                "bytes_per_second_p95": stats["p95"],
                "bytes_per_second_peak": stats["peak"],
            }
        )
    return rows, ""


def _collect_kafka_throughput(
    monitoring: Any,
    project: str,
    start_text: str,
    end_text: str,
) -> tuple[list[dict[str, Any]], str]:
    metric_in = _resolve_metric_type(
        monitoring=monitoring,
        project=project,
        prefix="managedkafka.googleapis.com/",
        suffix="cluster_byte_in_count",
    )
    metric_out = _resolve_metric_type(
        monitoring=monitoring,
        project=project,
        prefix="managedkafka.googleapis.com/",
        suffix="cluster_byte_out_count",
    )
    metric_msg = _resolve_metric_type(
        monitoring=monitoring,
        project=project,
        prefix="managedkafka.googleapis.com/",
        suffix="cluster_message_in_count",
    )

    if not metric_in and not metric_out and not metric_msg:
        return [], "managed kafka throughput metrics not found"

    rows_by_cluster: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    metric_map = {
        "bytes_in": metric_in,
        "bytes_out": metric_out,
        "messages_in": metric_msg,
    }
    for metric_key, metric_type in metric_map.items():
        if not metric_type:
            continue
        try:
            series = _fetch_time_series(
                monitoring=monitoring,
                project=project,
                metric_type=metric_type,
                start_text=start_text,
                end_text=end_text,
                aligner="ALIGN_RATE",
                alignment_period="300s",
            )
        except HttpError as exc:
            errors.append(str(exc))
            continue

        bucket: dict[str, list[float]] = {}
        for item in series:
            resource_labels = _as_dict(_as_dict(item.get("resource")).get("labels"))
            metric_labels = _as_dict(_as_dict(item.get("metric")).get("labels"))
            cluster = (
                str(resource_labels.get("cluster_id", "")).strip()
                or str(resource_labels.get("cluster_name", "")).strip()
                or str(metric_labels.get("cluster_id", "")).strip()
                or "unknown-cluster"
            )
            bucket.setdefault(cluster, [])
            bucket[cluster].extend(_extract_point_values(item))

        for cluster, values in bucket.items():
            stats = _summary_stats_raw(values)
            row = rows_by_cluster.setdefault(cluster, {"cluster": cluster})
            row[f"{metric_key}_avg"] = stats["avg"]
            row[f"{metric_key}_p95"] = stats["p95"]
            row[f"{metric_key}_peak"] = stats["peak"]

    rows = [rows_by_cluster[name] for name in sorted(rows_by_cluster)]
    return rows, "; ".join(errors)


def _collect_redis_resource_utilization(
    *,
    config: EnvConfig,
    monitoring: Any,
    project: str,
    start_text: str,
    end_text: str,
) -> tuple[list[dict[str, Any]], str]:
    descriptor_prefix = "redis.googleapis.com/"
    metric_map = {
        "cpu_peak_percent": (
            "cluster/cpu/maximum_utilization",
            "cluster/cpu/average_utilization",
            "cluster/node/cpu/utilization",
        ),
        "memory_peak_percent": (
            "stats/memory/usage_ratio",
            "stats/memory/usage",
        ),
        "eviction_rate_peak": (
            "stats/evicted_keys_count",
            "stats/evictions_count",
        ),
        "connected_clients_peak": (
            "stats/clients/connected",
            "stats/connected_clients",
        ),
        "ops_per_second_peak": (
            "stats/commands_count",
            "stats/operations_count",
        ),
        "replication_lag_peak_seconds": (
            "stats/replication/lag",
            "stats/replication_lag",
        ),
    }

    rows_by_instance: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for field_name, candidates in metric_map.items():
        try:
            metric_types = _resolve_metric_type_candidate_list(
                monitoring=monitoring,
                project=project,
                prefix=descriptor_prefix,
                suffixes=candidates,
            )
            aligner = "ALIGN_MEAN"
            if field_name in {"eviction_rate_peak", "ops_per_second_peak"}:
                aligner = "ALIGN_RATE"
            series: list[dict[str, Any]] = []
            for metric_type in metric_types:
                series = _fetch_time_series(
                    monitoring=monitoring,
                    project=project,
                    metric_type=metric_type,
                    start_text=start_text,
                    end_text=end_text,
                    aligner=aligner,
                    alignment_period="300s",
                )
                if series:
                    break
            if not series:
                continue
            bucket: dict[str, list[float]] = {}
            for item in series:
                resource_labels = _as_dict(_as_dict(item.get("resource")).get("labels"))
                instance = (
                    str(resource_labels.get("instance_id", "")).strip()
                    or str(resource_labels.get("instance_name", "")).strip()
                    or str(resource_labels.get("node_id", "")).strip()
                    or "unknown-instance"
                )
                bucket.setdefault(instance, [])
                bucket[instance].extend(_extract_point_values(item))
            for instance, values in bucket.items():
                stats = _summary_stats_raw(values)
                row = rows_by_instance.setdefault(instance, {"instance": instance})
                value = stats["peak"]
                if field_name in {"cpu_peak_percent", "memory_peak_percent"}:
                    value *= 100.0 if value <= 1.0 else 1.0
                row[field_name] = value
                row[f"{field_name}_observed"] = True
        except HttpError as exc:
            errors.append(str(exc))
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))

    warning_threshold = _threshold(
        config, "utilization_warning_percent", _UTILIZATION_WARNING_THRESHOLD
    )
    critical_threshold = _threshold(
        config, "utilization_critical_percent", _UTILIZATION_CRITICAL_THRESHOLD
    )
    redis_evictions_warning = _threshold(
        config, "redis_evictions_warning", _REDIS_EVICTIONS_WARNING
    )
    redis_evictions_critical = _threshold(
        config, "redis_evictions_critical", _REDIS_EVICTIONS_CRITICAL
    )
    redis_lag_warning = _threshold(
        config, "redis_replication_lag_warning_seconds", _REDIS_REPLICATION_LAG_WARNING_SECONDS
    )
    redis_lag_critical = _threshold(
        config, "redis_replication_lag_critical_seconds", _REDIS_REPLICATION_LAG_CRITICAL_SECONDS
    )

    rows: list[dict[str, Any]] = []
    for instance in sorted(rows_by_instance):
        row = rows_by_instance[instance]
        cpu_peak = _as_float(row.get("cpu_peak_percent", 0.0))
        memory_peak = _as_float(row.get("memory_peak_percent", 0.0))
        evictions_peak = _as_float(row.get("eviction_rate_peak", 0.0))
        lag_peak = _as_float(row.get("replication_lag_peak_seconds", 0.0))
        row_status = Status.OK
        if (
            cpu_peak >= critical_threshold
            or memory_peak >= critical_threshold
            or evictions_peak >= redis_evictions_critical
            or lag_peak >= redis_lag_critical
        ):
            row_status = Status.CRITICAL
        elif (
            cpu_peak >= warning_threshold
            or memory_peak >= warning_threshold
            or evictions_peak >= redis_evictions_warning
            or lag_peak >= redis_lag_warning
        ):
            row_status = Status.WARNING
        rows.append(
            {
                "instance": instance,
                "status": row_status.value,
                "cpu_peak_percent": cpu_peak,
                "cpu_observed": bool(row.get("cpu_peak_percent_observed", False)),
                "memory_peak_percent": memory_peak,
                "memory_observed": bool(row.get("memory_peak_percent_observed", False)),
                "eviction_rate_peak": evictions_peak,
                "connected_clients_peak": _as_float(row.get("connected_clients_peak", 0.0)),
                "ops_per_second_peak": _as_float(row.get("ops_per_second_peak", 0.0)),
                "replication_lag_peak_seconds": lag_peak,
                "note": _redis_utilization_note(row),
            }
        )

    if not rows:
        errors.append("redis utilization metrics not found")
    return rows, "; ".join(errors)


def _collect_kafka_resource_utilization(
    *,
    config: EnvConfig,
    monitoring: Any,
    project: str,
    start_text: str,
    end_text: str,
    known_clusters_by_location: dict[str, list[str]] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    descriptor_prefix = "managedkafka.googleapis.com/"
    metric_map = {
        "cpu_usage": ("cpu/core_usage_time",),
        "cpu_limit": ("cpu/limit",),
        "memory_usage": ("memory/usage",),
        "memory_limit": ("memory/limit",),
        "disk_usage": ("disk/used_bytes",),
        "disk_limit": ("disk/limit",),
        "under_replicated_partitions_peak": (
            "broker/under_replicated_partitions",
            "cluster/under_replicated_partitions",
        ),
        "offline_partitions_peak": ("offline_partitions", "cluster/offline_partitions"),
        "consumer_lag_peak": ("consumer_lag", "offset_lag", "consumer/lag", "consumer_group/lag"),
    }

    rows_by_cluster: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for field_name, candidates in metric_map.items():
        try:
            metric_type = _resolve_metric_type_candidates(
                monitoring=monitoring,
                project=project,
                prefix=descriptor_prefix,
                suffixes=candidates,
            )
            if not metric_type:
                continue
            aligner = "ALIGN_MAX"
            if field_name == "cpu_usage":
                aligner = "ALIGN_RATE"
            series = _fetch_time_series(
                monitoring=monitoring,
                project=project,
                metric_type=metric_type,
                start_text=start_text,
                end_text=end_text,
                aligner=aligner,
                alignment_period="300s",
            )
            bucket: dict[str, list[float]] = {}
            for item in series:
                resource_labels = _as_dict(_as_dict(item.get("resource")).get("labels"))
                metric_labels = _as_dict(_as_dict(item.get("metric")).get("labels"))
                cluster = _kafka_metric_cluster_name(
                    resource_labels=resource_labels,
                    metric_labels=metric_labels,
                    known_clusters_by_location=known_clusters_by_location or {},
                )
                bucket.setdefault(cluster, [])
                bucket[cluster].extend(_extract_point_values(item))
            for cluster, values in bucket.items():
                stats = _summary_stats_raw(values)
                row = rows_by_cluster.setdefault(cluster, {"cluster": cluster})
                row[field_name] = stats["peak"]
        except HttpError as exc:
            errors.append(str(exc))
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))

    warning_threshold = _threshold(
        config, "utilization_warning_percent", _UTILIZATION_WARNING_THRESHOLD
    )
    critical_threshold = _threshold(
        config, "utilization_critical_percent", _UTILIZATION_CRITICAL_THRESHOLD
    )
    consumer_lag_warning = _threshold(
        config, "kafka_consumer_lag_warning", _KAFKA_CONSUMER_LAG_WARNING
    )
    consumer_lag_critical = _threshold(
        config, "kafka_consumer_lag_critical", _KAFKA_CONSUMER_LAG_CRITICAL
    )

    rows: list[dict[str, Any]] = []
    for cluster in sorted(rows_by_cluster):
        row = rows_by_cluster[cluster]
        cpu_peak = _ratio_percent(row.get("cpu_usage", 0.0), row.get("cpu_limit", 0.0))
        memory_peak = _ratio_percent(row.get("memory_usage", 0.0), row.get("memory_limit", 0.0))
        disk_peak = _ratio_percent(row.get("disk_usage", 0.0), row.get("disk_limit", 0.0))
        under_replicated = _as_float(row.get("under_replicated_partitions_peak", 0.0))
        offline = _as_float(row.get("offline_partitions_peak", 0.0))
        consumer_lag = _as_float(row.get("consumer_lag_peak", 0.0))
        reported_values = (
            cpu_peak,
            memory_peak,
            disk_peak,
            under_replicated,
            offline,
            consumer_lag,
        )
        if cluster == "unknown-cluster" and all(value == 0.0 for value in reported_values):
            continue
        row_status = Status.OK
        if (
            cpu_peak >= critical_threshold
            or memory_peak >= critical_threshold
            or disk_peak >= critical_threshold
            or offline > 0.0
            or consumer_lag >= consumer_lag_critical
        ):
            row_status = Status.CRITICAL
        elif (
            cpu_peak >= warning_threshold
            or memory_peak >= warning_threshold
            or disk_peak >= warning_threshold
            or under_replicated > 0.0
            or consumer_lag >= consumer_lag_warning
        ):
            row_status = Status.WARNING
        rows.append(
            {
                "cluster": cluster,
                "status": row_status.value,
                "broker_cpu_peak_percent": cpu_peak,
                "broker_memory_peak_percent": memory_peak,
                "broker_disk_peak_percent": disk_peak,
                "under_replicated_partitions_peak": under_replicated,
                "offline_partitions_peak": offline,
                "consumer_lag_peak": consumer_lag,
            }
        )

    if not rows:
        errors.append("kafka utilization metrics not found")
    return rows, "; ".join(errors)


def _kafka_metric_cluster_name(
    *,
    resource_labels: dict[str, Any],
    metric_labels: dict[str, Any],
    known_clusters_by_location: dict[str, list[str]],
) -> str:
    cluster = (
        str(resource_labels.get("cluster_id", "")).strip()
        or str(resource_labels.get("cluster_name", "")).strip()
        or str(metric_labels.get("cluster_id", "")).strip()
    )
    if cluster:
        return cluster

    location = (
        str(resource_labels.get("location", "")).strip()
        or str(metric_labels.get("location", "")).strip()
    )
    if location:
        location_clusters = known_clusters_by_location.get(location, [])
        if len(location_clusters) == 1:
            return location_clusters[0]

    all_known_clusters = sorted(
        {
            cluster_name
            for cluster_names in known_clusters_by_location.values()
            for cluster_name in cluster_names
        }
    )
    if len(all_known_clusters) == 1:
        return all_known_clusters[0]
    return "unknown-cluster"


def _list_managed_kafka_clusters_by_location(
    service: Callable[[], Any] | Any | None,
    project: str,
    *,
    kafka_client: Any | None = None,
    timeout_seconds: int = 60,
) -> dict[str, list[str]]:
    if service is None:
        return {}
    try:
        resolved_service = service() if callable(service) else service
        clusters = list_managed_kafka_clusters(
            resolved_service,
            project,
            kafka_client=kafka_client,
            timeout_seconds=timeout_seconds,
        )
    except Exception:  # noqa: BLE001
        return {}

    clusters_by_location: dict[str, set[str]] = {}
    for cluster in clusters:
        identity = _managed_kafka_cluster_identity(cluster)
        if identity is None:
            continue
        location, cluster_id = identity
        clusters_by_location.setdefault(location, set()).add(cluster_id)
    return {
        location: sorted(cluster_names)
        for location, cluster_names in sorted(clusters_by_location.items())
    }


def _managed_kafka_cluster_identity(cluster: dict[str, Any]) -> tuple[str, str] | None:
    name = str(cluster.get("name", "")).strip()
    if name:
        parts = name.split("/")
        try:
            location = parts[parts.index("locations") + 1]
            cluster_id = parts[parts.index("clusters") + 1]
        except (ValueError, IndexError):
            location = ""
            cluster_id = ""
        if location and cluster_id:
            return location, cluster_id

    location = str(cluster.get("location", "")).strip()
    cluster_id = (
        str(cluster.get("clusterId", "")).strip()
        or str(cluster.get("cluster_id", "")).strip()
        or str(_as_dict(cluster.get("labels")).get("name", "")).strip()
    )
    if location and cluster_id:
        return location, cluster_id
    return None


def _resolve_metric_type(
    monitoring: Any,
    project: str,
    prefix: str,
    suffix: str,
) -> str:
    descriptors = _list_metric_descriptors(monitoring, project, prefix)
    suffix_norm = suffix.lower()
    for metric_type in descriptors:
        normalized = metric_type.lower()
        if normalized.endswith(f"/{suffix_norm}") or normalized.endswith(suffix_norm):
            return metric_type
    return ""


def _resolve_metric_type_candidates(
    monitoring: Any,
    project: str,
    prefix: str,
    suffixes: tuple[str, ...],
) -> str:
    descriptors = _list_metric_descriptors(monitoring, project, prefix)
    normalized_candidates = tuple(suffix.lower() for suffix in suffixes)
    for suffix in normalized_candidates:
        for metric_type in descriptors:
            normalized = metric_type.lower()
            if normalized.endswith(f"/{suffix}") or normalized.endswith(suffix):
                return metric_type
    return ""


def _resolve_metric_type_candidate_list(
    monitoring: Any,
    project: str,
    prefix: str,
    suffixes: tuple[str, ...],
) -> list[str]:
    descriptors = _list_metric_descriptors(monitoring, project, prefix)
    normalized_candidates = tuple(suffix.lower() for suffix in suffixes)
    matches: list[str] = []
    for suffix in normalized_candidates:
        for metric_type in descriptors:
            normalized = metric_type.lower()
            if normalized.endswith(f"/{suffix}") or normalized.endswith(suffix):
                if metric_type not in matches:
                    matches.append(metric_type)
                break
    return matches


def _list_metric_descriptors(monitoring: Any, project: str, prefix: str) -> list[str]:
    service: Any | None = None
    metric_client = _monitoring_metric_client(monitoring)
    if metric_client is not None:
        try:
            return _list_metric_descriptors_with_client(
                metric_client,
                project=project,
                prefix=prefix,
                timeout_seconds=_monitoring_timeout_seconds(monitoring),
            )
        except Exception:  # noqa: BLE001
            service = _monitoring_service(monitoring)
    return _list_metric_descriptors_with_service(
        service if service is not None else _monitoring_service(monitoring),
        project=project,
        prefix=prefix,
    )


def _list_metric_descriptors_with_client(
    metric_client: Any,
    *,
    project: str,
    prefix: str,
    timeout_seconds: int,
) -> list[str]:
    return list_monitoring_metric_descriptors(
        metric_client,
        project=project,
        prefix=prefix,
        timeout_seconds=timeout_seconds,
    )


def _list_metric_descriptors_with_service(
    monitoring: Any,
    *,
    project: str,
    prefix: str,
) -> list[str]:
    rows: list[str] = []
    page_token = ""
    while True:
        kwargs: dict[str, Any] = {
            "name": f"projects/{project}",
            "filter": f'metric.type = starts_with("{prefix}")',
            "pageSize": 200,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        response = monitoring.projects().metricDescriptors().list(**kwargs).execute()
        for item in _as_dict_list(response.get("metricDescriptors")):
            metric_type = str(item.get("type", "")).strip()
            if metric_type:
                rows.append(metric_type)
        page_token = str(response.get("nextPageToken", "")).strip()
        if not page_token or len(rows) >= 2000:
            return rows[:2000]


def _fetch_time_series(
    monitoring: Any,
    project: str,
    metric_type: str,
    start_text: str,
    end_text: str,
    aligner: str = "ALIGN_MEAN",
    alignment_period: str = "3600s",
    filter_suffix: str = "",
    reducer: str = "",
    group_by_fields: list[str] | None = None,
    max_series: int = _TIME_SERIES_MAX_SERIES,
    max_pages: int = _TIME_SERIES_MAX_PAGES,
    page_size: int = _TIME_SERIES_PAGE_SIZE,
) -> list[dict[str, Any]]:
    series_limit = max(1, max_series)
    page_limit = max(1, max_pages)
    filter_text = f'metric.type="{metric_type}"'
    suffix = filter_suffix.strip()
    if suffix:
        filter_text = f"{filter_text} AND {suffix}"

    service: Any | None = None
    metric_client = _monitoring_metric_client(monitoring)
    if metric_client is not None:
        try:
            return _fetch_time_series_with_client(
                metric_client,
                project=project,
                filter_text=filter_text,
                start_text=start_text,
                end_text=end_text,
                aligner=aligner,
                alignment_period=alignment_period,
                reducer=reducer,
                group_by_fields=group_by_fields,
                page_size=page_size,
                max_pages=page_limit,
                max_series=series_limit,
                timeout_seconds=_monitoring_timeout_seconds(monitoring),
            )
        except Exception:  # noqa: BLE001
            service = _monitoring_service(monitoring)
    return _fetch_time_series_with_service(
        service if service is not None else _monitoring_service(monitoring),
        project=project,
        filter_text=filter_text,
        start_text=start_text,
        end_text=end_text,
        aligner=aligner,
        alignment_period=alignment_period,
        reducer=reducer,
        group_by_fields=group_by_fields,
        page_size=page_size,
        max_pages=page_limit,
        max_series=series_limit,
    )


def _fetch_time_series_with_client(
    metric_client: Any,
    *,
    project: str,
    filter_text: str,
    start_text: str,
    end_text: str,
    aligner: str,
    alignment_period: str,
    reducer: str,
    group_by_fields: list[str] | None,
    page_size: int,
    max_pages: int,
    max_series: int,
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    return list_monitoring_time_series(
        metric_client,
        project=project,
        filter_text=filter_text,
        start_text=start_text,
        end_text=end_text,
        aligner=aligner,
        alignment_period=alignment_period,
        reducer=reducer,
        group_by_fields=group_by_fields,
        page_size=page_size,
        max_pages=max_pages,
        max_series=max_series,
        timeout_seconds=timeout_seconds,
    )


def _fetch_time_series_with_service(
    monitoring: Any,
    *,
    project: str,
    filter_text: str,
    start_text: str,
    end_text: str,
    aligner: str,
    alignment_period: str,
    reducer: str,
    group_by_fields: list[str] | None,
    page_size: int,
    max_pages: int,
    max_series: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page_token = ""
    page_count = 0
    while True:
        kwargs: dict[str, Any] = {
            "name": f"projects/{project}",
            "filter": filter_text,
            "interval_startTime": start_text,
            "interval_endTime": end_text,
            "view": "FULL",
            "pageSize": max(1, page_size),
            "aggregation_alignmentPeriod": alignment_period,
            "aggregation_perSeriesAligner": aligner,
        }
        if reducer:
            kwargs["aggregation_crossSeriesReducer"] = reducer
        if group_by_fields:
            kwargs["aggregation_groupByFields"] = group_by_fields
        if page_token:
            kwargs["pageToken"] = page_token
        response = monitoring.projects().timeSeries().list(**kwargs).execute()
        page_count += 1
        rows.extend(_as_dict_list(response.get("timeSeries")))
        page_token = str(response.get("nextPageToken", "")).strip()
        if not page_token or len(rows) >= max_series or page_count >= max_pages:
            return rows[:max_series]


def _monitoring_service(monitoring: Any) -> Any:
    if isinstance(monitoring, _MonitoringClients):
        service = monitoring.service
    else:
        service = monitoring
    if callable(service):
        return service()
    return service


def _monitoring_metric_client(monitoring: Any) -> Any | None:
    if isinstance(monitoring, _MonitoringClients):
        return monitoring.metric_client
    return None


def _monitoring_query_client(monitoring: Any) -> Any | None:
    if isinstance(monitoring, _MonitoringClients):
        return monitoring.query_client
    return None


def _monitoring_timeout_seconds(monitoring: Any) -> int:
    if isinstance(monitoring, _MonitoringClients):
        return max(1, monitoring.timeout_seconds)
    return 60


def _list_sql_instances(sqladmin: Any, project: str) -> list[dict[str, Any]]:
    return list_cloud_sql_instances(sqladmin, project)


def _list_non_gke_instances(
    compute: Any | Callable[[], Any] | None,
    project: str,
    *,
    instances_client: Any | None = None,
    timeout_seconds: int = 60,
) -> list[dict[str, str]]:
    if instances_client is not None:
        try:
            return _list_non_gke_instances_with_client(
                instances_client,
                project,
                timeout_seconds,
            )
        except Exception:  # noqa: BLE001
            pass
    return _list_non_gke_instances_with_service(compute, project)


def _list_non_gke_instances_with_client(
    client: Any,
    project: str,
    timeout_seconds: int,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for _zone, payload in client.aggregated_list(
        request={"project": project, "max_results": 500},
        timeout=max(1, timeout_seconds),
    ):
        rows.extend(_non_gke_instance_rows(protobuf_to_dict(payload).get("instances")))
    return rows


def _list_non_gke_instances_with_service(
    compute: Any | Callable[[], Any] | None, project: str
) -> list[dict[str, str]]:
    service = resolve_fallback_service(compute, None, "Compute discovery service unavailable")
    rows: list[dict[str, str]] = []
    page_token = ""
    while True:
        kwargs: dict[str, Any] = {"project": project, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        response = service.instances().aggregatedList(**kwargs).execute()
        for _zone, payload in _as_dict(response.get("items")).items():
            if not isinstance(payload, dict):
                continue
            rows.extend(_non_gke_instance_rows(payload.get("instances")))
        page_token = str(response.get("nextPageToken", "")).strip()
        if not page_token:
            return rows


def _non_gke_instance_rows(value: Any) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for instance in _as_dict_list(value):
        name = str(instance.get("name", ""))
        if name.startswith("gke-"):
            continue
        rows.append(
            {
                "id": str(instance.get("id", "")),
                "name": name,
            }
        )
    return rows


def _extract_point_values(series: dict[str, Any]) -> list[float]:
    values: list[float] = []
    for point in _as_dict_list(series.get("points")):
        value_block = _as_dict(point.get("value"))
        candidate = value_block.get("doubleValue")
        if candidate is None:
            candidate = value_block.get("int64Value")
        if isinstance(candidate, (int, float)):
            values.append(float(candidate))
        elif isinstance(candidate, str):
            try:
                values.append(float(candidate))
            except ValueError:
                continue
    return values


def _extract_timed_point_values(series: dict[str, Any]) -> list[tuple[str, float]]:
    values: list[tuple[str, float]] = []
    for point in _as_dict_list(series.get("points")):
        interval = _as_dict(point.get("interval"))
        timestamp = str(interval.get("endTime", "") or interval.get("startTime", "")).strip()
        if not timestamp:
            timestamp = str(len(values))
        value_block = _as_dict(point.get("value"))
        candidate = value_block.get("doubleValue")
        if candidate is None:
            candidate = value_block.get("int64Value")
        if isinstance(candidate, (int, float)):
            values.append((timestamp, float(candidate)))
        elif isinstance(candidate, str):
            try:
                values.append((timestamp, float(candidate)))
            except ValueError:
                continue
    return values


def _summary_stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {"avg": 0.0, "min": 0.0, "p95": 0.0, "peak": 0.0}
    scaled = [item * 100.0 for item in values]
    scaled.sort()
    avg = sum(scaled) / len(scaled)
    minimum = scaled[0]
    peak = scaled[-1]
    index = max(0, min(len(scaled) - 1, math.ceil(len(scaled) * 0.95) - 1))
    p95 = scaled[index]
    return {"avg": avg, "min": minimum, "p95": p95, "peak": peak}


def _idle_capacity_percent(cpu_min_percent: float) -> float:
    return max(0.0, min(100.0, 100.0 - cpu_min_percent))


def _trend_window_days(config: EnvConfig) -> int:
    return max(1, min(365, config.time_windows.trend_days))


def _threshold(config: EnvConfig, key: str, default: float) -> float:
    value = config.services.utilization_thresholds.get(key)
    if value is None:
        return default
    return float(value)


def _summary_stats_raw(values: list[float]) -> dict[str, float]:
    if not values:
        return {"avg": 0.0, "p95": 0.0, "peak": 0.0}
    ordered = sorted(values)
    avg = sum(ordered) / len(ordered)
    peak = ordered[-1]
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1))
    p95 = ordered[index]
    return {"avg": avg, "p95": p95, "peak": peak}


def _peak_aggregate(values_by_time: dict[str, float]) -> float:
    if not values_by_time:
        return 0.0
    return max(values_by_time.values())


def _peak_from_series(series: list[dict[str, Any]]) -> float:
    return max((value for item in series for value in _extract_point_values(item)), default=0.0)


def _ratio_percent(numerator: Any, denominator: Any) -> float:
    denominator_value = _as_float(denominator)
    if denominator_value <= 0.0:
        return 0.0
    return (_as_float(numerator) / denominator_value) * 100.0


def _redis_utilization_note(row: dict[str, Any]) -> str:
    notes: list[str] = []
    if not bool(row.get("cpu_peak_percent_observed", False)):
        notes.append("Redis CPU utilization metric not returned by Cloud Monitoring.")
    if not bool(row.get("memory_peak_percent_observed", False)):
        notes.append("Redis memory utilization metric not returned by Cloud Monitoring.")
    return " ".join(notes)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


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


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []
