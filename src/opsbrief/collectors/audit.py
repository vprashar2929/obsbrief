from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from googleapiclient.errors import HttpError

from opsbrief.cluster_discovery import candidate_projects
from opsbrief.config import EnvConfig
from opsbrief.gcp_api import build_logging_client, build_service, resolve_fallback_service
from opsbrief.gcp_auth import AuthMode, get_auth_bundle
from opsbrief.models import CheckResult, Status, now_utc_iso
from opsbrief.status import classify_http_error, max_status

_MAX_ENTRIES = 1000
_REVIEW_CANDIDATE_MAX_ENTRIES = 200
_HIGH_RISK_MAX_ENTRIES = 200


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
            collector="audit",
            status=Status.SKIPPED_CONFIG,
            summary="No projects configured for audit collector",
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
        logging_client = None
        try:
            logging_client = build_logging_client(auth)
        except Exception:  # noqa: BLE001
            logging_client = None
        logging_service = None

        def logging_service_fallback() -> Any:
            nonlocal logging_service
            if logging_service is None:
                logging_service = build_service(auth, "logging", "v2", timeout_seconds)
            return logging_service

    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            collector="audit",
            status=Status.FAILED,
            summary="Unable to initialize audit collector dependencies",
            details={},
            errors=[str(exc)],
            started_at=started_at,
            finished_at=now_utc_iso(),
        )

    end = datetime.now(tz=UTC)
    start = end - timedelta(days=7)
    start_text = start.isoformat().replace("+00:00", "Z")
    end_text = end.isoformat().replace("+00:00", "Z")
    for project in projects:
        filter_text = _base_audit_filter(project, start_text, end_text)
        review_filter_text = _review_candidate_filter(project, start_text, end_text)
        high_risk_filter_text = _high_risk_filter(project, start_text, end_text)
        try:
            entries, entries_limited = _list_entries(
                service=logging_service_fallback,
                project=project,
                filter_text=filter_text,
                max_entries=_MAX_ENTRIES,
                logging_client=logging_client,
            )
            review_entries, review_entries_limited = _list_entries(
                service=logging_service_fallback,
                project=project,
                filter_text=review_filter_text,
                max_entries=_REVIEW_CANDIDATE_MAX_ENTRIES,
                logging_client=logging_client,
            )
            high_risk_entries, high_risk_entries_limited = _list_entries(
                service=logging_service_fallback,
                project=project,
                filter_text=high_risk_filter_text,
                max_entries=_HIGH_RISK_MAX_ENTRIES,
                logging_client=logging_client,
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

        method_counts: Counter[str] = Counter()
        meaningful_method_counts: Counter[str] = Counter()
        new_secret_events = 0
        high_risk_events = 0
        delete_events = 0
        update_events = 0
        iam_events = 0
        noisy_events = 0
        meaningful_change_events = 0
        meaningful_create_events = 0
        meaningful_delete_events = 0
        meaningful_update_events = 0
        meaningful_secret_events = 0
        meaningful_configmap_events = 0
        recent_events: list[dict[str, str]] = []
        meaningful_recent_events: list[dict[str, str]] = []

        for entry in entries:
            event_row = _audit_event_row(entry)
            method = event_row["method"]
            principal = event_row["principal"]
            resource = event_row["resource"]
            method_counts[method] += 1
            is_noisy = _is_noisy_audit_event(method, resource, principal)
            if is_noisy:
                noisy_events += 1

            if len(recent_events) < 20:
                recent_events.append(event_row)

        targeted_entries = _dedupe_entries([*review_entries, *high_risk_entries])
        targeted_entries_limited = review_entries_limited or high_risk_entries_limited

        for entry in targeted_entries:
            event_row = _audit_event_row(entry)
            method = event_row["method"]
            principal = event_row["principal"]
            resource = event_row["resource"]
            action = event_row["action"]
            if _is_noisy_audit_event(method, resource, principal):
                continue
            if not _is_review_candidate_event(event_row):
                continue

            meaningful_method_counts[method] += 1
            if action in ("create", "delete", "update"):
                meaningful_change_events += 1
            if action == "create":
                meaningful_create_events += 1
            if action == "delete":
                meaningful_delete_events += 1
            if action == "update":
                meaningful_update_events += 1
            if event_row["resource_type"] == "Secret":
                meaningful_secret_events += 1
            if event_row["resource_type"] == "ConfigMap":
                meaningful_configmap_events += 1
            if _is_secret_change(method) or event_row["resource_type"] == "Secret":
                new_secret_events += 1
            if _is_high_risk_audit_method(method):
                high_risk_events += 1
            if action == "delete":
                delete_events += 1
            if action == "update":
                update_events += 1
            if _is_iam_change(method):
                iam_events += 1
            if len(meaningful_recent_events) < 20:
                meaningful_recent_events.append(event_row)

        row_status = Status.WARNING if high_risk_events > 0 else Status.OK
        status = max_status(status, row_status)
        project_rows.append(
            {
                "project": project,
                "status": row_status.value,
                "window_start": start_text,
                "window_end": end_text,
                "audit_change_count": len(entries),
                "audit_entries_limited": entries_limited,
                "audit_entry_limit": _MAX_ENTRIES,
                "review_candidate_entry_count": len(targeted_entries),
                "review_candidate_entries_limited": targeted_entries_limited,
                "review_candidate_entry_limit": _REVIEW_CANDIDATE_MAX_ENTRIES,
                "high_risk_entry_count": len(high_risk_entries),
                "high_risk_entries_limited": high_risk_entries_limited,
                "high_risk_entry_limit": _HIGH_RISK_MAX_ENTRIES,
                "new_secret_events": new_secret_events,
                "high_risk_events": high_risk_events,
                "delete_events": delete_events,
                "update_events": update_events,
                "iam_events": iam_events,
                "noisy_events_filtered": noisy_events,
                "meaningful_change_events": meaningful_change_events,
                "meaningful_create_events": meaningful_create_events,
                "meaningful_delete_events": meaningful_delete_events,
                "meaningful_update_events": meaningful_update_events,
                "meaningful_secret_events": meaningful_secret_events,
                "meaningful_configmap_events": meaningful_configmap_events,
                "top_methods": [
                    {"method": method, "count": count}
                    for method, count in method_counts.most_common(10)
                ],
                "top_meaningful_methods": [
                    {"method": method, "count": count}
                    for method, count in meaningful_method_counts.most_common(10)
                ],
                "recent_events": recent_events,
                "meaningful_recent_events": meaningful_recent_events,
            }
        )

    summary = f"Collected audit activity for {len(project_rows)} project(s)"
    return CheckResult(
        collector="audit",
        status=status,
        summary=summary,
        details={"projects": project_rows},
        errors=errors,
        started_at=started_at,
        finished_at=now_utc_iso(),
    )


def _list_entries(
    service: object,
    project: str,
    filter_text: str,
    max_entries: int,
    *,
    logging_client: Any | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    client_error: Exception | None = None
    if logging_client is not None:
        try:
            return _list_entries_with_client(
                logging_client,
                project,
                filter_text,
                max_entries,
            )
        except Exception as exc:  # noqa: BLE001
            client_error = exc
    service = resolve_fallback_service(
        service,
        client_error,
        "Logging discovery service unavailable",
    )
    return _list_entries_with_service(service, project, filter_text, max_entries)


def _list_entries_with_client(
    client: Any,
    project: str,
    filter_text: str,
    max_entries: int,
) -> tuple[list[dict[str, Any]], bool]:
    rows = [
        _logging_entry_to_dict(entry)
        for entry in client.list_entries(
            resource_names=[f"projects/{project}"],
            filter_=filter_text,
            order_by="timestamp desc",
            max_results=max_entries + 1,
            page_size=min(200, max_entries + 1),
        )
    ]
    return rows[:max_entries], len(rows) > max_entries


def _list_entries_with_service(
    service: Any,
    project: str,
    filter_text: str,
    max_entries: int,
) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    page_token = ""
    while True:
        body: dict[str, Any] = {
            "resourceNames": [f"projects/{project}"],
            "filter": filter_text,
            "orderBy": "timestamp desc",
            "pageSize": min(200, max_entries),
        }
        if page_token:
            body["pageToken"] = page_token
        response = service.entries().list(body=body).execute()
        items = _as_dict_list(response.get("entries"))
        rows.extend(items)
        if len(rows) >= max_entries:
            return rows[:max_entries], bool(str(response.get("nextPageToken", "")).strip())
        page_token = str(response.get("nextPageToken", "")).strip()
        if not page_token:
            return rows, False


def _logging_entry_to_dict(entry: Any) -> dict[str, Any]:
    if isinstance(entry, dict):
        return entry
    to_api_repr = getattr(entry, "to_api_repr", None)
    if callable(to_api_repr):
        value = to_api_repr()
        if isinstance(value, dict):
            return value
    return {}


def _base_audit_filter(project: str, start_text: str, end_text: str) -> str:
    return (
        f'timestamp>="{start_text}" AND timestamp<="{end_text}" '
        f'AND logName="projects/{project}/logs/cloudaudit.googleapis.com%2Factivity" '
        "AND protoPayload.methodName:*"
    )


def _review_candidate_filter(project: str, start_text: str, end_text: str) -> str:
    candidate_terms = (
        'protoPayload.methodName:"configmaps"',
        'protoPayload.resourceName:"/configmaps/"',
        'protoPayload.methodName:"secrets"',
        'protoPayload.resourceName:"/secrets/"',
        'protoPayload.methodName:"SecretManagerService"',
        'protoPayload.methodName:"secretmanager"',
        'protoPayload.methodName:"SetIamPolicy"',
        'protoPayload.methodName:"setIamPolicy"',
        'protoPayload.methodName:"firewalls"',
        'protoPayload.resourceName:"/firewalls/"',
        'protoPayload.methodName:"routes"',
        'protoPayload.resourceName:"/routes/"',
    )
    noisy_terms = (
        'protoPayload.methodName:"leases"',
        'protoPayload.resourceName:"/leases/"',
        'protoPayload.methodName:"status"',
        'protoPayload.resourceName:"/status"',
        'protoPayload.methodName:"services.proxy.get"',
        'protoPayload.resourceName:"cluster-autoscaler-status"',
        'protoPayload.resourceName:"cluster-kubestore"',
        'protoPayload.resourceName:"gke-common-webhook-heartbeat"',
        'protoPayload.resourceName:"istio-ip-autoallocate"',
        'protoPayload.resourceName:"istio-leader"',
        'protoPayload.resourceName:"istio-namespace-controller-election"',
    )
    filter_text = (
        f"{_base_audit_filter(project, start_text, end_text)} AND ({' OR '.join(candidate_terms)})"
    )
    for term in noisy_terms:
        filter_text = f"{filter_text} AND NOT {term}"
    return filter_text


def _high_risk_filter(project: str, start_text: str, end_text: str) -> str:
    high_risk_terms = (
        'protoPayload.methodName:"SetIamPolicy"',
        'protoPayload.methodName:"setIamPolicy"',
        'protoPayload.methodName:"firewalls.insert"',
        'protoPayload.methodName:"firewalls.patch"',
        'protoPayload.methodName:"firewalls.delete"',
        'protoPayload.resourceName:"/firewalls/"',
        'protoPayload.methodName:"routes.insert"',
        'protoPayload.methodName:"routes.delete"',
        'protoPayload.resourceName:"/routes/"',
    )
    return (
        f"{_base_audit_filter(project, start_text, end_text)} "
        'AND resource.type!="k8s_cluster" '
        f"AND ({' OR '.join(high_risk_terms)})"
    )


def _dedupe_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for entry in entries:
        proto = _as_dict(entry.get("protoPayload"))
        key = (
            str(entry.get("timestamp", "")),
            str(proto.get("methodName", "")),
            str(_as_dict(proto.get("authenticationInfo")).get("principalEmail", "")),
            str(proto.get("resourceName", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        rows.append(entry)
    return rows


def _audit_event_row(entry: dict[str, Any]) -> dict[str, str]:
    proto = _as_dict(entry.get("protoPayload"))
    method = str(proto.get("methodName", "unknown"))
    principal = str(_as_dict(proto.get("authenticationInfo")).get("principalEmail", ""))
    resource = str(proto.get("resourceName", "")) or str(entry.get("logName", ""))
    timestamp = str(entry.get("timestamp", ""))
    action = _audit_action(method)
    resource_info = _audit_resource_identity(method, resource)
    return {
        "timestamp": timestamp,
        "action": action,
        "method": method,
        "principal": principal,
        "resource": resource,
        "resource_type": resource_info["type"],
        "resource_scope": resource_info["scope"],
        "resource_name": resource_info["name"],
    }


def _is_review_candidate_event(event: dict[str, str]) -> bool:
    action = event.get("action", "")
    if action not in ("create", "delete", "update"):
        return False
    resource_type = event.get("resource_type", "")
    if resource_type in {"ConfigMap", "Secret", "IAM Policy", "Firewall Rule", "Route"}:
        return True
    method = event.get("method", "").lower()
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


def _is_secret_change(method_name: str) -> bool:
    text = method_name.lower()
    return "secret" in text and (
        "create" in text or "addsecretversion" in text or "add_version" in text
    )


def _is_high_risk_audit_method(method_name: str) -> bool:
    normalized = method_name.lower()
    tokens = (
        "setiampolicy",
        "firewalls.insert",
        "firewalls.patch",
        "firewalls.delete",
        "routes.insert",
        "routes.delete",
        "owners",
    )
    return any(token in normalized for token in tokens)


def _audit_action(method_name: str) -> str:
    normalized = method_name.lower()
    if ".delete" in normalized or normalized.endswith("delete") or "delete" in normalized:
        return "delete"
    update_tokens = (
        ".update",
        ".patch",
        "setiampolicy",
        "update",
        "patch",
    )
    if any(token in normalized for token in update_tokens):
        return "update"
    create_tokens = (".create", ".insert", "create", "insert")
    if any(token in normalized for token in create_tokens):
        return "create"
    return "other"


def _is_iam_change(method_name: str) -> bool:
    normalized = method_name.lower()
    return "setiampolicy" in normalized or ".iam." in normalized or "iam" in normalized


def _audit_resource_identity(method_name: str, resource_name: str) -> dict[str, str]:
    method = method_name.lower()
    parts = [part for part in resource_name.split("/") if part]
    namespace = _resource_part_after(parts, "namespaces")

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
            "scope": namespace or _resource_part_after(parts, "projects") or "project",
            "name": secret_name or (parts[-1] if parts else "resource not provided"),
        }
    if "firewalls" in parts:
        return {
            "type": "Firewall Rule",
            "scope": _resource_part_after(parts, "projects") or "project",
            "name": _resource_part_after(parts, "firewalls") or parts[-1],
        }
    if "routes" in parts:
        return {
            "type": "Route",
            "scope": _resource_part_after(parts, "projects") or "project",
            "name": _resource_part_after(parts, "routes") or parts[-1],
        }
    if "setiampolicy" in method:
        return {
            "type": "IAM Policy",
            "scope": _resource_part_after(parts, "projects") or "project",
            "name": resource_name or "resource not provided",
        }
    if parts:
        return {"type": "Audit Resource", "scope": namespace or "project", "name": parts[-1]}
    return {"type": "Audit Resource", "scope": "unknown", "name": "resource not provided"}


def _resource_part_after(parts: list[str], marker: str) -> str:
    try:
        index = parts.index(marker)
    except ValueError:
        return ""
    next_index = index + 1
    if next_index >= len(parts):
        return ""
    return parts[next_index]


def _is_noisy_audit_event(method_name: str, resource_name: str, principal: str = "") -> bool:
    method = method_name.lower()
    resource = resource_name.lower()
    actor = principal.lower()
    if ".leases." in method or "/leases/" in resource:
        return True
    if ".status." in method or resource.endswith("/status") or "/status" in resource:
        return True
    if ".services.proxy.get" in method:
        return True
    if "/pods/gke-system-balloon-pod" in resource and actor == "system:cluster-autoscaler":
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


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []
