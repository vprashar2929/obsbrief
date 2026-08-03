from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
from googleapiclient.errors import HttpError

from opsbrief.cluster_discovery import resolve_clusters
from opsbrief.config import EnvConfig
from opsbrief.gcp_api import (
    build_cloud_sql_admin_service,
    build_gke_backup_client,
    build_service,
    list_cloud_sql_backup_runs,
    list_cloud_sql_instances,
    protobuf_to_dict,
)
from opsbrief.gcp_auth import AuthMode, get_auth_bundle
from opsbrief.models import CheckResult, Status, now_utc_iso
from opsbrief.status import classify_http_error, max_status


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

    projects = _candidate_projects(config)
    if not projects:
        return CheckResult(
            collector="backup",
            status=Status.SKIPPED_CONFIG,
            summary="No projects configured for backup collector",
            details={},
            errors=[],
            started_at=started_at,
            finished_at=now_utc_iso(),
        )

    backup_policy = config.report_expectations.backup_policy
    if backup_policy.status == "out_of_scope":
        reason = (
            backup_policy.reason.strip()
            or "Backup policy assessment is out of scope for this environment."
        )
        return CheckResult(
            collector="backup",
            status=Status.OK,
            summary="Backup policy assessment is out of scope for this environment",
            details={
                "policy": {
                    "status": backup_policy.status,
                    "reason": reason,
                },
                "gke_backup": [
                    {
                        "status": Status.SKIPPED_CONFIG.value,
                        "reason": reason,
                        "plan_count": 0,
                    }
                ],
                "region_pairs_checked": [],
                "cloud_sql_backup": [
                    {
                        "status": Status.SKIPPED_CONFIG.value,
                        "reason": reason,
                        "instance_count": 0,
                        "instances": [],
                    }
                ],
                "elasticsearch_backup": [
                    {
                        "status": Status.SKIPPED_CONFIG.value,
                        "reason": reason,
                    }
                ],
            },
            errors=[],
            started_at=started_at,
            finished_at=now_utc_iso(),
        )

    try:
        auth = get_auth_bundle(
            auth_mode=auth_mode, impersonate_service_account=impersonate_service_account
        )
        try:
            gke_backup_client = build_gke_backup_client(auth)
        except Exception:  # noqa: BLE001
            gke_backup_client = None
        sqladmin = build_cloud_sql_admin_service(auth, timeout_seconds)
        try:
            container = build_service(auth, "container", "v1", timeout_seconds)
        except Exception:  # noqa: BLE001
            container = None
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            collector="backup",
            status=Status.FAILED,
            summary="Unable to initialize backup-related API clients",
            details={},
            errors=[str(exc)],
            started_at=started_at,
            finished_at=now_utc_iso(),
        )

    gkebackup: Any | None = None

    def gkebackup_fallback() -> Any:
        nonlocal gkebackup
        if gkebackup is None:
            gkebackup = build_service(auth, "gkebackup", "v1", timeout_seconds)
        return gkebackup

    region_pairs, gke_backup_expected = _resolve_backup_region_pairs(
        config=config,
        projects=projects,
        container_service=container,
    )

    gke_backup_rows: list[dict[str, Any]] = []
    if not gke_backup_expected:
        gke_backup_rows.append(
            {
                "status": Status.SKIPPED_CONFIG.value,
                "reason": "No GKE clusters in scope; no backup policy applies to this component.",
                "plan_count": 0,
            }
        )
    else:
        for project, region in sorted(region_pairs):
            parent = f"projects/{project}/locations/{region}"
            try:
                plans = _list_gke_backup_plans(
                    None,
                    parent,
                    backup_client=gke_backup_client,
                    fallback_service=gkebackup_fallback,
                    timeout_seconds=timeout_seconds,
                )
            except HttpError as exc:
                failure_status = classify_http_error(exc)
                gke_backup_rows.append(
                    {
                        "project": project,
                        "region": region,
                        "status": failure_status.value,
                        "error": str(exc),
                    }
                )
                errors.append(f"{project}/{region}: {exc}")
                status = max_status(status, failure_status)
                continue
            except Exception as exc:  # noqa: BLE001
                gke_backup_rows.append(
                    {
                        "project": project,
                        "region": region,
                        "status": Status.FAILED.value,
                        "error": str(exc),
                    }
                )
                errors.append(f"{project}/{region}: {exc}")
                status = max_status(status, Status.FAILED)
                continue

            row_status = Status.OK
            row_reason = ""
            if len(plans) == 0:
                row_status = Status.WARNING
                row_reason = "Backups expected but none detected."
                status = max_status(status, Status.WARNING)

            gke_backup_rows.append(
                {
                    "project": project,
                    "region": region,
                    "status": row_status.value,
                    "reason": row_reason,
                    "plan_count": len(plans),
                    "plans": [
                        {
                            "name": plan.get("name", ""),
                            "state": plan.get("state", ""),
                            "cron": (plan.get("backupSchedule") or {}).get("cronSchedule", ""),
                            "retention_days": (plan.get("retentionPolicy") or {}).get(
                                "backupRetainDays", None
                            ),
                        }
                        for plan in plans
                    ],
                }
            )

    sql_rows: list[dict[str, Any]] = []
    for project in projects:
        try:
            instances = _list_sql_instances(sqladmin, project)
        except HttpError as exc:
            failure_status = classify_http_error(exc)
            sql_rows.append({"project": project, "status": failure_status.value, "error": str(exc)})
            errors.append(f"{project}: {exc}")
            status = max_status(status, failure_status)
            continue
        except Exception as exc:  # noqa: BLE001
            sql_rows.append({"project": project, "status": Status.FAILED.value, "error": str(exc)})
            errors.append(f"{project}: {exc}")
            status = max_status(status, Status.FAILED)
            continue

        instance_rows: list[dict[str, Any]] = []
        for instance in instances:
            instance_name = instance.get("name", "")
            latest_backup: dict[str, Any] | None = None
            try:
                latest_backup = _latest_sql_backup_run(sqladmin, project, instance_name)
            except HttpError as exc:
                errors.append(f"{project}/{instance_name}: unable to list backup runs: {exc}")
                status = max_status(status, classify_http_error(exc))

            backup_cfg = (instance.get("settings") or {}).get("backupConfiguration") or {}
            instance_rows.append(
                {
                    "name": instance_name,
                    "state": instance.get("state", ""),
                    "database_version": instance.get("databaseVersion", ""),
                    "backup_enabled": backup_cfg.get("enabled", False),
                    "point_in_time_recovery_enabled": backup_cfg.get(
                        "pointInTimeRecoveryEnabled", False
                    ),
                    "latest_backup": latest_backup,
                }
            )

        enabled_backups = sum(
            1 for instance in instance_rows if instance.get("backup_enabled", False)
        )
        if not instances:
            sql_status = Status.SKIPPED_CONFIG
            sql_reason = "No Cloud SQL instances in scope for backup checks."
        elif enabled_backups == 0:
            sql_status = Status.WARNING
            sql_reason = "Backups expected but none detected."
            status = max_status(status, Status.WARNING)
        else:
            sql_status = Status.OK
            sql_reason = ""

        sql_rows.append(
            {
                "project": project,
                "status": sql_status.value,
                "reason": sql_reason,
                "instance_count": len(instances),
                "instances": instance_rows,
            }
        )

    elastic_rows: list[dict[str, Any]] = []
    elastic_targets = _as_dict_list(config.services.elasticsearch_backup_checks)
    if elastic_targets:
        for target in elastic_targets:
            try:
                row, row_status = _check_elasticsearch_backup_target(target, timeout_seconds)
                elastic_rows.append(row)
                status = max_status(status, row_status)
            except Exception as exc:  # noqa: BLE001
                elastic_rows.append(
                    {
                        "name": str(target.get("name", "")),
                        "status": Status.FAILED.value,
                        "error": str(exc),
                    }
                )
                errors.append(f"elasticsearch/{target.get('name', 'unknown')}: {exc}")
                status = max_status(status, Status.FAILED)
    else:
        elastic_rows.append(
            {
                "status": Status.SKIPPED_CONFIG.value,
                "reason": "services.elasticsearch_backup_checks not configured",
            }
        )
    gke_total_plans = sum(
        row.get("plan_count", 0) for row in gke_backup_rows if "plan_count" in row
    )
    sql_enabled_count = sum(
        1
        for row in sql_rows
        for instance in row.get("instances", [])
        if instance.get("backup_enabled", False)
    )
    backup_expected_without_detection = sum(
        1
        for row in gke_backup_rows
        if str(row.get("status", "")) == Status.WARNING.value
        and str(row.get("reason", "")).startswith("Backups expected")
    ) + sum(
        1
        for row in sql_rows
        if str(row.get("status", "")) == Status.WARNING.value
        and str(row.get("reason", "")).startswith("Backups expected")
    )

    summary = (
        f"GKE backup plans={gke_total_plans}, "
        f"Cloud SQL instances with backup enabled={sql_enabled_count}, "
        f"expected_without_detection={backup_expected_without_detection}"
    )
    return CheckResult(
        collector="backup",
        status=status,
        summary=summary,
        details={
            "policy": {
                "status": config.report_expectations.backup_policy.status,
                "reason": config.report_expectations.backup_policy.reason,
            },
            "gke_backup": gke_backup_rows,
            "region_pairs_checked": [
                {
                    "project": project,
                    "region": region,
                }
                for project, region in sorted(region_pairs)
            ],
            "cloud_sql_backup": sql_rows,
            "elasticsearch_backup": elastic_rows,
        },
        errors=errors,
        started_at=started_at,
        finished_at=now_utc_iso(),
    )


