from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from googleapiclient.errors import HttpError
from kubernetes import client as k8s_client
from kubernetes.client import exceptions as k8s_exceptions
from urllib3.exceptions import MaxRetryError, NewConnectionError

from opsbrief.cluster_discovery import resolve_clusters
from opsbrief.collectors.prometheus_monitoring import probe as probe_prometheus_monitoring
from opsbrief.config import EnvConfig
from opsbrief.gcp_api import (
    build_alert_policy_service_client,
    build_cloud_redis_client,
    build_cloud_sql_admin_service,
    build_cluster_manager_client,
    build_compute_backend_services_client,
    build_compute_instances_client,
    build_compute_networks_client,
    build_dns_client,
    build_gke_backup_client,
    build_logging_client,
    build_logging_config_service_client,
    build_managed_kafka_client,
    build_metric_service_client,
    build_service,
    get_gke_cluster,
    list_cloud_sql_instances,
)
from opsbrief.gcp_auth import AuthMode, get_auth_bundle
from opsbrief.k8s_api import access_token, api_client, cluster_ca_file, preferred_endpoints
from opsbrief.models import CheckResult, Status, now_utc_iso
from opsbrief.status import classify_http_error, max_status


def collect(
    config: EnvConfig,
    timeout_seconds: int = 30,
    output_dir: str | None = None,
    auth_mode: AuthMode = "auto",
    impersonate_service_account: str = "",
) -> CheckResult:
    started_at = now_utc_iso()
    checks: list[dict[str, str]] = []
    errors: list[str] = []
    status = Status.OK

    try:
        auth = get_auth_bundle(
            auth_mode=auth_mode, impersonate_service_account=impersonate_service_account
        )
        checks.append(
            {
                "name": "credentials",
                "status": "ok",
                "message": (
                    f"default_project={auth.default_project or 'unset'} "
                    f"principal={auth.principal_hint}"
                ),
            }
        )
    except Exception as exc:  # noqa: BLE001
        checks.append({"name": "credentials", "status": "critical", "message": "failed"})
        errors.append(str(exc))
        return CheckResult(
            collector="preflight",
            status=Status.CRITICAL,
            summary="Credential initialization failed",
            details={"checks": checks},
            errors=errors,
            started_at=started_at,
            finished_at=now_utc_iso(),
        )

    try:
        container = build_service(auth, "container", "v1", timeout_seconds)
        cluster_client = None
        try:
            cluster_client = build_cluster_manager_client(auth)
        except Exception:  # noqa: BLE001
            cluster_client = None
    except Exception as exc:  # noqa: BLE001
        checks.append(
            {"name": "api:container", "status": "critical", "message": "client_init_failed"}
        )
        errors.append(str(exc))
        status = max_status(status, Status.CRITICAL)
        container = None
        cluster_client = None

    if container is not None:
        clusters = resolve_clusters(
            config,
            container,
            cluster_client=cluster_client,
            timeout_seconds=timeout_seconds,
        )
        if not clusters:
            checks.append(
                {
                    "name": "cluster_discovery",
                    "status": Status.SKIPPED_CONFIG.value,
                    "message": "no clusters configured or discovered",
                }
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
                endpoint = payload.get("endpoint", "")
                checks.append(
                    {
                        "name": f"cluster:{cluster.name}",
                        "status": "ok",
                        "message": endpoint or "endpoint-missing",
                    }
                )
            except HttpError as exc:
                failure_status = classify_http_error(exc)
                checks.append(
                    {
                        "name": f"cluster:{cluster.name}",
                        "status": failure_status.value,
                        "message": str(exc),
                    }
                )
                errors.append(f"{cluster.name}: {exc}")
                status = max_status(status, failure_status)
                continue
            except Exception as exc:  # noqa: BLE001
                checks.append(
                    {
                        "name": f"cluster:{cluster.name}",
                        "status": Status.FAILED.value,
                        "message": str(exc),
                    }
                )
                errors.append(f"{cluster.name}: {exc}")
                status = max_status(status, Status.FAILED)
                continue

            kube_status, kube_checks = _probe_kubernetes_permissions(
                auth=auth,
                cluster_name=cluster.name,
                cluster_payload=payload,
                timeout_seconds=timeout_seconds,
                auth_mode=auth_mode,
            )
            for item in kube_checks:
                checks.append(item)
                if item["status"] == Status.CRITICAL.value:
                    errors.append(f"{cluster.name}/{item['name']}: {item['message']}")
            status = max_status(status, kube_status)

    status = max_status(
        status,
        _probe_api_for_collector(config, "monitoring", auth, checks, errors, timeout_seconds),
    )
    status = max_status(
        status,
        _probe_api_for_collector(
            config,
            "prometheus_monitoring",
            auth,
            checks,
            errors,
            timeout_seconds,
        ),
    )
    status = max_status(
        status, _probe_api_for_collector(config, "logging", auth, checks, errors, timeout_seconds)
    )
    status = max_status(
        status, _probe_api_for_collector(config, "audit", auth, checks, errors, timeout_seconds)
    )
    status = max_status(
        status, _probe_api_for_collector(config, "network", auth, checks, errors, timeout_seconds)
    )
    status = max_status(
        status, _probe_api_for_collector(config, "mesh", auth, checks, errors, timeout_seconds)
    )
    status = max_status(
        status,
        _probe_api_for_collector(config, "trend_metrics", auth, checks, errors, timeout_seconds),
    )
    status = max_status(
        status, _probe_api_for_collector(config, "backup", auth, checks, errors, timeout_seconds)
    )
    status = max_status(
        status, _probe_api_for_collector(config, "services", auth, checks, errors, timeout_seconds)
    )

    if output_dir:
        path = Path(output_dir)
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe_file = path / ".write_probe"
            probe_file.write_text("ok", encoding="utf-8")
            probe_file.unlink()
            checks.append({"name": "output_dir", "status": "ok", "message": str(path)})
        except OSError as exc:
            checks.append({"name": "output_dir", "status": "critical", "message": str(path)})
            errors.append(f"output_dir not writable: {exc}")
            status = max_status(status, Status.CRITICAL)

    summary = (
        "All preflight checks passed" if status == Status.OK else "Preflight has warnings/errors"
    )
    details: dict[str, Any] = {
        "environment": config.environment,
        "projects": config.projects,
        "checks": checks,
    }
    return CheckResult(
        collector="preflight",
        status=status,
        summary=summary,
        details=details,
        errors=errors,
        started_at=started_at,
        finished_at=now_utc_iso(),
    )


def _probe_api_for_collector(
    config: EnvConfig,
    collector_name: str,
    auth: Any,
    checks: list[dict[str, str]],
    errors: list[str],
    timeout_seconds: int,
) -> Status:
    if not config.collectors.get(collector_name, False):
        checks.append(
            {
                "name": f"api:{collector_name}",
                "status": Status.SKIPPED_CONFIG.value,
                "message": "collector disabled",
            }
        )
        return Status.SKIPPED_CONFIG

    if collector_name == "prometheus_monitoring":
        probe_status, message = probe_prometheus_monitoring(config, timeout_seconds)
        checks.append(
            {
                "name": f"api:{collector_name}",
                "status": probe_status.value,
                "message": message,
            }
        )
        if probe_status != Status.OK:
            errors.append(f"{collector_name}: {message}")
        return probe_status

    project = _first_project(config)
    if not project:
        checks.append(
            {
                "name": f"api:{collector_name}",
                "status": Status.SKIPPED_CONFIG.value,
                "message": "no project configured",
            }
        )
        return Status.SKIPPED_CONFIG

    try:
        if collector_name == "monitoring":
            _probe_monitoring_alert_policies_api(auth, project, timeout_seconds)
        elif collector_name == "logging":
            _probe_logging_sinks_api(auth, project, timeout_seconds)
        elif collector_name == "audit":
            _probe_logging_entries_api(auth, project, timeout_seconds)
        elif collector_name == "network":
            _probe_compute_networks_api(auth, project, timeout_seconds)
            _probe_dns_zones_api(auth, project, timeout_seconds)
        elif collector_name == "mesh":
            _probe_container_clusters_api(auth, project, timeout_seconds)
        elif collector_name == "trend_metrics":
            _probe_monitoring_metric_descriptors_api(auth, project, timeout_seconds)
            _probe_cloud_sql_api(auth, project, timeout_seconds)
            _probe_compute_instances_api(auth, project, timeout_seconds)
        elif collector_name == "backup":
            _probe_gke_backup_api(auth, project, config.default_region, timeout_seconds)
        elif collector_name == "services":
            service_probe_status = _probe_services_api(config, auth, project, timeout_seconds)
            if service_probe_status == Status.SKIPPED_CONFIG:
                checks.append(
                    {
                        "name": f"api:{collector_name}",
                        "status": Status.SKIPPED_CONFIG.value,
                        "message": "no service checks enabled",
                    }
                )
                return Status.SKIPPED_CONFIG
        else:
            return Status.SKIPPED_CONFIG
        checks.append({"name": f"api:{collector_name}", "status": "ok", "message": "reachable"})
        return Status.OK
    except HttpError as exc:
        failure_status = classify_http_error(exc)
        checks.append(
            {
                "name": f"api:{collector_name}",
                "status": failure_status.value,
                "message": str(exc),
            }
        )
        errors.append(f"{collector_name}: {exc}")
        return failure_status
    except Exception as exc:  # noqa: BLE001
        checks.append(
            {
                "name": f"api:{collector_name}",
                "status": Status.FAILED.value,
                "message": str(exc),
            }
        )
        errors.append(f"{collector_name}: {exc}")
        return Status.FAILED


def _probe_services_api(
    config: EnvConfig,
    auth: Any,
    project: str,
    timeout_seconds: int,
) -> Status:
    if config.services.cloud_sql:
        _probe_cloud_sql_api(auth, project, timeout_seconds)
        return Status.OK
    if config.services.redis:
        _probe_redis_api(auth, project, timeout_seconds)
        return Status.OK
    if config.services.managed_kafka:
        parent = f"projects/{project}/locations/-"
        try:
            client = build_managed_kafka_client(auth)
            _ = next(
                iter(client.list_clusters(parent=parent, timeout=max(1, timeout_seconds))), None
            )
        except Exception:  # noqa: BLE001
            service = build_service(auth, "managedkafka", "v1", timeout_seconds)
            service.projects().locations().clusters().list(
                parent=parent,
                pageSize=1,
            ).execute()
        return Status.OK
    if config.services.compute_instances or config.discovery.auto_discover_compute_instances:
        _probe_compute_instances_api(auth, project, timeout_seconds, max_results=1)
        return Status.OK
    if config.services.load_balancers or config.discovery.auto_discover_load_balancers:
        _probe_compute_backend_services_api(auth, project, timeout_seconds, max_results=1)
        return Status.OK
    return Status.SKIPPED_CONFIG


def _probe_monitoring_alert_policies_api(auth: Any, project: str, timeout_seconds: int) -> None:
    try:
        client = build_alert_policy_service_client(auth)
        _consume_probe(
            client.list_alert_policies(
                request={"name": f"projects/{project}", "page_size": 1},
                timeout=max(1, timeout_seconds),
            )
        )
        return
    except Exception:  # noqa: BLE001
        service = build_service(auth, "monitoring", "v3", timeout_seconds)
        service.projects().alertPolicies().list(
            name=f"projects/{project}",
            pageSize=1,
        ).execute()


def _probe_monitoring_metric_descriptors_api(auth: Any, project: str, timeout_seconds: int) -> None:
    try:
        client = build_metric_service_client(auth)
        _consume_probe(
            client.list_metric_descriptors(
                request={"name": f"projects/{project}", "page_size": 1},
                timeout=max(1, timeout_seconds),
            )
        )
        return
    except Exception:  # noqa: BLE001
        monitoring = build_service(auth, "monitoring", "v3", timeout_seconds)
        monitoring.projects().metricDescriptors().list(
            name=f"projects/{project}",
            pageSize=1,
        ).execute()


def _probe_container_clusters_api(auth: Any, project: str, timeout_seconds: int) -> None:
    try:
        client = build_cluster_manager_client(auth)
        response = client.list_clusters(
            parent=f"projects/{project}/locations/-",
            timeout=max(1, timeout_seconds),
        )
        _consume_probe(getattr(response, "clusters", []))
        return
    except Exception:  # noqa: BLE001
        service = build_service(auth, "container", "v1", timeout_seconds)
        service.projects().locations().clusters().list(
            parent=f"projects/{project}/locations/-"
        ).execute()


def _probe_gke_backup_api(
    auth: Any,
    project: str,
    region: str,
    timeout_seconds: int,
) -> None:
    parent = f"projects/{project}/locations/{region}"
    try:
        client = build_gke_backup_client(auth)
        _consume_probe(
            client.list_backup_plans(
                parent=parent,
                timeout=max(1, timeout_seconds),
            )
        )
        return
    except Exception:  # noqa: BLE001
        service = build_service(auth, "gkebackup", "v1", timeout_seconds)
        service.projects().locations().backupPlans().list(
            parent=parent,
            pageSize=1,
        ).execute()


def _probe_redis_api(auth: Any, project: str, timeout_seconds: int) -> None:
    parent = f"projects/{project}/locations/-"
    try:
        client = build_cloud_redis_client(auth)
        _consume_probe(
            client.list_instances(
                parent=parent,
                timeout=max(1, timeout_seconds),
            )
        )
        return
    except Exception:  # noqa: BLE001
        service = build_service(auth, "redis", "v1", timeout_seconds)
        service.projects().locations().instances().list(
            parent=parent,
            pageSize=1,
        ).execute()


def _probe_logging_sinks_api(auth: Any, project: str, timeout_seconds: int) -> None:
    try:
        client = build_logging_config_service_client(auth)
        _consume_probe(
            client.list_sinks(
                parent=f"projects/{project}",
                timeout=max(1, timeout_seconds),
            )
        )
        return
    except Exception:  # noqa: BLE001
        service = build_service(auth, "logging", "v2", timeout_seconds)
        service.projects().sinks().list(parent=f"projects/{project}", pageSize=1).execute()


def _probe_cloud_sql_api(auth: Any, project: str, timeout_seconds: int) -> None:
    service = build_cloud_sql_admin_service(auth, timeout_seconds)
    list_cloud_sql_instances(service, project)


def _probe_logging_entries_api(auth: Any, project: str, timeout_seconds: int) -> None:
    try:
        client: Any = build_logging_client(auth)
        _consume_probe(
            client.list_entries(
                resource_names=[f"projects/{project}"],
                order_by="timestamp desc",
                max_results=1,
                page_size=1,
            )
        )
        return
    except Exception:  # noqa: BLE001
        service = build_service(auth, "logging", "v2", timeout_seconds)
        service.entries().list(
            body={
                "resourceNames": [f"projects/{project}"],
                "orderBy": "timestamp desc",
                "pageSize": 1,
            }
        ).execute()


def _probe_compute_networks_api(auth: Any, project: str, timeout_seconds: int) -> None:
    try:
        client = build_compute_networks_client(auth)
        _consume_probe(client.list(project=project, timeout=max(1, timeout_seconds)))
        return
    except Exception:  # noqa: BLE001
        compute = build_service(auth, "compute", "v1", timeout_seconds)
        compute.networks().list(project=project).execute()


def _probe_dns_zones_api(auth: Any, project: str, timeout_seconds: int) -> None:
    try:
        client = build_dns_client(auth, project)
        _consume_probe(client.list_zones(max_results=1))
        return
    except Exception:  # noqa: BLE001
        dns = build_service(auth, "dns", "v1", timeout_seconds)
        dns.managedZones().list(project=project).execute()


def _probe_compute_instances_api(
    auth: Any,
    project: str,
    timeout_seconds: int,
    *,
    max_results: int | None = None,
) -> None:
    try:
        client = build_compute_instances_client(auth)
        request: dict[str, Any] = {"project": project}
        if max_results is not None:
            request["max_results"] = max(1, max_results)
        _consume_probe(client.aggregated_list(request=request, timeout=max(1, timeout_seconds)))
        return
    except Exception:  # noqa: BLE001
        compute = build_service(auth, "compute", "v1", timeout_seconds)
        kwargs: dict[str, Any] = {"project": project}
        if max_results is not None:
            kwargs["maxResults"] = max(1, max_results)
        compute.instances().aggregatedList(**kwargs).execute()


def _probe_compute_backend_services_api(
    auth: Any,
    project: str,
    timeout_seconds: int,
    *,
    max_results: int | None = None,
) -> None:
    try:
        client = build_compute_backend_services_client(auth)
        request: dict[str, Any] = {"project": project}
        if max_results is not None:
            request["max_results"] = max(1, max_results)
        _consume_probe(client.aggregated_list(request=request, timeout=max(1, timeout_seconds)))
        return
    except Exception:  # noqa: BLE001
        compute = build_service(auth, "compute", "v1", timeout_seconds)
        kwargs: dict[str, Any] = {"project": project}
        if max_results is not None:
            kwargs["maxResults"] = max(1, max_results)
        compute.backendServices().aggregatedList(**kwargs).execute()


def _consume_probe(items: Iterable[Any]) -> None:
    for _ in items:
        return


def _first_project(config: EnvConfig) -> str:
    if config.projects:
        return next(iter(config.projects.values()))
    if config.clusters:
        return config.clusters[0].project
    return ""


def _probe_kubernetes_permissions(
    auth: Any,
    cluster_name: str,
    cluster_payload: dict[str, Any],
    timeout_seconds: int,
    auth_mode: AuthMode,
) -> tuple[Status, list[dict[str, str]]]:
    cert_b64 = ((cluster_payload.get("masterAuth") or {}).get("clusterCaCertificate", "")) or ""
    endpoint = str(cluster_payload.get("endpoint", ""))
    dns_endpoint = (
        (
            (cluster_payload.get("controlPlaneEndpointsConfig") or {}).get("dnsEndpointConfig")
            or {}
        ).get("endpoint", "")
    ) or ""
    endpoints = preferred_endpoints(dns_endpoint=dns_endpoint, ip_endpoint=endpoint)
    if not cert_b64 or not endpoints:
        return Status.CRITICAL, [
            {
                "name": f"k8s:{cluster_name}:api_access",
                "status": Status.CRITICAL.value,
                "message": "missing endpoint or cluster CA in GKE API response",
            }
        ]

    token = access_token(auth.credentials, allow_gcloud_fallback=auth_mode == "auto")
    if not token:
        return Status.CRITICAL, [
            {
                "name": f"k8s:{cluster_name}:api_access",
                "status": Status.CRITICAL.value,
                "message": "unable to obtain token for Kubernetes API",
            }
        ]

    checks: list[dict[str, str]] = []
    final_status = Status.OK
    attempt_errors: list[str] = []
    with cluster_ca_file(cert_b64) as ca_path:
        for candidate, use_cluster_ca in endpoints:
            try:
                candidate_checks, candidate_status = _run_kubernetes_checks(
                    cluster_name=cluster_name,
                    endpoint=candidate,
                    ca_path=ca_path if use_cluster_ca else None,
                    token=token,
                    timeout_seconds=timeout_seconds,
                )
                checks.extend(candidate_checks)
                final_status = max_status(final_status, candidate_status)
                return final_status, checks
            except (MaxRetryError, NewConnectionError, TimeoutError) as exc:
                attempt_errors.append(f"{candidate}: {exc}")
                continue
            except k8s_exceptions.ApiException as exc:
                attempt_errors.append(f"{candidate}: {exc}")
                break

    error_text = "; ".join(attempt_errors) if attempt_errors else "unable to reach Kubernetes API"
    checks.append(
        {
            "name": f"k8s:{cluster_name}:api_access",
            "status": Status.CRITICAL.value,
            "message": error_text,
        }
    )
    return Status.CRITICAL, checks


def _run_kubernetes_checks(
    cluster_name: str,
    endpoint: str,
    ca_path: Path | None,
    token: str,
    timeout_seconds: int,
) -> tuple[list[dict[str, str]], Status]:
    checks: list[dict[str, str]] = []
    status = Status.OK

    with api_client(endpoint=endpoint, token=token, ca_path=ca_path) as client:
        core_api = k8s_client.CoreV1Api(client)
        apps_api = k8s_client.AppsV1Api(client)
        autoscaling_api = k8s_client.AutoscalingV2Api(client)
        custom_api = k8s_client.CustomObjectsApi(client)

        probes: list[tuple[str, bool, Callable[[], Any]]] = [
            (
                "namespaces:list",
                True,
                lambda: core_api.list_namespace(limit=1, _request_timeout=timeout_seconds),
            ),
            (
                "nodes:list",
                True,
                lambda: core_api.list_node(limit=1, _request_timeout=timeout_seconds),
            ),
            (
                "pods:list",
                True,
                lambda: core_api.list_pod_for_all_namespaces(
                    limit=1, _request_timeout=timeout_seconds
                ),
            ),
            (
                "deployments:list",
                True,
                lambda: apps_api.list_deployment_for_all_namespaces(
                    limit=1, _request_timeout=timeout_seconds
                ),
            ),
            (
                "statefulsets:list",
                True,
                lambda: apps_api.list_stateful_set_for_all_namespaces(
                    limit=1, _request_timeout=timeout_seconds
                ),
            ),
            (
                "daemonsets:list",
                True,
                lambda: apps_api.list_daemon_set_for_all_namespaces(
                    limit=1, _request_timeout=timeout_seconds
                ),
            ),
            (
                "hpas:list",
                True,
                lambda: autoscaling_api.list_horizontal_pod_autoscaler_for_all_namespaces(
                    limit=1, _request_timeout=timeout_seconds
                ),
            ),
            (
                "events:list",
                True,
                lambda: core_api.list_event_for_all_namespaces(
                    limit=1, _request_timeout=timeout_seconds
                ),
            ),
            (
                "metrics_nodes:list",
                False,
                lambda: custom_api.list_cluster_custom_object(
                    group="metrics.k8s.io",
                    version="v1beta1",
                    plural="nodes",
                    _request_timeout=timeout_seconds,
                ),
            ),
        ]

        for probe_name, required, probe_fn in probes:
            try:
                probe_fn()
            except k8s_exceptions.ApiException as exc:
                mapped = _classify_k8s_api_exception(exc)
                if required:
                    checks.append(
                        {
                            "name": f"k8s:{cluster_name}:{probe_name}",
                            "status": Status.CRITICAL.value,
                            "message": str(exc),
                        }
                    )
                    status = max_status(status, Status.CRITICAL)
                else:
                    checks.append(
                        {
                            "name": f"k8s:{cluster_name}:{probe_name}",
                            "status": mapped.value,
                            "message": str(exc),
                        }
                    )
                    status = max_status(status, mapped)
            except (MaxRetryError, NewConnectionError, TimeoutError) as exc:
                checks.append(
                    {
                        "name": f"k8s:{cluster_name}:{probe_name}",
                        "status": Status.CRITICAL.value,
                        "message": str(exc),
                    }
                )
                status = max_status(status, Status.CRITICAL)
            except Exception as exc:  # noqa: BLE001
                checks.append(
                    {
                        "name": f"k8s:{cluster_name}:{probe_name}",
                        "status": Status.CRITICAL.value,
                        "message": str(exc),
                    }
                )
                status = max_status(status, Status.CRITICAL)
            else:
                checks.append(
                    {
                        "name": f"k8s:{cluster_name}:{probe_name}",
                        "status": Status.OK.value,
                        "message": "allowed",
                    }
                )

    return checks, status


def _classify_k8s_api_exception(exc: k8s_exceptions.ApiException) -> Status:
    if exc.status in (401, 403):
        return Status.SKIPPED_PERMISSION
    if exc.status == 404:
        return Status.SKIPPED_CONFIG
    if exc.status in (429, 500, 502, 503, 504):
        return Status.SKIPPED_NETWORK
    return Status.FAILED
