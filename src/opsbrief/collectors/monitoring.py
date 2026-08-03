from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from googleapiclient.errors import HttpError

from opsbrief.config import EnvConfig
from opsbrief.gcp_api import (
    build_alert_policy_service_client,
    build_notification_channel_service_client,
    build_service,
    collect_paged,
    protobuf_to_dict,
    resolve_fallback_service,
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
    project_summaries: list[dict[str, Any]] = []
    now = datetime.now(tz=UTC)
    cutoff = now - timedelta(days=7)

    projects = _candidate_projects(config)
    if not projects:
        return CheckResult(
            collector="monitoring",
            status=Status.SKIPPED_CONFIG,
            summary="No projects configured for monitoring collector",
            details={"projects": []},
            errors=[],
            started_at=started_at,
            finished_at=now_utc_iso(),
        )

    try:
        auth = get_auth_bundle(
            auth_mode=auth_mode, impersonate_service_account=impersonate_service_account
        )
        monitoring_service = None

        def monitoring_service_fallback() -> Any:
            nonlocal monitoring_service
            if monitoring_service is None:
                monitoring_service = build_service(auth, "monitoring", "v3", timeout_seconds)
            return monitoring_service

        alert_policy_client = None
        notification_channel_client = None
        try:
            alert_policy_client = build_alert_policy_service_client(auth)
        except Exception:  # noqa: BLE001
            alert_policy_client = None
        try:
            notification_channel_client = build_notification_channel_service_client(auth)
        except Exception:  # noqa: BLE001
            notification_channel_client = None
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            collector="monitoring",
            status=Status.FAILED,
            summary="Unable to initialize Monitoring API client",
            details={},
            errors=[str(exc)],
            started_at=started_at,
            finished_at=now_utc_iso(),
        )

    for project in projects:
        try:
            policies = _list_alert_policies(
                monitoring_service_fallback,
                project,
                alert_policy_client=alert_policy_client,
                timeout_seconds=timeout_seconds,
            )
            channels = _list_notification_channels(
                monitoring_service_fallback,
                project,
                notification_channel_client=notification_channel_client,
                timeout_seconds=timeout_seconds,
            )
            alerts, alerts_api_available, alerts_api_error = _list_alerts(
                monitoring_service_fallback, project, limit=250
            )
        except HttpError as exc:
            failure_status = classify_http_error(exc)
            project_summaries.append(
                {"project": project, "status": failure_status.value, "error": str(exc)}
            )
            errors.append(f"{project}: {exc}")
            status = max_status(status, failure_status)
            continue
        except Exception as exc:  # noqa: BLE001
            project_summaries.append(
                {"project": project, "status": Status.FAILED.value, "error": str(exc)}
            )
            errors.append(f"{project}: {exc}")
            status = max_status(status, Status.FAILED)
            continue

        enabled = [p for p in policies if p.get("enabled", True)]
        disabled = [p for p in policies if not p.get("enabled", True)]
        channel_summary = _notification_channel_summary(policies, channels)
        alert_summary = _alert_summary(alerts, cutoff)
        alert_summary["api_available"] = alerts_api_available
        if alerts_api_error:
            alert_summary["error"] = alerts_api_error
        if alert_summary["open_alerts"] > 0:
            status = max_status(status, Status.WARNING)
        if channel_summary["missing_channels"] > 0:
            status = max_status(status, Status.WARNING)

        project_status = Status.OK
        if alert_summary["open_alerts"] > 0:
            project_status = max_status(project_status, Status.WARNING)
        if channel_summary["missing_channels"] > 0:
            project_status = max_status(project_status, Status.WARNING)

        project_summaries.append(
            {
                "project": project,
                "status": project_status.value,
                "alert_policy_total": len(policies),
                "alert_policy_enabled": len(enabled),
                "alert_policy_disabled": len(disabled),
                "sample_policies": [
                    {
                        "name": p.get("name", ""),
                        "display_name": p.get("displayName", ""),
                        "enabled": p.get("enabled", True),
                    }
                    for p in policies[:10]
                ],
                "notification_channels": channel_summary,
                "alerts": alert_summary,
            }
        )

    summary = f"Collected monitoring policy summary for {len(project_summaries)} project(s)"
    return CheckResult(
        collector="monitoring",
        status=status,
        summary=summary,
        details={"projects": project_summaries},
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


def _list_alert_policies(
    service: object,
    project: str,
    *,
    alert_policy_client: Any | None = None,
    timeout_seconds: int = 60,
) -> list[dict[str, Any]]:
    client_error: Exception | None = None
    if alert_policy_client is not None:
        try:
            return _list_alert_policies_with_client(
                alert_policy_client,
                project,
                timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            client_error = exc
    service = resolve_fallback_service(
        service,
        client_error,
        "Monitoring discovery service unavailable",
    )
    return _list_alert_policies_with_service(service, project)


def _list_alert_policies_with_client(
    client: Any,
    project: str,
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    response = client.list_alert_policies(
        name=f"projects/{project}",
        timeout=max(1, timeout_seconds),
    )
    return [protobuf_to_dict(item) for item in response]


def _list_alert_policies_with_service(service: Any, project: str) -> list[dict[str, Any]]:
    request = service.projects().alertPolicies().list(name=f"projects/{project}", pageSize=100)
    return collect_paged(
        request,
        item_key="alertPolicies",
        next_request=lambda previous_request, previous_response: (
            service.projects()
            .alertPolicies()
            .list_next(
                previous_request=previous_request,
                previous_response=previous_response,
            )
        ),
    )


def _list_notification_channels(
    service: object,
    project: str,
    *,
    notification_channel_client: Any | None = None,
    timeout_seconds: int = 60,
) -> list[dict[str, Any]]:
    client_error: Exception | None = None
    if notification_channel_client is not None:
        try:
            return _list_notification_channels_with_client(
                notification_channel_client,
                project,
                timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            client_error = exc
    service = resolve_fallback_service(
        service,
        client_error,
        "Monitoring discovery service unavailable",
    )
    return _list_notification_channels_with_service(service, project)


def _list_notification_channels_with_client(
    client: Any,
    project: str,
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    response = client.list_notification_channels(
        name=f"projects/{project}",
        timeout=max(1, timeout_seconds),
    )
    return [protobuf_to_dict(item) for item in response]


def _list_notification_channels_with_service(service: Any, project: str) -> list[dict[str, Any]]:
    request = (
        service.projects().notificationChannels().list(name=f"projects/{project}", pageSize=200)
    )
    return collect_paged(
        request,
        item_key="notificationChannels",
        next_request=lambda previous_request, previous_response: (
            service.projects()
            .notificationChannels()
            .list_next(
                previous_request=previous_request,
                previous_response=previous_response,
            )
        ),
    )


def _list_alerts(
    service: Any,
    project: str,
    limit: int,
) -> tuple[list[dict[str, Any]], bool, str]:
    try:
        resolved_service = resolve_fallback_service(
            service,
            None,
            "Monitoring discovery service unavailable",
        )
    except Exception as exc:  # noqa: BLE001
        return [], False, str(exc)

    try:
        request = (
            resolved_service.projects()
            .alerts()
            .list(
                parent=f"projects/{project}",
                pageSize=min(limit, 1000),
            )
        )
    except AttributeError:
        return [], False, "alerts endpoint unavailable in API discovery document"

    collected: list[dict[str, Any]] = []
    while request is not None and len(collected) < limit:
        try:
            response = request.execute()
        except HttpError as exc:
            code = getattr(exc.resp, "status", 0)
            if code in (400, 403, 404):
                return [], False, str(exc)
            raise
        collected.extend(response.get("alerts", []))
        if len(collected) >= limit:
            break
        request = (
            resolved_service.projects()
            .alerts()
            .list_next(previous_request=request, previous_response=response)
        )
    return collected[:limit], True, ""


def _notification_channel_summary(
    policies: list[dict[str, Any]], channels: list[dict[str, Any]]
) -> dict[str, Any]:
    lookup = {item.get("name", ""): item for item in channels if item.get("name")}
    referenced: list[str] = []
    for policy in policies:
        refs = policy.get("notificationChannels", [])
        for channel_name in refs:
            if channel_name not in referenced:
                referenced.append(channel_name)

    missing = [name for name in referenced if name not in lookup]
    resolved = [lookup[name] for name in referenced if name in lookup]
    disabled = [item for item in resolved if not item.get("enabled", True)]
    unverified = [
        item
        for item in resolved
        if item.get("verificationStatus", "VERIFICATION_STATUS_UNSPECIFIED")
        not in (
            "VERIFIED",
            "VERIFICATION_STATUS_UNSPECIFIED",
        )
    ]
    by_type: dict[str, int] = {}
    for item in resolved:
        channel_type = item.get("type", "unknown")
        by_type[channel_type] = by_type.get(channel_type, 0) + 1

    return {
        "referenced_channels": len(referenced),
        "resolved_channels": len(resolved),
        "missing_channels": len(missing),
        "disabled_channels": len(disabled),
        "unverified_channels": len(unverified),
        "channels_by_type": by_type,
        "missing_channel_names": missing[:10],
        "sample_channels": [
            {
                "name": item.get("name", ""),
                "display_name": item.get("displayName", ""),
                "type": item.get("type", ""),
                "enabled": item.get("enabled", True),
                "verification_status": item.get("verificationStatus", ""),
            }
            for item in resolved[:10]
        ],
    }


def _alert_summary(alerts: list[dict[str, Any]], cutoff: datetime) -> dict[str, Any]:
    open_alerts = 0
    closed_alerts = 0
    opened_last_7d = 0
    closed_last_7d = 0
    policy_counts: dict[str, int] = {}
    sample_open: list[dict[str, str]] = []

    for alert in alerts:
        state = alert.get("state", "")
        close_time = _parse_rfc3339(alert.get("closeTime", ""))
        open_time = _parse_rfc3339(alert.get("openTime", ""))
        is_open = state == "OPEN" or close_time is None
        if is_open:
            open_alerts += 1
            if len(sample_open) < 10:
                policy_name = (
                    (alert.get("policy") or {}).get("displayName")
                    or (alert.get("policy") or {}).get("name")
                    or ""
                )
                sample_open.append(
                    {
                        "name": alert.get("name", ""),
                        "open_time": alert.get("openTime", ""),
                        "policy": policy_name,
                        "state": state,
                    }
                )
        else:
            closed_alerts += 1

        if open_time is not None and open_time >= cutoff:
            opened_last_7d += 1
        if close_time is not None and close_time >= cutoff:
            closed_last_7d += 1

        policy_name = (
            (alert.get("policy") or {}).get("displayName")
            or (alert.get("policy") or {}).get("name")
            or "unknown-policy"
        )
        policy_counts[policy_name] = policy_counts.get(policy_name, 0) + 1

    policy_hotspots = sorted(policy_counts.items(), key=lambda item: item[1], reverse=True)
    return {
        "sample_size": len(alerts),
        "open_alerts": open_alerts,
        "closed_alerts": closed_alerts,
        "opened_last_7d": opened_last_7d,
        "closed_last_7d": closed_last_7d,
        "top_policies": [
            {"policy": policy, "count": count} for policy, count in policy_hotspots[:10]
        ],
        "sample_open_alerts": sample_open,
    }


def _parse_rfc3339(value: str) -> datetime | None:
    raw = value.strip()
    if not raw:
        return None
    try:
        # Cloud APIs return Z-normalized timestamps.
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
