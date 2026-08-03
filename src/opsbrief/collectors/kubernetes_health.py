from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, cast

from googleapiclient.errors import HttpError
from kubernetes import client as k8s_client
from kubernetes.client import exceptions as k8s_exceptions
from urllib3.exceptions import MaxRetryError, NewConnectionError

from opsbrief.cluster_discovery import resolve_clusters
from opsbrief.config import EnvConfig
from opsbrief.gcp_api import build_cluster_manager_client, build_service, get_gke_cluster
from opsbrief.gcp_auth import AuthMode, get_auth_bundle
from opsbrief.k8s_api import access_token, api_client, cluster_ca_file, preferred_endpoints
from opsbrief.models import CheckResult, Status, now_utc_iso
from opsbrief.status import classify_http_error, max_status

_HIGH_RESTART_COUNT_THRESHOLD = 3


def collect(
    config: EnvConfig,
    timeout_seconds: int = 45,
    output_dir: str | None = None,
    auth_mode: AuthMode = "auto",
    impersonate_service_account: str = "",
) -> CheckResult:
    started_at = now_utc_iso()
    _ = output_dir
    cluster_results: list[dict[str, Any]] = []
    errors: list[str] = []
    status = Status.OK

    try:
        auth = get_auth_bundle(
            auth_mode=auth_mode, impersonate_service_account=impersonate_service_account
        )
        container = build_service(auth, "container", "v1", timeout_seconds)
        cluster_client = None
        try:
            cluster_client = build_cluster_manager_client(auth)
        except Exception:  # noqa: BLE001
            cluster_client = None
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            collector="kubernetes_health",
            status=Status.FAILED,
            summary="Unable to initialize Kubernetes collector dependencies",
            details={},
            errors=[str(exc)],
            started_at=started_at,
            finished_at=now_utc_iso(),
        )

    clusters = resolve_clusters(
        config,
        container,
        cluster_client=cluster_client,
        timeout_seconds=timeout_seconds,
    )
    if not clusters:
        return CheckResult(
            collector="kubernetes_health",
            status=Status.SKIPPED_CONFIG,
            summary="No clusters configured or discovered for kubernetes_health",
            details={"clusters": []},
            errors=[],
            started_at=started_at,
            finished_at=now_utc_iso(),
        )

    for cluster in clusters:
        request_name = (
            f"projects/{cluster.project}/locations/{cluster.region}/clusters/{cluster.name}"
        )
        try:
            cluster_payload = get_gke_cluster(
                container,
                request_name,
                cluster_client=cluster_client,
                timeout_seconds=timeout_seconds,
            )
        except HttpError as exc:
            failure_status = classify_http_error(exc)
            cluster_results.append(
                {
                    "cluster": cluster.name,
                    "status": failure_status.value,
                    "error": str(exc),
                }
            )
            errors.append(f"{cluster.name}: {exc}")
            status = max_status(status, failure_status)
            continue
        except Exception as exc:  # noqa: BLE001
            cluster_results.append(
                {"cluster": cluster.name, "status": Status.FAILED.value, "error": str(exc)}
            )
            errors.append(f"{cluster.name}: {exc}")
            status = max_status(status, Status.FAILED)
            continue

        endpoint = cluster_payload.get("endpoint", "")
        dns_endpoint = (
            (cluster_payload.get("controlPlaneEndpointsConfig") or {}).get("dnsEndpointConfig")
            or {}
        ).get("endpoint", "") or ""
        cert_b64 = ((cluster_payload.get("masterAuth") or {}).get("clusterCaCertificate", "")) or ""
        endpoints = preferred_endpoints(dns_endpoint=dns_endpoint, ip_endpoint=endpoint)
        if not endpoints or not cert_b64:
            cluster_results.append(
                {
                    "cluster": cluster.name,
                    "status": Status.FAILED.value,
                    "error": "Missing cluster endpoints or CA certificate in GKE API response",
                }
            )
            errors.append(f"{cluster.name}: missing endpoints or CA certificate")
            status = max_status(status, Status.FAILED)
            continue

        successful_endpoint = ""
        attempt_errors: list[str] = []
        last_api_exception: k8s_exceptions.ApiException | None = None
        last_exception: Exception | None = None
        try:
            token = access_token(auth.credentials, allow_gcloud_fallback=auth_mode == "auto")
            if not token:
                raise RuntimeError("Missing access token for Kubernetes API")

            with cluster_ca_file(cert_b64) as ca_path:
                snapshot: dict[str, Any] | None = None
                for candidate, use_cluster_ca in endpoints:
                    try:
                        snapshot = _query_cluster(
                            endpoint=candidate,
                            ca_path=ca_path if use_cluster_ca else None,
                            token=token,
                            timeout_seconds=timeout_seconds,
                        )
                        successful_endpoint = candidate
                        break
                    except (MaxRetryError, NewConnectionError, TimeoutError) as exc:
                        attempt_errors.append(f"{candidate}: {exc}")
                        last_exception = exc
                        continue
                    except k8s_exceptions.ApiException as exc:
                        attempt_errors.append(f"{candidate}: {exc}")
                        last_api_exception = exc
                        break

                if snapshot is None:
                    if last_api_exception is not None:
                        raise last_api_exception
                    if last_exception is not None:
                        raise last_exception
                    raise RuntimeError("Unable to query Kubernetes API on any endpoint")
        except k8s_exceptions.ApiException as exc:
            failure_status = _classify_k8s_api_exception(exc)
            cluster_results.append(
                {
                    "cluster": cluster.name,
                    "endpoint_candidates": [item[0] for item in endpoints],
                    "status": failure_status.value,
                    "error": _join_errors(attempt_errors) or str(exc),
                }
            )
            errors.append(f"{cluster.name}: {_join_errors(attempt_errors) or str(exc)}")
            status = max_status(status, failure_status)
            continue
        except (MaxRetryError, NewConnectionError, TimeoutError) as exc:
            cluster_results.append(
                {
                    "cluster": cluster.name,
                    "endpoint_candidates": [item[0] for item in endpoints],
                    "status": Status.SKIPPED_NETWORK.value,
                    "error": _join_errors(attempt_errors) or str(exc),
                }
            )
            errors.append(f"{cluster.name}: {_join_errors(attempt_errors) or str(exc)}")
            status = max_status(status, Status.SKIPPED_NETWORK)
            continue
        except Exception as exc:  # noqa: BLE001
            cluster_results.append(
                {
                    "cluster": cluster.name,
                    "endpoint_candidates": [item[0] for item in endpoints],
                    "status": Status.FAILED.value,
                    "error": _join_errors(attempt_errors) or str(exc),
                }
            )
            errors.append(f"{cluster.name}: {_join_errors(attempt_errors) or str(exc)}")
            status = max_status(status, Status.FAILED)
            continue

        assert snapshot is not None
        nodes_data = cast(k8s_client.V1NodeList, snapshot["nodes"])
        pods_data = cast(k8s_client.V1PodList, snapshot["pods"])
        ready_nodes = _count_ready_nodes(nodes_data)
        phase_counter, waiting_reasons = _pod_counters(pods_data)
        pod_issues = _pod_issue_diagnostics(pods_data, snapshot)
        workload_summary = _workload_summary(snapshot)
        hpa_summary = _hpa_summary(snapshot, config)
        metrics_summary = _metrics_summary(snapshot)
        namespace_utilization = _namespace_utilization(snapshot)
        events_summary = _events_summary(snapshot)
        cluster_status = Status.OK
        node_total = len(nodes_data.items)
        if ready_nodes == 0:
            cluster_status = Status.CRITICAL
        elif ready_nodes < node_total:
            cluster_status = Status.WARNING
        if waiting_reasons:
            cluster_status = max_status(cluster_status, Status.WARNING)
        if pod_issues:
            cluster_status = max_status(cluster_status, Status.WARNING)
        if workload_summary["unhealthy_workload_total"] > 0:
            cluster_status = max_status(cluster_status, Status.WARNING)
        if hpa_summary["scored_hpas_at_max"] > 0:
            cluster_status = max_status(cluster_status, Status.WARNING)
        if hpa_summary.get("scored_hpa_failure_count", 0) > 0:
            cluster_status = max_status(cluster_status, Status.WARNING)

        status = max_status(status, cluster_status)
        cluster_results.append(
            {
                "cluster": cluster.name,
                "endpoint": successful_endpoint or endpoints[0][0],
                "endpoint_candidates": [item[0] for item in endpoints],
                "node_total": node_total,
                "node_ready": ready_nodes,
                "node_inventory": _node_inventory(nodes_data),
                "pod_total": len(pods_data.items),
                "pod_phase_counts": dict(phase_counter),
                "pod_waiting_reasons": dict(waiting_reasons),
                "pod_issues": pod_issues,
                "workloads": workload_summary,
                "hpa": hpa_summary,
                "utilization": metrics_summary,
                "namespace_utilization": namespace_utilization,
                "events": events_summary,
                "status": cluster_status.value,
            }
        )

    summary = f"Collected kubernetes health for {len(cluster_results)} clusters"
    return CheckResult(
        collector="kubernetes_health",
        status=status,
        summary=summary,
        details={"clusters": cluster_results},
        errors=errors,
        started_at=started_at,
        finished_at=now_utc_iso(),
    )


def _query_cluster(
    endpoint: str,
    ca_path: Path | None,
    token: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    with api_client(endpoint=endpoint, token=token, ca_path=ca_path) as client:
        core_api = k8s_client.CoreV1Api(client)
        apps_api = k8s_client.AppsV1Api(client)
        autoscaling_api = k8s_client.AutoscalingV2Api(client)
        custom_api = k8s_client.CustomObjectsApi(client)

        nodes = core_api.list_node(_request_timeout=timeout_seconds)
        pods = core_api.list_pod_for_all_namespaces(_request_timeout=timeout_seconds)
        deployments = apps_api.list_deployment_for_all_namespaces(_request_timeout=timeout_seconds)
        statefulsets = apps_api.list_stateful_set_for_all_namespaces(
            _request_timeout=timeout_seconds
        )
        daemonsets = apps_api.list_daemon_set_for_all_namespaces(_request_timeout=timeout_seconds)
        events: Any = None
        events_error = ""
        try:
            events = core_api.list_event_for_all_namespaces(
                limit=500, _request_timeout=timeout_seconds
            )
        except k8s_exceptions.ApiException as exc:
            # Events API may be constrained by RBAC in some environments.
            events_error = str(exc)
            if exc.status not in (403, 404, 503):
                raise

        hpas: Any = None
        hpa_error = ""
        try:
            hpas = autoscaling_api.list_horizontal_pod_autoscaler_for_all_namespaces(
                _request_timeout=timeout_seconds
            )
        except k8s_exceptions.ApiException as exc:
            # Some clusters run without autoscaling/v2 enabled.
            # Keep health check read-only and resilient in that case.
            hpa_error = str(exc)
            if exc.status not in (404, 503):
                raise

        node_metrics: list[dict[str, Any]] = []
        pod_metrics: list[dict[str, Any]] = []
        metrics_error = ""
        try:
            node_resp = custom_api.list_cluster_custom_object(
                group="metrics.k8s.io",
                version="v1beta1",
                plural="nodes",
                _request_timeout=timeout_seconds,
            )
            pod_resp = custom_api.list_cluster_custom_object(
                group="metrics.k8s.io",
                version="v1beta1",
                plural="pods",
                _request_timeout=timeout_seconds,
            )
            node_metrics = cast(list[dict[str, Any]], node_resp.get("items", []))
            pod_metrics = cast(list[dict[str, Any]], pod_resp.get("items", []))
        except k8s_exceptions.ApiException as exc:
            # Metrics server API is optional in some environments.
            metrics_error = str(exc)
            if exc.status not in (404, 503):
                raise

        return {
            "nodes": nodes,
            "pods": pods,
            "deployments": deployments,
            "statefulsets": statefulsets,
            "daemonsets": daemonsets,
            "events": events,
            "events_error": events_error,
            "hpas": hpas,
            "hpa_error": hpa_error,
            "node_metrics": node_metrics,
            "pod_metrics": pod_metrics,
            "metrics_error": metrics_error,
        }


def _count_ready_nodes(nodes: k8s_client.V1NodeList) -> int:
    ready_nodes = 0
    for node in nodes.items:
        if _node_is_ready(node):
            ready_nodes += 1
    return ready_nodes


def _node_inventory(nodes: k8s_client.V1NodeList) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    for node in nodes.items:
        metadata = getattr(node, "metadata", None)
        name = str(getattr(metadata, "name", "") or "").strip()
        if not name:
            continue
        created_at_raw = getattr(metadata, "creation_timestamp", "") or ""
        created_at = (
            created_at_raw.isoformat()
            if hasattr(created_at_raw, "isoformat")
            else str(created_at_raw)
        )
        ready = _node_is_ready(node)
        inventory.append(
            {
                "name": name,
                "ready": ready,
                "status": "Ready" if ready else "NotReady",
                "created_at": created_at,
            }
        )
    return sorted(inventory, key=lambda item: str(item.get("name", "")))


def _node_is_ready(node: Any) -> bool:
    conditions = getattr(getattr(node, "status", None), "conditions", None) or []
    return any(
        getattr(condition, "type", "") == "Ready" and getattr(condition, "status", "") == "True"
        for condition in conditions
    )


def _pod_counters(pods: k8s_client.V1PodList) -> tuple[Counter[str], Counter[str]]:
    phase_counter: Counter[str] = Counter()
    waiting_reasons: Counter[str] = Counter()
    for pod in pods.items:
        phase = (pod.status.phase or "Unknown") if pod.status else "Unknown"
        phase_counter[phase] += 1
        statuses = (pod.status.container_statuses or []) if pod.status else []
        for item in statuses:
            state_obj = getattr(item, "state", None)
            state = getattr(state_obj, "waiting", None) if state_obj else None
            if state and state.reason:
                waiting_reasons[state.reason] += 1
    return phase_counter, waiting_reasons


def _pod_issue_diagnostics(
    pods: k8s_client.V1PodList, snapshot: dict[str, Any]
) -> list[dict[str, Any]]:
    event_index = _index_events_by_pod(snapshot.get("events"))
    issues: list[dict[str, Any]] = []

    for pod in pods.items:
        metadata = pod.metadata
        status = pod.status
        if metadata is None or status is None:
            continue

        namespace = metadata.namespace or "default"
        pod_name = metadata.name or "unknown-pod"
        pod_events = event_index.get((namespace, pod_name), [])
        has_probe_failure = any(
            event.get("reason") == "Unhealthy"
            and "probe failed" in str(event.get("message", "")).lower()
            for event in pod_events
        )

        container_statuses: list[Any] = []
        init_statuses = getattr(status, "init_container_statuses", None)
        if init_statuses:
            container_statuses.extend(init_statuses)
        runtime_statuses = getattr(status, "container_statuses", None)
        if runtime_statuses:
            container_statuses.extend(runtime_statuses)

        for container in container_statuses:
            state_obj = getattr(container, "state", None)
            waiting_state = getattr(state_obj, "waiting", None) if state_obj else None
            waiting_reason = str(getattr(waiting_state, "reason", "") or "")
            last_state = getattr(container, "last_state", None)
            last_terminated = getattr(last_state, "terminated", None) if last_state else None
            last_reason = str(getattr(last_terminated, "reason", "") or "")
            exit_code = int(getattr(last_terminated, "exit_code", 0) or 0)
            restart_count = int(getattr(container, "restart_count", 0) or 0)

            symptom = ""
            if waiting_reason == "CrashLoopBackOff":
                symptom = "CrashLoopBackOff"
            elif restart_count >= _HIGH_RESTART_COUNT_THRESHOLD:
                symptom = "HighRestartCount"
            if not symptom:
                continue

            waiting_message = str(getattr(waiting_state, "message", "") or "")
            current_state = _container_current_state(state_obj)

            probable_cause = "application_restart_loop"
            confidence = "medium"
            recommended_action = (
                "Review previous container logs and startup path for recurring crash signal."
            )

            if last_reason == "OOMKilled":
                probable_cause = "oom_killed"
                confidence = "high"
                recommended_action = (
                    "Increase memory requests/limits or reduce memory usage; "
                    "validate GC/heap behavior."
                )
            elif last_reason == "Completed" and exit_code == 0:
                probable_cause = "process_exits_successfully_then_restarts"
                confidence = "high"
                recommended_action = (
                    "Container exits successfully but controller restarts it. "
                    "Use Job/CronJob for one-shot tasks or keep process running."
                )
            elif has_probe_failure:
                probable_cause = "probe_failure_restart"
                confidence = "high"
                recommended_action = (
                    "Review startup/liveness probe timing and endpoint health "
                    "before container restarts."
                )
            elif exit_code != 0:
                probable_cause = "non_zero_exit_code"
                confidence = "medium"
                recommended_action = (
                    "Inspect application startup error and dependencies causing process exit."
                )
            elif symptom == "HighRestartCount":
                probable_cause = "repeated_restarts_observed"
                confidence = "low"
                recommended_action = (
                    "Review previous container logs and correlate restart time with rollout, "
                    "node, dependency, or probe events."
                )

            issues.append(
                {
                    "resource_type": "kubernetes_pod",
                    "resource": f"{namespace}/{pod_name}",
                    "namespace": namespace,
                    "pod": pod_name,
                    "container": str(getattr(container, "name", "") or "unknown-container"),
                    "symptom": symptom,
                    "probable_cause": probable_cause,
                    "confidence": confidence,
                    "evidence": {
                        "restart_count": restart_count,
                        "current_state": current_state,
                        "last_terminated_reason": last_reason or "unknown",
                        "last_exit_code": exit_code,
                        "last_finished_at": _object_timestamp(
                            getattr(last_terminated, "finished_at", "") if last_terminated else ""
                        ),
                        "waiting_message": waiting_message,
                        "warning_event_reasons": sorted(
                            {
                                str(event.get("reason", ""))
                                for event in pod_events
                                if str(event.get("type", "")).lower() == "warning"
                            }
                        ),
                    },
                    "recommended_action": recommended_action,
                }
            )

    return issues[:100]


def _container_current_state(state: Any) -> str:
    if state is None:
        return "unknown"
    waiting = getattr(state, "waiting", None)
    if waiting is not None:
        reason = str(getattr(waiting, "reason", "") or "").strip()
        return reason or "Waiting"
    running = getattr(state, "running", None)
    if running is not None:
        return "Running"
    terminated = getattr(state, "terminated", None)
    if terminated is not None:
        reason = str(getattr(terminated, "reason", "") or "").strip()
        return reason or "Terminated"
    return "unknown"


def _object_timestamp(value: Any) -> str:
    if value is None:
        return ""
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value)


def _index_events_by_pod(events_obj: Any) -> dict[tuple[str, str], list[dict[str, str]]]:
    index: dict[tuple[str, str], list[dict[str, str]]] = {}
    if events_obj is None:
        return index

    events = cast(list[Any], getattr(events_obj, "items", []) or [])
    for event in events:
        involved = getattr(event, "involved_object", None)
        if involved is None:
            continue
        if str(getattr(involved, "kind", "") or "").lower() != "pod":
            continue
        namespace = str(getattr(involved, "namespace", "") or "").strip()
        name = str(getattr(involved, "name", "") or "").strip()
        if not namespace or not name:
            continue
        key = (namespace, name)
        index.setdefault(key, []).append(
            {
                "type": str(getattr(event, "type", "") or ""),
                "reason": str(getattr(event, "reason", "") or ""),
                "message": str(getattr(event, "message", "") or ""),
            }
        )
    return index


def _workload_summary(snapshot: dict[str, Any]) -> dict[str, int]:
    deployments = cast(list[Any], getattr(snapshot.get("deployments"), "items", []) or [])
    statefulsets = cast(list[Any], getattr(snapshot.get("statefulsets"), "items", []) or [])
    daemonsets = cast(list[Any], getattr(snapshot.get("daemonsets"), "items", []) or [])

    deployments_unavailable = 0
    for item in deployments:
        desired = 0
        if item.spec and item.spec.replicas is not None:
            desired = item.spec.replicas
        available = (item.status.available_replicas or 0) if item.status else 0
        if desired > available:
            deployments_unavailable += 1

    statefulsets_unready = 0
    for item in statefulsets:
        desired = (item.status.replicas or 0) if item.status else 0
        ready = (item.status.ready_replicas or 0) if item.status else 0
        if desired > ready:
            statefulsets_unready += 1

    daemonsets_unavailable = 0
    for item in daemonsets:
        desired = (item.status.desired_number_scheduled or 0) if item.status else 0
        ready = (item.status.number_ready or 0) if item.status else 0
        if desired > ready:
            daemonsets_unavailable += 1

    return {
        "deployments_total": len(deployments),
        "deployments_unavailable": deployments_unavailable,
        "statefulsets_total": len(statefulsets),
        "statefulsets_unready": statefulsets_unready,
        "daemonsets_total": len(daemonsets),
        "daemonsets_unavailable": daemonsets_unavailable,
        "unhealthy_workload_total": (
            deployments_unavailable + statefulsets_unready + daemonsets_unavailable
        ),
    }


def _hpa_summary(snapshot: dict[str, Any], config: EnvConfig) -> dict[str, Any]:
    autoscaling_policy = _autoscaling_policy_summary(config)
    hpas_obj = snapshot.get("hpas")
    if hpas_obj is None:
        return {
            "total": 0,
            "hpas_at_max": 0,
            "hpas_scaling_limited": 0,
            "hpa_failure_count": 0,
            "workload_hpa_total": 0,
            "platform_hpa_total": 0,
            "workload_hpas_at_max": 0,
            "platform_hpas_at_max": 0,
            "workload_hpa_failure_count": 0,
            "platform_hpa_failure_count": 0,
            "scored_hpas_at_max": 0,
            "scored_hpa_failure_count": 0,
            "details": [],
            "api_available": False,
            "error": snapshot.get("hpa_error", ""),
            "policy": autoscaling_policy,
        }

    hpas = cast(list[Any], getattr(hpas_obj, "items", []) or [])
    event_index = _index_hpa_warning_events(snapshot.get("events"))
    at_max = 0
    scaling_limited = 0
    failure_count = 0
    workload_hpa_total = 0
    platform_hpa_total = 0
    workload_hpas_at_max = 0
    platform_hpas_at_max = 0
    workload_hpa_failure_count = 0
    platform_hpa_failure_count = 0
    scored_hpas_at_max = 0
    scored_hpa_failure_count = 0
    details: list[dict[str, Any]] = []
    for item in hpas:
        status = getattr(item, "status", None)
        spec = getattr(item, "spec", None)
        current = (getattr(status, "current_replicas", 0) or 0) if status else 0
        desired = (getattr(status, "desired_replicas", 0) or 0) if status else 0
        min_replicas = (getattr(spec, "min_replicas", 1) or 1) if spec else 1
        max_replicas = (getattr(spec, "max_replicas", 0) or 0) if spec else 0
        if max_replicas and current >= max_replicas:
            at_max += 1

        metadata = getattr(item, "metadata", None)
        namespace = str(getattr(metadata, "namespace", "") or "default")
        name = str(getattr(metadata, "name", "") or "unknown-hpa")
        layer = _autoscaling_layer_for_namespace(config, namespace)
        policy = _autoscaling_policy_for_layer(config, layer)
        is_scored = policy.status == "assessed"
        if layer == "platform_hpa":
            platform_hpa_total += 1
        else:
            workload_hpa_total += 1
        hpa_scaling_limited = False
        hpa_failure_conditions: list[dict[str, str]] = []
        hpa_conditions: list[dict[str, str]] = []
        conditions = (getattr(status, "conditions", []) or []) if status else []
        for condition in conditions:
            condition_row = _hpa_condition_row(condition)
            hpa_conditions.append(condition_row)
            if (
                getattr(condition, "type", "") == "ScalingLimited"
                and getattr(condition, "status", "") == "True"
            ):
                hpa_scaling_limited = True
                scaling_limited += 1
            if _hpa_condition_is_failure(condition):
                hpa_failure_conditions.append(condition_row)
        warning_events = event_index.get((namespace, name), [])
        if hpa_failure_conditions or warning_events:
            failure_count += 1
            if layer == "platform_hpa":
                platform_hpa_failure_count += 1
            else:
                workload_hpa_failure_count += 1
            if is_scored:
                scored_hpa_failure_count += 1
        if max_replicas and current >= max_replicas:
            if layer == "platform_hpa":
                platform_hpas_at_max += 1
            else:
                workload_hpas_at_max += 1
            if is_scored:
                scored_hpas_at_max += 1

        details.append(
            {
                "namespace": namespace,
                "name": name,
                "autoscaling_layer": layer,
                "policy_status": policy.status,
                "policy_reason": policy.reason,
                "current_replicas": current,
                "desired_replicas": desired,
                "min_replicas": min_replicas,
                "max_replicas": max_replicas,
                "at_max_replicas": bool(max_replicas and current >= max_replicas),
                "scaling_limited": hpa_scaling_limited,
                "conditions": hpa_conditions,
                "failure_conditions": hpa_failure_conditions,
                "warning_events": warning_events,
            }
        )

    return {
        "total": len(hpas),
        "hpas_at_max": at_max,
        "hpas_scaling_limited": scaling_limited,
        "hpa_failure_count": failure_count,
        "workload_hpa_total": workload_hpa_total,
        "platform_hpa_total": platform_hpa_total,
        "workload_hpas_at_max": workload_hpas_at_max,
        "platform_hpas_at_max": platform_hpas_at_max,
        "workload_hpa_failure_count": workload_hpa_failure_count,
        "platform_hpa_failure_count": platform_hpa_failure_count,
        "scored_hpas_at_max": scored_hpas_at_max,
        "scored_hpa_failure_count": scored_hpa_failure_count,
        "details": details,
        "api_available": True,
        "error": "",
        "policy": autoscaling_policy,
    }


def _autoscaling_policy_summary(config: EnvConfig) -> dict[str, Any]:
    autoscaling = config.report_expectations.autoscaling
    return {
        "workload_hpa": {
            "status": autoscaling.workload_hpa.status,
            "reason": autoscaling.workload_hpa.reason,
        },
        "platform_hpa": {
            "status": autoscaling.platform_hpa.status,
            "reason": autoscaling.platform_hpa.reason,
        },
        "node_pool_autoscaling": {
            "status": autoscaling.node_pool_autoscaling.status,
            "reason": autoscaling.node_pool_autoscaling.reason,
        },
        "platform_namespaces": list(autoscaling.platform_namespaces),
    }


def _autoscaling_layer_for_namespace(config: EnvConfig, namespace: str) -> str:
    platform_namespaces = set(config.report_expectations.autoscaling.platform_namespaces)
    if namespace in platform_namespaces:
        return "platform_hpa"
    return "workload_hpa"


def _autoscaling_policy_for_layer(config: EnvConfig, layer: str) -> Any:
    autoscaling = config.report_expectations.autoscaling
    if layer == "platform_hpa":
        return autoscaling.platform_hpa
    return autoscaling.workload_hpa


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def _hpa_condition_row(condition: Any) -> dict[str, str]:
    return {
        "type": str(getattr(condition, "type", "") or ""),
        "status": str(getattr(condition, "status", "") or ""),
        "reason": str(getattr(condition, "reason", "") or ""),
        "message": str(getattr(condition, "message", "") or ""),
    }


def _hpa_condition_is_failure(condition: Any) -> bool:
    condition_type = str(getattr(condition, "type", "") or "")
    condition_status = str(getattr(condition, "status", "") or "")
    reason = str(getattr(condition, "reason", "") or "").lower()
    message = str(getattr(condition, "message", "") or "").lower()
    if condition_type in ("AbleToScale", "ScalingActive") and condition_status == "False":
        return True
    return any(token in reason or token in message for token in ("failed", "error", "invalid"))


def _index_hpa_warning_events(events_obj: Any) -> dict[tuple[str, str], list[dict[str, str]]]:
    index: dict[tuple[str, str], list[dict[str, str]]] = {}
    if events_obj is None:
        return index

    events = cast(list[Any], getattr(events_obj, "items", []) or [])
    for event in events:
        if str(getattr(event, "type", "") or "").upper() != "WARNING":
            continue
        involved = getattr(event, "involved_object", None)
        if involved is None:
            continue
        if str(getattr(involved, "kind", "") or "") != "HorizontalPodAutoscaler":
            continue
        namespace = str(getattr(involved, "namespace", "") or "default")
        name = str(getattr(involved, "name", "") or "")
        if not name:
            continue
        index.setdefault((namespace, name), []).append(
            {
                "reason": str(getattr(event, "reason", "") or ""),
                "message": str(getattr(event, "message", "") or ""),
            }
        )
    return index


def _metrics_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    node_metrics = cast(list[dict[str, Any]], snapshot.get("node_metrics", []))
    pod_metrics = cast(list[dict[str, Any]], snapshot.get("pod_metrics", []))
    metrics_error = str(snapshot.get("metrics_error", ""))

    if metrics_error:
        return {
            "metrics_server_available": False,
            "nodes_with_metrics": 0,
            "pods_with_metrics": 0,
            "total_node_cpu_millicores": 0,
            "total_node_memory_bytes": 0,
            "error": metrics_error,
        }

    node_cpu_total = 0
    node_memory_total = 0
    for item in node_metrics:
        usage = cast(dict[str, str], item.get("usage", {}))
        node_cpu_total += _parse_cpu_millicores(usage.get("cpu", "0"))
        node_memory_total += _parse_memory_bytes(usage.get("memory", "0"))

    return {
        "metrics_server_available": True,
        "nodes_with_metrics": len(node_metrics),
        "pods_with_metrics": len(pod_metrics),
        "total_node_cpu_millicores": node_cpu_total,
        "total_node_memory_bytes": node_memory_total,
        "error": "",
    }


def _namespace_utilization(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    pod_metrics = cast(list[dict[str, Any]], snapshot.get("pod_metrics", []))
    metrics_error = str(snapshot.get("metrics_error", ""))
    if metrics_error:
        return []

    namespace_totals: dict[str, dict[str, int]] = {}
    for pod in pod_metrics:
        metadata = _as_dict(pod.get("metadata"))
        namespace = str(metadata.get("namespace", "")).strip() or "default"
        bucket = namespace_totals.setdefault(
            namespace,
            {
                "cpu_millicores": 0,
                "memory_bytes": 0,
                "pod_count": 0,
            },
        )
        bucket["pod_count"] += 1
        for container in _as_dict_list(pod.get("containers")):
            usage = _as_dict(container.get("usage"))
            bucket["cpu_millicores"] += _parse_cpu_millicores(str(usage.get("cpu", "0")))
            bucket["memory_bytes"] += _parse_memory_bytes(str(usage.get("memory", "0")))

    rows = [
        {
            "namespace": namespace,
            "pod_count": values["pod_count"],
            "cpu_millicores": values["cpu_millicores"],
            "memory_bytes": values["memory_bytes"],
        }
        for namespace, values in namespace_totals.items()
    ]
    rows.sort(
        key=lambda item: (
            cast(int, item["cpu_millicores"]),
            cast(int, item["memory_bytes"]),
        ),
        reverse=True,
    )
    return rows[:30]


def _events_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    events_obj = snapshot.get("events")
    if events_obj is None:
        return {
            "api_available": False,
            "total": 0,
            "warning_total": 0,
            "top_warning_reasons": [],
            "error": str(snapshot.get("events_error", "")),
        }

    events = cast(list[Any], getattr(events_obj, "items", []) or [])
    warning_reasons: Counter[str] = Counter()
    warning_total = 0
    for event in events:
        event_type = str(getattr(event, "type", "") or "")
        reason = str(getattr(event, "reason", "") or "unknown")
        if event_type.upper() != "WARNING":
            continue
        warning_total += 1
        warning_reasons[reason] += 1

    return {
        "api_available": True,
        "total": len(events),
        "warning_total": warning_total,
        "top_warning_reasons": [
            {"reason": reason, "count": count} for reason, count in warning_reasons.most_common(5)
        ],
        "error": "",
    }


def _parse_cpu_millicores(value: str) -> int:
    raw = value.strip()
    if not raw:
        return 0
    if raw.endswith("n"):
        return int(raw[:-1]) // 1_000_000
    if raw.endswith("u"):
        return int(raw[:-1]) // 1_000
    if raw.endswith("m"):
        return int(raw[:-1])
    return int(float(raw) * 1000)


def _parse_memory_bytes(value: str) -> int:
    raw = value.strip()
    if not raw:
        return 0

    base10 = {
        "K": 10**3,
        "M": 10**6,
        "G": 10**9,
        "T": 10**12,
        "P": 10**15,
        "E": 10**18,
    }
    base2 = {
        "Ki": 2**10,
        "Mi": 2**20,
        "Gi": 2**30,
        "Ti": 2**40,
        "Pi": 2**50,
        "Ei": 2**60,
    }

    for suffix, factor in base2.items():
        if raw.endswith(suffix):
            return int(float(raw[: -len(suffix)]) * factor)
    for suffix, factor in base10.items():
        if raw.endswith(suffix):
            return int(float(raw[: -len(suffix)]) * factor)
    return int(float(raw))


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _classify_k8s_api_exception(exc: k8s_exceptions.ApiException) -> Status:
    if exc.status in (401, 403):
        return Status.SKIPPED_PERMISSION
    if exc.status in (429, 500, 502, 503, 504):
        return Status.SKIPPED_NETWORK
    return Status.FAILED


def _join_errors(errors: list[str]) -> str:
    if not errors:
        return ""
    return "; ".join(errors)
