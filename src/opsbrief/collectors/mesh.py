from __future__ import annotations

import ast
import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from googleapiclient.errors import HttpError
from kubernetes import client as k8s_client
from kubernetes.client import exceptions as k8s_exceptions
from kubernetes.stream import stream as k8s_stream
from urllib3.exceptions import MaxRetryError, NewConnectionError

from opsbrief.cluster_discovery import resolve_clusters
from opsbrief.config import EnvConfig
from opsbrief.gcp_api import build_cluster_manager_client, build_service, get_gke_cluster
from opsbrief.gcp_auth import AuthMode, get_auth_bundle
from opsbrief.k8s_api import access_token, api_client, cluster_ca_file, preferred_endpoints
from opsbrief.models import CheckResult, Status, now_utc_iso
from opsbrief.status import classify_http_error, max_status

_ENVOY_SAMPLE_TIMEOUT_SECONDS = 8
_ENVOY_SAMPLE_LIMIT = 2
_ISTIOCTL_TIMEOUT_SECONDS = 30
_PROXY_LOG_TIMEOUT_SECONDS = 20
_PROXY_LOG_TAIL_LINES = 80
_PROXY_LOG_EXTENDED_TAIL_LINES = 2000


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
    cluster_rows: list[dict[str, Any]] = []

    try:
        auth = get_auth_bundle(
            auth_mode=auth_mode,
            impersonate_service_account=impersonate_service_account,
        )
        container = build_service(auth, "container", "v1", timeout_seconds)
        cluster_client = None
        try:
            cluster_client = build_cluster_manager_client(auth)
        except Exception:  # noqa: BLE001
            cluster_client = None
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            collector="mesh",
            status=Status.FAILED,
            summary="Unable to initialize mesh collector dependencies",
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
            collector="mesh",
            status=Status.SKIPPED_CONFIG,
            summary="No clusters configured or discovered for mesh collector",
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
            payload = get_gke_cluster(
                container,
                request_name,
                cluster_client=cluster_client,
                timeout_seconds=timeout_seconds,
            )
        except HttpError as exc:
            failure_status = classify_http_error(exc)
            cluster_rows.append(
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
            cluster_rows.append(
                {
                    "cluster": cluster.name,
                    "status": Status.FAILED.value,
                    "error": str(exc),
                }
            )
            errors.append(f"{cluster.name}: {exc}")
            status = max_status(status, Status.FAILED)
            continue

        endpoint = str(payload.get("endpoint", ""))
        dns_endpoint = (
            (payload.get("controlPlaneEndpointsConfig") or {}).get("dnsEndpointConfig") or {}
        ).get("endpoint", "") or ""
        cert_b64 = str(((payload.get("masterAuth") or {}).get("clusterCaCertificate", "")) or "")
        endpoints = preferred_endpoints(dns_endpoint=dns_endpoint, ip_endpoint=endpoint)
        if not cert_b64 or not endpoints:
            cluster_rows.append(
                {
                    "cluster": cluster.name,
                    "status": Status.FAILED.value,
                    "error": "missing cluster endpoint or CA certificate",
                }
            )
            errors.append(f"{cluster.name}: missing cluster endpoint or CA certificate")
            status = max_status(status, Status.FAILED)
            continue

        token = access_token(auth.credentials, allow_gcloud_fallback=auth_mode == "auto")
        if not token:
            cluster_rows.append(
                {
                    "cluster": cluster.name,
                    "status": Status.CRITICAL.value,
                    "error": "unable to obtain token for Kubernetes API",
                }
            )
            status = max_status(status, Status.CRITICAL)
            continue

        attempt_errors: list[str] = []
        mesh_result: dict[str, Any] | None = None
        terminal_api_error = False
        with cluster_ca_file(cert_b64) as ca_path:
            mesh_api_proxy_checks = _mesh_api_proxy_checks_for_cluster(
                config=config,
                project=cluster.project,
                cluster_name=cluster.name,
            )
            for candidate, use_cluster_ca in endpoints:
                try:
                    mesh_result = _query_mesh_health(
                        endpoint=candidate,
                        ca_path=ca_path if use_cluster_ca else None,
                        token=token,
                        timeout_seconds=timeout_seconds,
                        mesh_api_proxy_checks=mesh_api_proxy_checks,
                    )
                    mesh_result["endpoint"] = candidate
                    break
                except (MaxRetryError, NewConnectionError, TimeoutError) as exc:
                    attempt_errors.append(f"{candidate}: {exc}")
                    continue
                except k8s_exceptions.ApiException as exc:
                    attempt_errors.append(f"{candidate}: {exc}")
                    if exc.status in (401, 403):
                        cluster_rows.append(
                            {
                                "cluster": cluster.name,
                                "status": Status.SKIPPED_PERMISSION.value,
                                "error": str(exc),
                            }
                        )
                        status = max_status(status, Status.SKIPPED_PERMISSION)
                    elif exc.status in (404,):
                        cluster_rows.append(
                            {
                                "cluster": cluster.name,
                                "status": Status.SKIPPED_CONFIG.value,
                                "error": str(exc),
                            }
                        )
                        status = max_status(status, Status.SKIPPED_CONFIG)
                    else:
                        cluster_rows.append(
                            {
                                "cluster": cluster.name,
                                "status": Status.FAILED.value,
                                "error": str(exc),
                            }
                        )
                        status = max_status(status, Status.FAILED)
                    terminal_api_error = True
                    break

        if mesh_result is None:
            if terminal_api_error:
                continue
            if attempt_errors:
                cluster_rows.append(
                    {
                        "cluster": cluster.name,
                        "status": Status.SKIPPED_NETWORK.value,
                        "error": "; ".join(attempt_errors),
                    }
                )
                errors.append(f"{cluster.name}: {'; '.join(attempt_errors)}")
                status = max_status(status, Status.SKIPPED_NETWORK)
            continue

        ingress = _as_dict(mesh_result.get("ingress_gateway", {}))
        east_west = _as_dict(mesh_result.get("east_west_gateway", {}))
        istiod = _as_dict(mesh_result.get("istiod", {}))
        envoy_proxy_samples = _as_dict_list(mesh_result.get("envoy_proxy_samples", []))
        cluster_status = Status.OK
        components = [ingress, east_west]
        if "istiod" in mesh_result:
            components.append(istiod)
        for component in components:
            total = _as_int(component.get("total_pods", 0))
            ready = _as_int(component.get("ready_pods", 0))
            if total == 0:
                cluster_status = max_status(cluster_status, Status.WARNING)
            elif ready < total:
                cluster_status = max_status(cluster_status, Status.WARNING)
        mesh_api_proxies = _as_dict_list(
            mesh_result.get("mesh_api_proxies", mesh_result.get("squid_proxies", []))
        )
        for proxy in mesh_api_proxies:
            cluster_status = max_status(
                cluster_status,
                _status_from_value(str(proxy.get("status", Status.OK.value))),
            )
        for sample in envoy_proxy_samples:
            sample_status = _status_from_value(str(sample.get("status", Status.OK.value)))
            if sample_status in {Status.WARNING, Status.CRITICAL, Status.FAILED}:
                cluster_status = max_status(cluster_status, sample_status)

        remote_clusters = _collect_istio_remote_clusters(
            context=cluster.resolved_context(),
            timeout_seconds=timeout_seconds,
        )
        proxy_status = _collect_istio_proxy_status(
            context=cluster.resolved_context(),
            timeout_seconds=timeout_seconds,
        )
        expected_remote_links = max(0, len(clusters) - 1)
        remote_secret_count = _as_int(mesh_result.get("remote_secret_count", 0))
        remote_cluster_rows = _as_dict_list(remote_clusters.get("rows", []))
        synced_remote_clusters = sum(
            1
            for row in remote_cluster_rows
            if str(row.get("sync_status", "")).strip().lower() == "synced"
        )
        multicluster_status = Status.OK
        if (
            expected_remote_links > 0
            and str(remote_clusters.get("status", "")) == Status.OK.value
            and synced_remote_clusters < expected_remote_links
        ):
            multicluster_status = Status.WARNING
        cluster_status = max_status(cluster_status, multicluster_status)
        multicluster = {
            "status": multicluster_status.value,
            "expected_remote_links": expected_remote_links,
            "remote_cluster_count": len(remote_cluster_rows),
            "synced_remote_clusters": synced_remote_clusters,
            "missing_remote_links": max(0, expected_remote_links - synced_remote_clusters),
        }

        status = max_status(status, cluster_status)
        cluster_rows.append(
            {
                "cluster": cluster.name,
                "status": cluster_status.value,
                "endpoint": mesh_result.get("endpoint", ""),
                "namespace": "istio-system",
                "context": cluster.resolved_context(),
                "ingress_gateway": ingress,
                "east_west_gateway": east_west,
                "istiod": istiod,
                "remote_secret_count": remote_secret_count,
                "remote_secret_names": mesh_result.get("remote_secret_names", []),
                "mesh_api_proxies": mesh_api_proxies,
                "envoy_proxy_samples": envoy_proxy_samples,
                "remote_clusters": remote_clusters,
                "proxy_status": proxy_status,
                "multicluster_sync": multicluster,
                "control_plane_discovery": {
                    "status": Status.OK.value,
                    "istiod_ready_pods": _as_int(istiod.get("ready_pods", 0)),
                    "istiod_total_pods": _as_int(istiod.get("total_pods", 0)),
                },
            }
        )

    summary = f"Collected mesh posture for {len(cluster_rows)} clusters"
    return CheckResult(
        collector="mesh",
        status=status,
        summary=summary,
        details={"clusters": cluster_rows},
        errors=errors,
        started_at=started_at,
        finished_at=now_utc_iso(),
    )


def _query_mesh_health(
    endpoint: str,
    ca_path: Path | None,
    token: str,
    timeout_seconds: int,
    mesh_api_proxy_checks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    with api_client(endpoint=endpoint, token=token, ca_path=ca_path) as client:
        core_api = k8s_client.CoreV1Api(client)
        apps_api = k8s_client.AppsV1Api(client)
        pods = core_api.list_namespaced_pod(
            namespace="istio-system",
            _request_timeout=timeout_seconds,
        )
        secrets = core_api.list_namespaced_secret(
            namespace="istio-system",
            _request_timeout=timeout_seconds,
        )
        pod_items = list(pods.items)
        ingress_pods = [
            pod for pod in pod_items if _pod_matches(pod, ("ingressgateway", "istio-ingress"))
        ]
        east_west_pods = [pod for pod in pod_items if _pod_matches(pod, ("eastwest", "east-west"))]
        istiod_pods = [pod for pod in pod_items if _pod_matches(pod, ("istiod",))]
        ingress = _component_state(ingress_pods)
        east_west = _component_state(east_west_pods)
        istiod = _component_state(istiod_pods)
        mesh_api_proxies = [
            _query_mesh_api_proxy(
                core_api=core_api,
                apps_api=apps_api,
                check=check,
                timeout_seconds=timeout_seconds,
            )
            for check in mesh_api_proxy_checks or []
        ]
        envoy_proxy_samples = _collect_envoy_proxy_samples(
            core_api=core_api,
            components=(ingress, east_west),
            timeout_seconds=timeout_seconds,
        )
        _attach_mesh_api_proxy_log_summaries(
            core_api=core_api,
            apps_api=apps_api,
            proxies=mesh_api_proxies,
            timeout_seconds=timeout_seconds,
        )
        remote_secret_names = [
            name
            for name in (
                str(getattr(getattr(secret, "metadata", None), "name", "") or "")
                for secret in secrets.items
            )
            if name.startswith("istio-remote-secret") or "remote-secret" in name
        ]

    return {
        "ingress_gateway": ingress,
        "east_west_gateway": east_west,
        "istiod": istiod,
        "remote_secret_count": len(remote_secret_names),
        "remote_secret_names": sorted(remote_secret_names),
        "mesh_api_proxies": mesh_api_proxies,
        "envoy_proxy_samples": envoy_proxy_samples,
    }


def _collect_envoy_proxy_samples(
    *,
    core_api: Any,
    components: tuple[dict[str, Any], ...],
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    sample_timeout = _bounded_timeout(
        timeout_seconds,
        default_seconds=_ENVOY_SAMPLE_TIMEOUT_SECONDS,
    )
    pod_names: list[str] = []
    for component in components:
        for pod_name in _str_list(component.get("ready_pod_names", [])):
            if pod_name and pod_name not in pod_names:
                pod_names.append(pod_name)
    if not pod_names:
        return []

    samples: list[dict[str, Any]] = []
    for pod_name in pod_names[:_ENVOY_SAMPLE_LIMIT]:
        samples.append(
            _collect_envoy_server_info_sample(
                core_api=core_api,
                pod_name=pod_name,
                timeout_seconds=sample_timeout,
            )
        )
    return samples


def _collect_envoy_server_info_sample(
    *,
    core_api: Any,
    pod_name: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    try:
        raw = k8s_stream(
            core_api.connect_get_namespaced_pod_exec,
            pod_name,
            "istio-system",
            command=["pilot-agent", "request", "GET", "server_info"],
            container="istio-proxy",
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
            _request_timeout=max(1, timeout_seconds),
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "pod": pod_name,
            "status": _status_for_kubernetes_runtime_exception(
                exc,
                not_found_status=Status.WARNING,
            ).value,
            "error": _compact_kubernetes_runtime_error(exc),
        }

    payload = _as_dict(_parse_json(_text_from_kubernetes_response(raw)))
    node = _as_dict(payload.get("node"))
    metadata = _as_dict(node.get("metadata"))
    proxy_config = _as_dict(metadata.get("PROXY_CONFIG"))
    state = str(payload.get("state", "")).strip()
    sample_status = Status.OK if state == "LIVE" else Status.WARNING
    return {
        "pod": pod_name,
        "status": sample_status.value,
        "state": state,
        "istio_version": str(metadata.get("ISTIO_VERSION", "")),
        "cluster_id": str(metadata.get("CLUSTER_ID", "")),
        "network": str(metadata.get("NETWORK", "")),
        "discovery_address": str(proxy_config.get("discoveryAddress", "")),
        "service_cluster": str(node.get("cluster", "")),
    }


def _collect_istio_remote_clusters(*, context: str, timeout_seconds: int) -> dict[str, Any]:
    return _run_istioctl_table_command(
        command=["istioctl", "--context", context, "remote-clusters"],
        parser=_parse_istio_remote_clusters,
        timeout_seconds=timeout_seconds,
    )


def _collect_istio_proxy_status(*, context: str, timeout_seconds: int) -> dict[str, Any]:
    return _run_istioctl_table_command(
        command=["istioctl", "--context", context, "proxy-status"],
        parser=_parse_istio_proxy_status,
        timeout_seconds=timeout_seconds,
    )


def _run_istioctl_table_command(
    *,
    command: list[str],
    parser: Callable[[str], list[dict[str, str]]],
    timeout_seconds: int,
) -> dict[str, Any]:
    if not command[2]:
        return {
            "status": Status.SKIPPED_CONFIG.value,
            "rows": [],
            "error": "cluster context is not configured",
        }
    command_timeout = _bounded_timeout(
        timeout_seconds,
        default_seconds=_ISTIOCTL_TIMEOUT_SECONDS,
    )
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=command_timeout,
        )
    except FileNotFoundError:
        return {
            "status": Status.SKIPPED_CONFIG.value,
            "rows": [],
            "error": "istioctl command not found",
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "status": Status.SKIPPED_NETWORK.value,
            "rows": [],
            "error": _format_istioctl_timeout(command, exc.timeout),
        }

    if completed.returncode != 0:
        rendered = completed.stderr.strip() or completed.stdout.strip() or "istioctl failed"
        return {
            "status": _status_for_command_error(rendered).value,
            "rows": [],
            "error": rendered,
        }

    rows = parser(completed.stdout)
    return {
        "status": Status.OK.value,
        "rows": rows,
        "error": "",
    }


def _format_istioctl_timeout(command: list[str], timeout_seconds: float | None) -> str:
    operation = command[3] if len(command) > 3 and command[3] else "command"
    context = command[2] if len(command) > 2 and command[2] else "unknown context"
    timeout = int(timeout_seconds) if timeout_seconds is not None else _ISTIOCTL_TIMEOUT_SECONDS
    return f"istioctl {operation} timed out after {timeout} seconds for context {context}"


def _parse_istio_remote_clusters(raw: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower().startswith("name "):
            continue
        parts = stripped.split()
        if len(parts) < 3:
            continue
        if len(parts) == 3:
            name, sync_status, istiod = parts
            secret = ""
        else:
            name, secret, sync_status = parts[:3]
            istiod = " ".join(parts[3:])
        rows.append(
            {
                "name": name,
                "secret": secret,
                "sync_status": sync_status,
                "istiod": istiod,
            }
        )
    return rows


def _parse_istio_proxy_status(raw: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower().startswith("name "):
            continue
        parts = stripped.split()
        if len(parts) < 4:
            continue
        if len(parts) >= 9 and _looks_like_proxy_sync_state(parts[2]):
            name, cluster = parts[:2]
            sync_state = " ".join(parts[2:7])
            istiod = parts[7]
            version = parts[8]
            subscribed_types = " ".join(parts[9:])
        else:
            name, cluster, istiod, version = parts[:4]
            subscribed_types = " ".join(parts[4:])
            sync_state = subscribed_types
        rows.append(
            {
                "name": name,
                "cluster": cluster,
                "istiod": istiod,
                "version": version,
                "sync_state": sync_state,
                "subscribed_types": subscribed_types,
            }
        )
    return rows


def _looks_like_proxy_sync_state(value: str) -> bool:
    return value.upper() in {
        "SYNCED",
        "STALE",
        "NOT_SENT",
        "IGNORED",
        "ERROR",
    }


def _attach_mesh_api_proxy_log_summaries(
    *,
    core_api: Any,
    apps_api: Any,
    proxies: list[dict[str, Any]],
    timeout_seconds: int,
) -> None:
    log_supported_proxy_types = {"squid", "mesh-api-proxy"}
    for proxy in proxies:
        proxy_type = str(proxy.get("type", proxy.get("proxy_type", ""))).strip().lower()
        if proxy_type and proxy_type not in log_supported_proxy_types:
            continue
        proxy["control_plane_tunnel_logs"] = _collect_mesh_api_proxy_logs(
            core_api=core_api,
            apps_api=apps_api,
            proxy=proxy,
            timeout_seconds=timeout_seconds,
        )


def _collect_mesh_api_proxy_logs(
    *,
    core_api: Any,
    apps_api: Any,
    proxy: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    namespace = str(proxy.get("namespace", "")).strip()
    deployment = str(proxy.get("deployment", "")).strip()
    pod_names = _str_list(proxy.get("ready_pod_names", proxy.get("pod_names", [])))
    label_selector = str(proxy.get("label_selector", "")).strip()
    if not namespace:
        return {
            "status": Status.SKIPPED_CONFIG.value,
            "error": "proxy namespace is not configured",
        }
    if not pod_names and label_selector:
        try:
            pod_names = _pod_names_for_selector(
                core_api=core_api,
                namespace=namespace,
                label_selector=label_selector,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "status": _status_for_kubernetes_runtime_exception(
                    exc,
                    not_found_status=Status.SKIPPED_CONFIG,
                ).value,
                "error": _compact_kubernetes_runtime_error(exc),
            }
    if not pod_names and deployment:
        try:
            pod_names = _pod_names_for_deployment(
                core_api=core_api,
                apps_api=apps_api,
                namespace=namespace,
                deployment=deployment,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "status": _status_for_kubernetes_runtime_exception(
                    exc,
                    not_found_status=Status.SKIPPED_CONFIG,
                ).value,
                "error": _compact_kubernetes_runtime_error(exc),
            }
    if not pod_names:
        return {
            "status": Status.SKIPPED_CONFIG.value,
            "error": "proxy deployment or pod is not available",
        }

    command_timeout = _bounded_timeout(
        timeout_seconds,
        default_seconds=_PROXY_LOG_TIMEOUT_SECONDS,
    )
    try:
        summary = _read_mesh_api_proxy_log_summary(
            core_api=core_api,
            pod_name=pod_names[0],
            namespace=namespace,
            tail_lines=_PROXY_LOG_TAIL_LINES,
            timeout_seconds=command_timeout,
        )
        if (
            _as_int(summary.get("tunnel_line_count", 0)) == 0
            and _PROXY_LOG_EXTENDED_TAIL_LINES > _PROXY_LOG_TAIL_LINES
        ):
            summary = _read_mesh_api_proxy_log_summary(
                core_api=core_api,
                pod_name=pod_names[0],
                namespace=namespace,
                tail_lines=_PROXY_LOG_EXTENDED_TAIL_LINES,
                timeout_seconds=command_timeout,
            )
    except Exception as exc:  # noqa: BLE001
        return {
            "status": _status_for_kubernetes_runtime_exception(
                exc,
                not_found_status=Status.SKIPPED_CONFIG,
            ).value,
            "error": _compact_kubernetes_runtime_error(exc),
        }

    summary["status"] = Status.OK.value
    return summary


def _read_mesh_api_proxy_log_summary(
    *,
    core_api: Any,
    pod_name: str,
    namespace: str,
    tail_lines: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    raw = core_api.read_namespaced_pod_log(
        name=pod_name,
        namespace=namespace,
        tail_lines=tail_lines,
        _request_timeout=timeout_seconds,
    )
    return _parse_squid_access_log(_text_from_kubernetes_response(raw))


def _pod_names_for_selector(
    *,
    core_api: Any,
    namespace: str,
    label_selector: str,
    timeout_seconds: int,
) -> list[str]:
    pods = core_api.list_namespaced_pod(
        namespace=namespace,
        label_selector=label_selector,
        _request_timeout=timeout_seconds,
    )
    pod_state = _component_state(list(pods.items))
    ready_names = _str_list(pod_state.get("ready_pod_names", []))
    return ready_names or _str_list(pod_state.get("pod_names", []))


def _pod_names_for_deployment(
    *,
    core_api: Any,
    apps_api: Any,
    namespace: str,
    deployment: str,
    timeout_seconds: int,
) -> list[str]:
    workload = apps_api.read_namespaced_deployment(
        name=deployment,
        namespace=namespace,
        _request_timeout=timeout_seconds,
    )
    label_selector = _deployment_label_selector(workload)
    if not label_selector:
        return []
    return _pod_names_for_selector(
        core_api=core_api,
        namespace=namespace,
        label_selector=label_selector,
        timeout_seconds=timeout_seconds,
    )


def _deployment_label_selector(deployment: Any) -> str:
    selector = getattr(getattr(deployment, "spec", None), "selector", None)
    match_labels = getattr(selector, "match_labels", None) or {}
    if not isinstance(match_labels, dict):
        return ""
    labels = [
        f"{key}={value}"
        for key, value in sorted(match_labels.items())
        if str(key).strip() and str(value).strip()
    ]
    return ",".join(labels)


def _parse_squid_access_log(raw: str) -> dict[str, Any]:
    success_count = 0
    failure_count = 0
    latest_success: dict[str, str] = {}
    latest_failure: dict[str, str] = {}
    target_hosts: list[str] = []
    tunnel_lines = 0
    for line in raw.splitlines():
        parsed = _parse_squid_access_log_line(line)
        if not parsed:
            continue
        tunnel_lines += 1
        target = parsed["target"]
        if target and target not in target_hosts:
            target_hosts.append(target)
        if parsed["code"] == "200":
            success_count += 1
            latest_success = parsed
        else:
            failure_count += 1
            latest_failure = parsed
    return {
        "lines_checked": len(raw.splitlines()),
        "tunnel_line_count": tunnel_lines,
        "tunnel_success_count": success_count,
        "tunnel_failure_count": failure_count,
        "latest_success": latest_success,
        "latest_failure": latest_failure,
        "target_hosts": sorted(target_hosts),
    }


def _parse_squid_access_log_line(line: str) -> dict[str, str]:
    match = re.search(
        r"(?P<timestamp>\d+(?:\.\d+)?)\s+\d+\s+\S+\s+TCP_TUNNEL/"
        r"(?P<code>\d{3})\b.*?\sCONNECT\s+(?P<target>\S+)",
        line,
    )
    if match is None:
        return {}
    return {
        "timestamp": match.group("timestamp"),
        "code": match.group("code"),
        "target": match.group("target"),
        "raw": line.strip(),
    }


def _status_for_command_error(message: str) -> Status:
    lowered = message.lower()
    if "forbidden" in lowered or "unauthorized" in lowered:
        return Status.SKIPPED_PERMISSION
    if "not found" in lowered:
        return Status.SKIPPED_CONFIG
    if "timeout" in lowered or "i/o timeout" in lowered or "no such host" in lowered:
        return Status.SKIPPED_NETWORK
    return Status.WARNING


def _status_for_kubernetes_runtime_exception(
    exc: Exception,
    *,
    not_found_status: Status,
) -> Status:
    if isinstance(exc, k8s_exceptions.ApiException):
        if exc.status in (401, 403):
            return Status.SKIPPED_PERMISSION
        if exc.status == 404:
            return not_found_status
        if exc.status in (408, 429, 500, 502, 503, 504):
            return Status.SKIPPED_NETWORK
        return Status.FAILED

    lowered = str(exc).lower()
    if "forbidden" in lowered or "unauthorized" in lowered:
        return Status.SKIPPED_PERMISSION
    if "not found" in lowered:
        return not_found_status
    if "timeout" in lowered or "timed out" in lowered or "i/o timeout" in lowered:
        return Status.SKIPPED_NETWORK
    if "no such host" in lowered or "connection refused" in lowered:
        return Status.SKIPPED_NETWORK
    return Status.WARNING


def _compact_kubernetes_runtime_error(exc: Exception) -> str:
    if isinstance(exc, k8s_exceptions.ApiException):
        return _compact_k8s_error(exc)
    return str(exc)


def _text_from_kubernetes_response(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _parse_json(raw: str) -> object:
    text = raw.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            return ast.literal_eval(text)
        except (SyntaxError, ValueError):
            return {}


def _mesh_api_proxy_checks_for_cluster(
    *,
    config: EnvConfig,
    project: str,
    cluster_name: str,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for check in config.services.mesh_api_proxies:
        if _check_targets_cluster(check=check, project=project, cluster_name=cluster_name):
            checks.append(check)
    for check in config.services.squid_proxies:
        if not _check_targets_cluster(check=check, project=project, cluster_name=cluster_name):
            continue
        legacy_check = dict(check)
        legacy_check.setdefault("type", "squid")
        checks.append(legacy_check)
    return checks


def _check_targets_cluster(*, check: dict[str, Any], project: str, cluster_name: str) -> bool:
    target_project = str(check.get("project", "")).strip()
    target_cluster = str(check.get("cluster", "")).strip()
    if target_project and target_project != project:
        return False
    if target_cluster and target_cluster != cluster_name:
        return False
    return True


def _query_mesh_api_proxy(
    *,
    core_api: Any,
    apps_api: Any,
    check: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    name = str(check.get("name") or check.get("deployment") or check.get("service") or "").strip()
    namespace = str(check.get("namespace", "")).strip()
    deployment_name = str(check.get("deployment", "")).strip()
    service_name = str(check.get("service", "")).strip()
    label_selector = str(check.get("label_selector", "")).strip()
    proxy_type = str(check.get("type", check.get("proxy_type", "mesh-api-proxy"))).strip()
    expected_service_port = _first_int(
        check.get("service_port"),
        check.get("port"),
    )
    expected_endpoint_port = _first_int(
        check.get("endpoint_port"),
        check.get("target_port"),
        check.get("container_port"),
        check.get("port"),
    )
    configmap_name = str(check.get("configmap", "")).strip()
    certificate_secret_name = str(
        check.get("certificate_secret", check.get("cert_secret", ""))
    ).strip()
    token_secret_name = str(check.get("token_secret", "")).strip()
    if not name:
        name = deployment_name or service_name or "mesh-api-proxy"

    row: dict[str, Any] = {
        "name": name,
        "type": proxy_type,
        "namespace": namespace,
        "deployment": deployment_name,
        "service": service_name,
        "label_selector": label_selector,
        "expected_service_port": expected_service_port,
        "expected_endpoint_port": expected_endpoint_port,
        "configmap": configmap_name,
        "certificate_secret": certificate_secret_name,
        "token_secret": token_secret_name,
        "desired_replicas": 0,
        "ready_replicas": 0,
        "available_replicas": 0,
        "pod_total": 0,
        "pod_ready": 0,
        "pod_names": [],
        "ready_pod_names": [],
        "service_type": "",
        "service_ports": [],
        "load_balancer_ingress": [],
        "endpoint_address_count": 0,
        "endpoint_ports": [],
        "configmap_found": None,
        "certificate_secret_found": None,
        "token_secret_found": None,
        "status": Status.OK.value,
        "note": "",
    }
    findings: list[str] = []
    proxy_status = Status.OK

    if not namespace:
        row["status"] = Status.SKIPPED_CONFIG.value
        row["note"] = "namespace is not configured"
        return row

    if deployment_name:
        try:
            deployment = apps_api.read_namespaced_deployment(
                name=deployment_name,
                namespace=namespace,
                _request_timeout=timeout_seconds,
            )
            deployment_spec = getattr(deployment, "spec", None)
            deployment_status = getattr(deployment, "status", None)
            desired = _as_int(getattr(deployment_spec, "replicas", 0))
            ready = _as_int(getattr(deployment_status, "ready_replicas", 0))
            available = _as_int(getattr(deployment_status, "available_replicas", 0))
            row["desired_replicas"] = desired
            row["ready_replicas"] = ready
            row["available_replicas"] = available
            if desired <= 0:
                findings.append("deployment has no desired replicas")
                proxy_status = max_status(proxy_status, Status.WARNING)
            elif ready < desired or available < desired:
                findings.append("deployment replicas are not fully ready")
                proxy_status = max_status(proxy_status, Status.WARNING)
        except k8s_exceptions.ApiException as exc:
            findings.append(f"deployment query failed: {_compact_k8s_error(exc)}")
            proxy_status = max_status(proxy_status, _status_for_mesh_proxy_api_exception(exc))
    else:
        findings.append("deployment is not configured")
        proxy_status = max_status(proxy_status, Status.SKIPPED_CONFIG)

    if label_selector:
        try:
            pods = core_api.list_namespaced_pod(
                namespace=namespace,
                label_selector=label_selector,
                _request_timeout=timeout_seconds,
            )
            pod_state = _component_state(list(pods.items))
            row["pod_total"] = pod_state["total_pods"]
            row["pod_ready"] = pod_state["ready_pods"]
            row["pod_names"] = pod_state["pod_names"]
            row["ready_pod_names"] = pod_state["ready_pod_names"]
            if _as_int(pod_state["total_pods"]) == 0:
                findings.append("no pods match the configured label selector")
                proxy_status = max_status(proxy_status, Status.WARNING)
            elif _as_int(pod_state["ready_pods"]) < _as_int(pod_state["total_pods"]):
                findings.append("not all matching pods are ready")
                proxy_status = max_status(proxy_status, Status.WARNING)
        except k8s_exceptions.ApiException as exc:
            findings.append(f"pod query failed: {_compact_k8s_error(exc)}")
            proxy_status = max_status(proxy_status, _status_for_mesh_proxy_api_exception(exc))
    else:
        findings.append("label selector is not configured")
        proxy_status = max_status(proxy_status, Status.SKIPPED_CONFIG)

    if service_name:
        try:
            service = core_api.read_namespaced_service(
                name=service_name,
                namespace=namespace,
                _request_timeout=timeout_seconds,
            )
            service_spec = getattr(service, "spec", None)
            service_status = getattr(service, "status", None)
            row["service_type"] = str(getattr(service_spec, "type", "") or "")
            row["service_ports"] = _service_ports(service_spec)
            row["load_balancer_ingress"] = _load_balancer_ingress(service_status)
            if expected_service_port and str(expected_service_port) not in row["service_ports"]:
                findings.append(f"service does not expose expected port {expected_service_port}")
                proxy_status = max_status(proxy_status, Status.WARNING)
        except k8s_exceptions.ApiException as exc:
            findings.append(f"service query failed: {_compact_k8s_error(exc)}")
            proxy_status = max_status(proxy_status, _status_for_mesh_proxy_api_exception(exc))

        try:
            endpoints = core_api.read_namespaced_endpoints(
                name=service_name,
                namespace=namespace,
                _request_timeout=timeout_seconds,
            )
            row["endpoint_address_count"] = _endpoint_address_count(endpoints)
            row["endpoint_ports"] = _endpoint_ports(endpoints)
            if _as_int(row["endpoint_address_count"]) <= 0:
                findings.append("service has no ready endpoint addresses")
                proxy_status = max_status(proxy_status, Status.WARNING)
            if expected_endpoint_port and str(expected_endpoint_port) not in row["endpoint_ports"]:
                findings.append(f"endpoints do not expose expected port {expected_endpoint_port}")
                proxy_status = max_status(proxy_status, Status.WARNING)
        except k8s_exceptions.ApiException as exc:
            findings.append(f"endpoint query failed: {_compact_k8s_error(exc)}")
            proxy_status = max_status(proxy_status, _status_for_mesh_proxy_api_exception(exc))
    else:
        findings.append("service is not configured")
        proxy_status = max_status(proxy_status, Status.SKIPPED_CONFIG)

    for resource_name, row_key, label, reader_name in (
        (
            configmap_name,
            "configmap_found",
            "configmap",
            "read_namespaced_config_map",
        ),
        (
            certificate_secret_name,
            "certificate_secret_found",
            "certificate secret",
            "read_namespaced_secret",
        ),
        (
            token_secret_name,
            "token_secret_found",
            "token secret",
            "read_namespaced_secret",
        ),
    ):
        if not resource_name:
            continue
        try:
            reader = getattr(core_api, reader_name)
            reader(
                name=resource_name,
                namespace=namespace,
                _request_timeout=timeout_seconds,
            )
            row[row_key] = True
        except k8s_exceptions.ApiException as exc:
            row[row_key] = False
            findings.append(f"{label} query failed: {_compact_k8s_error(exc)}")
            proxy_status = max_status(proxy_status, _status_for_mesh_proxy_api_exception(exc))

    row["status"] = proxy_status.value
    row["note"] = "; ".join(findings)
    return row


def _query_squid_proxy(
    *,
    core_api: Any,
    apps_api: Any,
    check: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    legacy_check = dict(check)
    legacy_check.setdefault("type", "squid")
    return _query_mesh_api_proxy(
        core_api=core_api,
        apps_api=apps_api,
        check=legacy_check,
        timeout_seconds=timeout_seconds,
    )


def _service_ports(service_spec: Any) -> list[str]:
    ports = getattr(service_spec, "ports", None) or []
    values: list[str] = []
    for port in ports:
        port_value = _as_int(getattr(port, "port", 0))
        if port_value:
            values.append(str(port_value))
    return sorted(set(values))


def _load_balancer_ingress(service_status: Any) -> list[str]:
    load_balancer = getattr(service_status, "load_balancer", None)
    ingress = getattr(load_balancer, "ingress", None) or []
    values: list[str] = []
    for item in ingress:
        value = str(getattr(item, "ip", "") or getattr(item, "hostname", "") or "").strip()
        if value:
            values.append(value)
    return sorted(set(values))


def _endpoint_address_count(endpoints: Any) -> int:
    count = 0
    for subset in getattr(endpoints, "subsets", None) or []:
        count += len(getattr(subset, "addresses", None) or [])
    return count


def _endpoint_ports(endpoints: Any) -> list[str]:
    values: list[str] = []
    for subset in getattr(endpoints, "subsets", None) or []:
        for port in getattr(subset, "ports", None) or []:
            port_value = _as_int(getattr(port, "port", 0))
            if port_value:
                values.append(str(port_value))
    return sorted(set(values))


def _status_for_mesh_proxy_api_exception(exc: k8s_exceptions.ApiException) -> Status:
    if exc.status in (401, 403):
        return Status.SKIPPED_PERMISSION
    if exc.status == 404:
        return Status.WARNING
    return Status.FAILED


def _status_for_squid_api_exception(exc: k8s_exceptions.ApiException) -> Status:
    return _status_for_mesh_proxy_api_exception(exc)


def _compact_k8s_error(exc: k8s_exceptions.ApiException) -> str:
    reason = str(getattr(exc, "reason", "") or "").strip()
    if reason:
        return f"{exc.status} {reason}".strip()
    return str(exc)


def _status_from_value(value: str) -> Status:
    try:
        return Status(value)
    except ValueError:
        return Status.FAILED


def _component_state(pods: list[Any]) -> dict[str, Any]:
    ready = sum(1 for pod in pods if _pod_ready(pod))
    pod_names = [
        str(getattr(getattr(pod, "metadata", None), "name", "") or "")
        for pod in pods
        if str(getattr(getattr(pod, "metadata", None), "name", "") or "")
    ]
    ready_names = [
        str(getattr(getattr(pod, "metadata", None), "name", "") or "")
        for pod in pods
        if _pod_ready(pod) and str(getattr(getattr(pod, "metadata", None), "name", "") or "")
    ]
    container_images: list[str] = []
    versions: list[str] = []
    for pod in pods:
        for image in _pod_container_images(pod):
            if image not in container_images:
                container_images.append(image)
            version = _container_image_version(image)
            if version and version not in versions:
                versions.append(version)
    return {
        "total_pods": len(pods),
        "ready_pods": ready,
        "pod_names": pod_names,
        "ready_pod_names": ready_names,
        "container_images": container_images,
        "versions": versions,
        "version": ", ".join(versions[:3]),
    }


def _pod_container_images(pod: Any) -> list[str]:
    spec = getattr(pod, "spec", None)
    containers = getattr(spec, "containers", None) or []
    images: list[str] = []
    for container in containers:
        image = str(getattr(container, "image", "") or "").strip()
        if image:
            images.append(image)
    return images


def _container_image_version(image: str) -> str:
    without_digest = image.split("@", maxsplit=1)[0]
    if ":" not in without_digest:
        return ""
    tag = without_digest.rsplit(":", maxsplit=1)[-1].strip()
    if not tag or tag == "latest":
        return ""
    return tag


def _pod_matches(pod: Any, tokens: tuple[str, ...]) -> bool:
    metadata = getattr(pod, "metadata", None)
    if metadata is None:
        return False
    name = str(getattr(metadata, "name", "") or "").lower()
    labels = getattr(metadata, "labels", {}) or {}
    app_value = str(_as_dict(labels).get("app", "")).lower()
    combined = f"{name} {app_value}"
    return any(token in combined for token in tokens)


def _pod_ready(pod: Any) -> bool:
    status = getattr(pod, "status", None)
    if status is None:
        return False
    phase = str(getattr(status, "phase", "") or "")
    if phase != "Running":
        return False
    statuses = getattr(status, "container_statuses", None) or []
    if not statuses:
        return False
    return all(bool(getattr(item, "ready", False)) for item in statuses)


def _bounded_timeout(timeout_seconds: int, *, default_seconds: int) -> int:
    return max(1, min(max(1, timeout_seconds), default_seconds))


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


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


def _first_int(*values: Any) -> int:
    for value in values:
        integer = _as_int(value)
        if integer:
            return integer
    return 0