def _candidate_projects(config: EnvConfig) -> list[str]:
    unique: list[str] = []
    for project in config.projects.values():
        if project and project not in unique:
            unique.append(project)
    for cluster in config.clusters:
        if cluster.project and cluster.project not in unique:
            unique.append(cluster.project)
    return unique


def _resolve_backup_region_pairs(
    config: EnvConfig,
    projects: list[str],
    container_service: Any | None,
) -> tuple[set[tuple[str, str]], bool]:
    pairs: set[tuple[str, str]] = {
        (cluster.project, cluster.region)
        for cluster in config.clusters
        if cluster.project and cluster.region
    }

    if container_service is not None:
        try:
            discovered = resolve_clusters(config, container_service)
        except Exception:  # noqa: BLE001
            discovered = []
        for cluster in discovered:
            if cluster.project and cluster.region:
                pairs.add((cluster.project, cluster.region))

    if pairs:
        return pairs, True

    fallback_pairs: set[tuple[str, str]] = set()
    for project in projects:
        if project and config.default_region:
            fallback_pairs.add((project, config.default_region))
    return fallback_pairs, False


def _list_gke_backup_plans(
    service: Any | None,
    parent: str,
    *,
    backup_client: Any | None = None,
    fallback_service: Callable[[], Any] | None = None,
    timeout_seconds: int = 60,
) -> list[dict[str, Any]]:
    if backup_client is not None:
        try:
            return [
                protobuf_to_dict(plan)
                for plan in backup_client.list_backup_plans(
                    parent=parent,
                    timeout=max(1, timeout_seconds),
                )
            ]
        except Exception:  # noqa: BLE001
            pass

    if service is None:
        if fallback_service is None:
            raise RuntimeError("GKE Backup discovery service unavailable")
        service = fallback_service()

    request = service.projects().locations().backupPlans().list(parent=parent, pageSize=100)
    collected: list[dict[str, Any]] = []
    while request is not None:
        response = request.execute()
        collected.extend(response.get("backupPlans", []))
        request = service.projects().locations().backupPlans().list_next(request, response)
    return collected


def _list_sql_instances(service: Any, project: str) -> list[dict[str, Any]]:
    return list_cloud_sql_instances(service, project)


def _latest_sql_backup_run(service: Any, project: str, instance: str) -> dict[str, Any] | None:
    items = list_cloud_sql_backup_runs(
        service,
        project=project,
        instance=instance,
        max_results=1,
    )
    if not items:
        return None
    run = items[0]
    return {
        "id": run.get("id", ""),
        "status": run.get("status", ""),
        "type": run.get("type", ""),
        "start_time": run.get("startTime", ""),
        "end_time": run.get("endTime", ""),
    }


def _check_elasticsearch_backup_target(
    target: dict[str, Any], timeout_seconds: int
) -> tuple[dict[str, Any], Status]:
    name = str(target.get("name", "")).strip() or "unnamed"
    snapshot_url = str(target.get("snapshot_url", "")).strip()
    ilm_url = str(target.get("ilm_url", "")).strip()
    headers_raw = target.get("headers", {})
    headers = headers_raw if isinstance(headers_raw, dict) else {}

    if not snapshot_url:
        return (
            {
                "name": name,
                "status": Status.SKIPPED_CONFIG.value,
                "reason": "snapshot_url not configured",
            },
            Status.SKIPPED_CONFIG,
        )

    try:
        snapshot_status, snapshot_payload = _http_json_get(
            url=snapshot_url, headers=headers, timeout_seconds=timeout_seconds
        )
    except httpx.RequestError as exc:
        return (
            {
                "name": name,
                "snapshot_url": snapshot_url,
                "status": Status.SKIPPED_NETWORK.value,
                "reason": str(exc),
            },
            Status.SKIPPED_NETWORK,
        )

    result: dict[str, Any] = {
        "name": name,
        "snapshot_url": snapshot_url,
        "snapshot_http_status": snapshot_status,
        "snapshot_payload_sample": snapshot_payload,
    }

    derived_status = Status.OK if snapshot_status == 200 else Status.WARNING
    if ilm_url:
        result["ilm_url"] = ilm_url
        try:
            ilm_status, ilm_payload = _http_json_get(
                url=ilm_url, headers=headers, timeout_seconds=timeout_seconds
            )
            result["ilm_http_status"] = ilm_status
            result["ilm_payload_sample"] = ilm_payload
            if ilm_status != 200:
                derived_status = max_status(derived_status, Status.WARNING)
        except httpx.RequestError as exc:
            result["ilm_http_status"] = "n/a"
            result["ilm_payload_sample"] = {}
            result["reason"] = f"ilm check unreachable: {exc}"
            derived_status = max_status(derived_status, Status.SKIPPED_NETWORK)

    result["status"] = derived_status.value
    return result, derived_status


def _http_json_get(url: str, headers: dict[str, Any], timeout_seconds: int) -> tuple[int, Any]:
    response = httpx.get(
        url,
        headers={str(key): str(value) for key, value in headers.items()},
        timeout=max(1, timeout_seconds),
        follow_redirects=True,
    )
    return response.status_code, _safe_json_sample(response.text)


def _safe_json_sample(raw: str, max_len: int = 1000) -> Any:
    payload = raw.strip()
    if not payload:
        return {}
    try:
        parsed = json.loads(payload)
        if isinstance(parsed, (dict, list)):
            return parsed
    except json.JSONDecodeError:
        pass
    return payload[:max_len]


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []
