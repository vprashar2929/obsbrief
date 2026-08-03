from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import Any, cast

from googleapiclient.errors import HttpError

from opsbrief.cluster_discovery import candidate_projects
from opsbrief.config import EnvConfig
from opsbrief.gcp_api import (
    build_cloud_redis_client,
    build_cloud_sql_admin_service,
    build_compute_backend_services_client,
    build_compute_instances_client,
    build_compute_region_backend_services_client,
    build_managed_kafka_client,
    build_service,
    lazy_service,
    list_cloud_sql_instances,
    list_managed_kafka_clusters,
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
    details: dict[str, Any] = {
        "cloud_sql": [],
        "redis": [],
        "managed_kafka": [],
        "compute_instances": [],
        "load_balancers": [],
    }

    projects = candidate_projects(config)
    if not projects:
        return CheckResult(
            collector="services",
            status=Status.SKIPPED_CONFIG,
            summary="No projects configured for services collector",
            details=details,
            errors=[],
            started_at=started_at,
            finished_at=now_utc_iso(),
        )

    try:
        auth = get_auth_bundle(
            auth_mode=auth_mode, impersonate_service_account=impersonate_service_account
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            collector="services",
            status=Status.FAILED,
            summary="Unable to initialize service collector authentication",
            details=details,
            errors=[str(exc)],
            started_at=started_at,
            finished_at=now_utc_iso(),
        )

    if config.services.cloud_sql:
        try:
            sqladmin = build_cloud_sql_admin_service(auth, timeout_seconds)
        except Exception as exc:  # noqa: BLE001
            return CheckResult(
                collector="services",
                status=Status.FAILED,
                summary="Unable to initialize Cloud SQL API client",
                details=details,
                errors=[str(exc)],
                started_at=started_at,
                finished_at=now_utc_iso(),
            )

        for project in projects:
            try:
                sql_instances = _list_sql_instances(sqladmin, project)
                details["cloud_sql"].append(
                    {
                        "project": project,
                        "instance_count": len(sql_instances),
                        "instances": [
                            {
                                "name": item.get("name", ""),
                                "state": item.get("state", ""),
                                "database_version": item.get("databaseVersion", ""),
                                "tier": ((item.get("settings") or {}).get("tier", "")),
                                "availability_type": (
                                    (item.get("settings") or {}).get("availabilityType", "")
                                ),
                            }
                            for item in sql_instances
                        ],
                        "status": Status.OK.value,
                    }
                )
            except HttpError as exc:
                failure_status = classify_http_error(exc)
                details["cloud_sql"].append(
                    {"project": project, "status": failure_status.value, "error": str(exc)}
                )
                errors.append(f"{project}/cloud_sql: {exc}")
                status = max_status(status, failure_status)
            except Exception as exc:  # noqa: BLE001
                details["cloud_sql"].append(
                    {"project": project, "status": Status.FAILED.value, "error": str(exc)}
                )
                errors.append(f"{project}/cloud_sql: {exc}")
                status = max_status(status, Status.FAILED)
    else:
        details["cloud_sql"] = [
            {"status": Status.SKIPPED_CONFIG.value, "reason": "cloud_sql disabled in config"}
        ]

    redis_enabled = config.services.redis
    if redis_enabled:
        try:
            redis_service = build_service(auth, "redis", "v1", timeout_seconds)
            redis_client = None
            try:
                redis_client = build_cloud_redis_client(auth)
            except Exception:  # noqa: BLE001
                redis_client = None
            details["redis"] = _collect_redis(
                redis_service,
                projects,
                redis_client=redis_client,
                timeout_seconds=timeout_seconds,
            )
        except HttpError as exc:
            failure_status = classify_http_error(exc)
            details["redis"] = [{"status": failure_status.value, "error": str(exc)}]
            errors.append(f"redis: {exc}")
            status = max_status(status, failure_status)
        except Exception as exc:  # noqa: BLE001
            details["redis"] = [{"status": Status.FAILED.value, "error": str(exc)}]
            errors.append(f"redis: {exc}")
            status = max_status(status, Status.FAILED)
    else:
        details["redis"] = [
            {"status": Status.SKIPPED_CONFIG.value, "reason": "redis disabled in config"}
        ]

    kafka_enabled = config.services.managed_kafka
    if kafka_enabled:
        try:
            kafka_service = build_service(auth, "managedkafka", "v1", timeout_seconds)
            kafka_client = None
            try:
                kafka_client = build_managed_kafka_client(auth)
            except Exception:  # noqa: BLE001
                kafka_client = None
            details["managed_kafka"] = _collect_managed_kafka(
                kafka_service,
                projects,
                kafka_client=kafka_client,
                timeout_seconds=timeout_seconds,
            )
        except HttpError as exc:
            failure_status = classify_http_error(exc)
            details["managed_kafka"] = [{"status": failure_status.value, "error": str(exc)}]
            errors.append(f"managed_kafka: {exc}")
            status = max_status(status, failure_status)
        except Exception as exc:  # noqa: BLE001
            details["managed_kafka"] = [{"status": Status.FAILED.value, "error": str(exc)}]
            errors.append(f"managed_kafka: {exc}")
            status = max_status(status, Status.FAILED)
    else:
        details["managed_kafka"] = [
            {"status": Status.SKIPPED_CONFIG.value, "reason": "managed_kafka disabled in config"}
        ]

    compute_targets = _as_dict_list(config.services.compute_instances)
    auto_compute_discovery = config.discovery.auto_discover_compute_instances
    compute_instances_client = None
    compute_service: Callable[[], Any] | None = None
    if compute_targets or auto_compute_discovery:
        try:
            compute_instances_client = build_compute_instances_client(auth)
        except Exception:  # noqa: BLE001
            compute_instances_client = None
        compute_service = lazy_service(auth, "compute", "v1", timeout_seconds)
    if compute_targets:
        try:
            details["compute_instances"] = _collect_compute_instances(
                compute_service,
                compute_targets,
                instances_client=compute_instances_client,
                timeout_seconds=timeout_seconds,
            )
        except HttpError as exc:
            failure_status = classify_http_error(exc)
            details["compute_instances"] = [{"status": failure_status.value, "error": str(exc)}]
            errors.append(f"compute_instances: {exc}")
            status = max_status(status, failure_status)
        except Exception as exc:  # noqa: BLE001
            details["compute_instances"] = [{"status": Status.FAILED.value, "error": str(exc)}]
            errors.append(f"compute_instances: {exc}")
            status = max_status(status, Status.FAILED)
    elif auto_compute_discovery:
        try:
            details["compute_instances"] = _discover_compute_instances(
                compute_service,
                projects,
                instances_client=compute_instances_client,
                timeout_seconds=timeout_seconds,
            )
        except HttpError as exc:
            failure_status = classify_http_error(exc)
            details["compute_instances"] = [{"status": failure_status.value, "error": str(exc)}]
            errors.append(f"compute_instances: {exc}")
            status = max_status(status, failure_status)
        except Exception as exc:  # noqa: BLE001
            details["compute_instances"] = [{"status": Status.FAILED.value, "error": str(exc)}]
            errors.append(f"compute_instances: {exc}")
            status = max_status(status, Status.FAILED)
    else:
        details["compute_instances"] = [
            {"status": Status.SKIPPED_CONFIG.value, "reason": "compute_instances not configured"}
        ]

    lb_targets = _as_dict_list(config.services.load_balancers)
    auto_lb_discovery = config.discovery.auto_discover_load_balancers
    backend_services_client = None
    region_backend_services_client = None
    lb_compute_service: Callable[[], Any] | None = None
    if lb_targets or auto_lb_discovery:
        try:
            backend_services_client = build_compute_backend_services_client(auth)
        except Exception:  # noqa: BLE001
            backend_services_client = None
        try:
            region_backend_services_client = build_compute_region_backend_services_client(auth)
        except Exception:  # noqa: BLE001
            region_backend_services_client = None
        lb_compute_service = lazy_service(auth, "compute", "v1", timeout_seconds)
    if lb_targets:
        try:
            details["load_balancers"] = _collect_load_balancers(
                lb_compute_service,
                lb_targets,
                backend_services_client=backend_services_client,
                region_backend_services_client=region_backend_services_client,
                timeout_seconds=timeout_seconds,
            )
        except HttpError as exc:
            failure_status = classify_http_error(exc)
            details["load_balancers"] = [{"status": failure_status.value, "error": str(exc)}]
            errors.append(f"load_balancers: {exc}")
            status = max_status(status, failure_status)
        except Exception as exc:  # noqa: BLE001
            details["load_balancers"] = [{"status": Status.FAILED.value, "error": str(exc)}]
            errors.append(f"load_balancers: {exc}")
            status = max_status(status, Status.FAILED)
    elif auto_lb_discovery:
        try:
            details["load_balancers"] = _discover_load_balancers(
                lb_compute_service,
                projects,
                backend_services_client=backend_services_client,
                region_backend_services_client=region_backend_services_client,
                timeout_seconds=timeout_seconds,
            )
        except HttpError as exc:
            failure_status = classify_http_error(exc)
            details["load_balancers"] = [{"status": failure_status.value, "error": str(exc)}]
            errors.append(f"load_balancers: {exc}")
            status = max_status(status, failure_status)
        except Exception as exc:  # noqa: BLE001
            details["load_balancers"] = [{"status": Status.FAILED.value, "error": str(exc)}]
            errors.append(f"load_balancers: {exc}")
            status = max_status(status, Status.FAILED)
    else:
        details["load_balancers"] = [
            {"status": Status.SKIPPED_CONFIG.value, "reason": "load_balancers not configured"}
        ]

    status = max_status(status, _status_from_resource_rows(details["load_balancers"]))

    cloud_sql_total = sum(item.get("instance_count", 0) for item in details["cloud_sql"])
    summary = f"Cloud SQL instances discovered={cloud_sql_total}"
    return CheckResult(
        collector="services",
        status=status,
        summary=summary,
        details=details,
        errors=errors,
        started_at=started_at,
        finished_at=now_utc_iso(),
    )


def _list_sql_instances(service: Any, project: str) -> list[dict[str, Any]]:
    return list_cloud_sql_instances(service, project)


def _collect_redis(
    service: Any,
    projects: list[str],
    *,
    redis_client: Any | None = None,
    timeout_seconds: int = 60,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for project in projects:
        instances = _list_redis_instances(
            service,
            project,
            redis_client=redis_client,
            timeout_seconds=timeout_seconds,
        )
        results.append(
            {
                "project": project,
                "status": Status.OK.value,
                "instance_count": len(instances),
                "instances": [
                    {
                        "name": item.get("name", ""),
                        "state": item.get("state", ""),
                        "memory_size_gb": item.get("memorySizeGb", None),
                        "redis_version": item.get("redisVersion", ""),
                    }
                    for item in instances
                ],
            }
        )
    return results


def _list_redis_instances(
    service: Any,
    project: str,
    *,
    redis_client: Any | None = None,
    timeout_seconds: int = 60,
) -> list[dict[str, Any]]:
    parent = f"projects/{project}/locations/-"
    if redis_client is not None:
        try:
            return [
                protobuf_to_dict(instance)
                for instance in redis_client.list_instances(
                    parent=parent,
                    timeout=max(1, timeout_seconds),
                )
            ]
        except Exception:  # noqa: BLE001
            pass

    request = service.projects().locations().instances().list(parent=parent, pageSize=100)
    instances: list[dict[str, Any]] = []
    while request is not None:
        response = request.execute()
        instances.extend(response.get("instances", []))
        request = service.projects().locations().instances().list_next(request, response)
    return instances


def _collect_managed_kafka(
    service: Any,
    projects: list[str],
    *,
    kafka_client: Any | None = None,
    timeout_seconds: int = 60,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for project in projects:
        clusters = _list_managed_kafka_clusters(
            service,
            project,
            kafka_client=kafka_client,
            timeout_seconds=timeout_seconds,
        )
        results.append(
            {
                "project": project,
                "status": Status.OK.value,
                "cluster_count": len(clusters),
                "clusters": [
                    {
                        "name": item.get("name", ""),
                        "state": item.get("state", ""),
                        "capacity": item.get("capacityConfig", {}),
                    }
                    for item in clusters
                ],
            }
        )
    return results


def _list_managed_kafka_clusters(
    service: Any,
    project: str,
    *,
    kafka_client: Any | None = None,
    timeout_seconds: int = 60,
) -> list[dict[str, Any]]:
    return list_managed_kafka_clusters(
        service,
        project,
        kafka_client=kafka_client,
        timeout_seconds=timeout_seconds,
    )


def _collect_compute_instances(
    service: Any | Callable[[], Any] | None,
    targets: list[dict[str, Any]],
    *,
    instances_client: Any | None = None,
    timeout_seconds: int = 60,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for target in targets:
        project = str(target.get("project", "")).strip()
        zone = str(target.get("zone", "")).strip()
        name = str(target.get("name", "")).strip()
        if not project or not zone or not name:
            rows.append(
                {
                    "status": Status.SKIPPED_CONFIG.value,
                    "reason": "compute target requires project, zone, and name",
                    "target": target,
                }
            )
            continue

        response = _get_compute_instance(
            service,
            project,
            zone,
            name,
            instances_client=instances_client,
            timeout_seconds=timeout_seconds,
        )
        rows.append(_compute_instance_row(project, zone, response))
    return rows


def _collect_load_balancers(
    service: Any | Callable[[], Any] | None,
    targets: list[dict[str, Any]],
    *,
    backend_services_client: Any | None = None,
    region_backend_services_client: Any | None = None,
    timeout_seconds: int = 60,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for target in targets:
        project = str(target.get("project", "")).strip()
        backend_service = str(target.get("backend_service", "")).strip()
        scope = str(target.get("scope", "global")).strip() or "global"
        region = str(target.get("region", "")).strip()

        if not project or not backend_service:
            rows.append(
                {
                    "status": Status.SKIPPED_CONFIG.value,
                    "reason": "load balancer target requires project and backend_service",
                    "target": target,
                }
            )
            continue

        if scope == "region" and not region:
            rows.append(
                {
                    "status": Status.SKIPPED_CONFIG.value,
                    "reason": "region scope requires region value",
                    "target": target,
                }
            )
            continue

        if scope == "region":
            payload = _get_load_balancer_backend_service(
                service,
                project,
                backend_service,
                scope="region",
                region=region,
                backend_services_client=backend_services_client,
                region_backend_services_client=region_backend_services_client,
                timeout_seconds=timeout_seconds,
            )
            scope_type = "region"
        else:
            payload = _get_load_balancer_backend_service(
                service,
                project,
                backend_service,
                scope="global",
                region="",
                backend_services_client=backend_services_client,
                region_backend_services_client=region_backend_services_client,
                timeout_seconds=timeout_seconds,
            )
            scope_type = "global"

        rows.append(
            _load_balancer_row(
                service=service,
                project=project,
                row_scope=scope,
                health_scope=scope_type,
                region=region,
                backend_service=backend_service,
                backend=payload,
                backend_services_client=backend_services_client,
                region_backend_services_client=region_backend_services_client,
                timeout_seconds=timeout_seconds,
            )
        )
    return rows


def _get_load_balancer_backend_service(
    service: Any | Callable[[], Any] | None,
    project: str,
    backend_service: str,
    *,
    scope: str,
    region: str,
    backend_services_client: Any | None = None,
    region_backend_services_client: Any | None = None,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    if scope == "region":
        if region_backend_services_client is not None:
            try:
                payload = region_backend_services_client.get(
                    project=project,
                    region=region,
                    backend_service=backend_service,
                    timeout=max(1, timeout_seconds),
                )
                return protobuf_to_dict(payload)
            except Exception:  # noqa: BLE001
                pass
        return _get_load_balancer_backend_service_with_service(
            service,
            project,
            backend_service,
            scope=scope,
            region=region,
        )

    if backend_services_client is not None:
        try:
            payload = backend_services_client.get(
                project=project,
                backend_service=backend_service,
                timeout=max(1, timeout_seconds),
            )
            return protobuf_to_dict(payload)
        except Exception:  # noqa: BLE001
            pass
    return _get_load_balancer_backend_service_with_service(
        service,
        project,
        backend_service,
        scope=scope,
        region=region,
    )


def _get_load_balancer_backend_service_with_service(
    service: Any | Callable[[], Any] | None,
    project: str,
    backend_service: str,
    *,
    scope: str,
    region: str,
) -> dict[str, Any]:
    compute = resolve_fallback_service(service, None, "Compute discovery service unavailable")
    if scope == "region":
        payload = (
            compute.regionBackendServices()
            .get(project=project, region=region, backendService=backend_service)
            .execute()
        )
    else:
        payload = (
            compute.backendServices().get(project=project, backendService=backend_service).execute()
        )
    return cast(dict[str, Any], payload)


def _get_compute_instance(
    service: Any | Callable[[], Any] | None,
    project: str,
    zone: str,
    name: str,
    *,
    instances_client: Any | None = None,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    if instances_client is not None:
        try:
            instance = instances_client.get(
                project=project,
                zone=zone,
                instance=name,
                timeout=max(1, timeout_seconds),
            )
            return protobuf_to_dict(instance)
        except Exception:  # noqa: BLE001
            pass
    return _get_compute_instance_with_service(service, project, zone, name)


def _get_compute_instance_with_service(
    service: Any | Callable[[], Any] | None,
    project: str,
    zone: str,
    name: str,
) -> dict[str, Any]:
    compute = resolve_fallback_service(service, None, "Compute discovery service unavailable")
    response = compute.instances().get(project=project, zone=zone, instance=name).execute()
    return cast(dict[str, Any], response)


def _discover_compute_instances(
    service: Any | Callable[[], Any] | None,
    projects: list[str],
    *,
    instances_client: Any | None = None,
    timeout_seconds: int = 60,
) -> list[dict[str, Any]]:
    if instances_client is not None:
        try:
            rows = _discover_compute_instances_with_client(
                instances_client,
                projects,
                timeout_seconds,
            )
            if rows:
                return rows
            return [
                {"status": Status.SKIPPED_CONFIG.value, "reason": "no compute instances discovered"}
            ]
        except Exception:  # noqa: BLE001
            pass
    return _discover_compute_instances_with_service(service, projects)


def _discover_compute_instances_with_client(
    client: Any,
    projects: list[str],
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for project in projects:
        request = {
            "project": project,
            "max_results": 500,
        }
        for zone, payload in client.aggregated_list(
            request=request,
            timeout=max(1, timeout_seconds),
        ):
            zone_name = str(zone).split("/")[-1]
            for instance in _as_dict_list(protobuf_to_dict(payload).get("instances")):
                rows.append(_compute_instance_row(project, zone_name, instance))
    return rows


def _discover_compute_instances_with_service(
    service: Any | Callable[[], Any] | None,
    projects: list[str],
) -> list[dict[str, Any]]:
    compute = resolve_fallback_service(service, None, "Compute discovery service unavailable")
    rows: list[dict[str, Any]] = []
    for project in projects:
        request = compute.instances().aggregatedList(project=project, maxResults=500)
        while request is not None:
            response = request.execute()
            for zone, payload in cast(dict[str, dict[str, Any]], response.get("items", {})).items():
                instances = cast(list[dict[str, Any]], payload.get("instances", []))
                zone_name = zone.split("/")[-1]
                for instance in instances:
                    rows.append(_compute_instance_row(project, zone_name, instance))
            request = compute.instances().aggregatedList_next(request, response)
    if rows:
        return rows
    return [{"status": Status.SKIPPED_CONFIG.value, "reason": "no compute instances discovered"}]


def _compute_instance_row(
    project: str,
    zone: str,
    instance: dict[str, Any],
) -> dict[str, Any]:
    network_interfaces = instance.get("networkInterfaces", [])
    return {
        "status": Status.OK.value,
        "project": project,
        "zone": zone,
        "name": instance.get("name", ""),
        "instance_status": instance.get("status", ""),
        "machine_type": str(instance.get("machineType", "")).split("/")[-1],
        "network_interfaces": len(network_interfaces)
        if isinstance(network_interfaces, list)
        else 0,
    }


def _discover_load_balancers(
    service: Any | Callable[[], Any] | None,
    projects: list[str],
    *,
    backend_services_client: Any | None = None,
    region_backend_services_client: Any | None = None,
    timeout_seconds: int = 60,
) -> list[dict[str, Any]]:
    if backend_services_client is not None:
        try:
            rows = _discover_load_balancers_with_client(
                service,
                backend_services_client,
                projects,
                region_backend_services_client=region_backend_services_client,
                timeout_seconds=timeout_seconds,
            )
            if rows:
                return rows
            return [
                {"status": Status.SKIPPED_CONFIG.value, "reason": "no load balancers discovered"}
            ]
        except Exception:  # noqa: BLE001
            pass
    return _discover_load_balancers_with_service(service, projects)


def _discover_load_balancers_with_client(
    service: Any | Callable[[], Any] | None,
    client: Any,
    projects: list[str],
    *,
    region_backend_services_client: Any | None = None,
    timeout_seconds: int = 60,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for project in projects:
        request = {
            "project": project,
            "max_results": 500,
        }
        for scope, payload in client.aggregated_list(
            request=request,
            timeout=max(1, timeout_seconds),
        ):
            scoped_payload = protobuf_to_dict(payload)
            scope_text = str(scope)
            scope_name = scope_text.split("/")[-1]
            for backend in _as_dict_list(scoped_payload.get("backendServices")):
                scope_type, region = _discovered_lb_scope(scope_text)
                backend_name = str(backend.get("name", ""))
                rows.append(
                    _load_balancer_row(
                        service=service,
                        project=project,
                        row_scope=scope_name,
                        health_scope=scope_type,
                        region=region,
                        backend_service=backend_name,
                        backend=backend,
                        backend_services_client=client,
                        region_backend_services_client=region_backend_services_client,
                        timeout_seconds=timeout_seconds,
                    )
                )
    return rows


def _discover_load_balancers_with_service(
    service: Any | Callable[[], Any] | None, projects: list[str]
) -> list[dict[str, Any]]:
    compute = resolve_fallback_service(service, None, "Compute discovery service unavailable")
    rows: list[dict[str, Any]] = []
    for project in projects:
        request = compute.backendServices().aggregatedList(project=project, maxResults=500)
        while request is not None:
            response = request.execute()
            items = cast(dict[str, dict[str, Any]], response.get("items", {}))
            for scope, payload in items.items():
                backends = cast(list[dict[str, Any]], payload.get("backendServices", []))
                scope_name = scope.split("/")[-1]
                for backend in backends:
                    scope_type, region = _discovered_lb_scope(scope)
                    backend_name = str(backend.get("name", ""))
                    rows.append(
                        _load_balancer_row(
                            service=service,
                            project=project,
                            row_scope=scope_name,
                            health_scope=scope_type,
                            region=region,
                            backend_service=backend_name,
                            backend=backend,
                        )
                    )
            request = compute.backendServices().aggregatedList_next(request, response)
    if rows:
        return rows
    return [{"status": Status.SKIPPED_CONFIG.value, "reason": "no load balancers discovered"}]


def _load_balancer_row(
    *,
    service: Any | Callable[[], Any] | None,
    project: str,
    row_scope: str,
    health_scope: str,
    region: str,
    backend_service: str,
    backend: dict[str, Any],
    backend_services_client: Any | None = None,
    region_backend_services_client: Any | None = None,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    backends = backend.get("backends", [])
    health_checks = backend.get("healthChecks", [])
    backends_list = backends if isinstance(backends, list) else []
    health_checks_list = health_checks if isinstance(health_checks, list) else []
    health = _load_balancer_backend_health(
        service=service,
        project=project,
        backend_service=backend_service,
        scope=health_scope,
        region=region,
        backends=cast(list[dict[str, Any]], backends_list),
        backend_services_client=backend_services_client,
        region_backend_services_client=region_backend_services_client,
        timeout_seconds=timeout_seconds,
    )
    return {
        "status": health["status"],
        "project": project,
        "scope": row_scope,
        "region": region,
        "backend_service": backend_service,
        "protocol": backend.get("protocol", ""),
        "scheme": backend.get("loadBalancingScheme", ""),
        "health_checks": len(health_checks_list),
        "backends": len(backends_list),
        "backend_health": health["summary"],
        "backend_health_counts": health["counts"],
        "reason": health.get("reason", ""),
    }


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _status_from_resource_rows(rows: Any) -> Status:
    if not isinstance(rows, list):
        return Status.OK

    status = Status.OK
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = str(row.get("status", "ok"))
        if raw == Status.WARNING.value:
            status = max_status(status, Status.WARNING)
        elif raw == Status.CRITICAL.value:
            status = max_status(status, Status.CRITICAL)
        elif raw == Status.FAILED.value:
            status = max_status(status, Status.FAILED)
        elif raw == Status.SKIPPED_PERMISSION.value:
            status = max_status(status, Status.SKIPPED_PERMISSION)
        elif raw == Status.SKIPPED_NETWORK.value:
            status = max_status(status, Status.SKIPPED_NETWORK)
    return status


def _discovered_lb_scope(scope_key: str) -> tuple[str, str]:
    key = scope_key.strip()
    if key == "global":
        return "global", ""
    if key.startswith("regions/"):
        return "region", key.split("/", 1)[1]
    return "global", ""


def _load_balancer_backend_health(
    service: Any | Callable[[], Any] | None,
    project: str,
    backend_service: str,
    scope: str,
    region: str,
    backends: list[dict[str, Any]],
    backend_services_client: Any | None = None,
    region_backend_services_client: Any | None = None,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    groups = [
        str(item.get("group", "")).strip()
        for item in backends
        if isinstance(item, dict) and str(item.get("group", "")).strip()
    ]
    if not groups:
        return {
            "status": Status.SKIPPED_CONFIG.value,
            "summary": "no backend groups",
            "counts": {},
            "reason": "backend service has no backend groups",
        }

    states: Counter[str] = Counter()
    for group in groups[:10]:
        try:
            response = _get_load_balancer_backend_health(
                service=service,
                project=project,
                backend_service=backend_service,
                scope=scope,
                region=region,
                group=group,
                backend_services_client=backend_services_client,
                region_backend_services_client=region_backend_services_client,
                timeout_seconds=timeout_seconds,
            )
        except HttpError as exc:
            failure_status = classify_http_error(exc)
            return {
                "status": failure_status.value,
                "summary": "backend health unavailable",
                "counts": {},
                "reason": str(exc),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "status": Status.FAILED.value,
                "summary": "backend health unavailable",
                "counts": {},
                "reason": str(exc),
            }

        for item in cast(list[dict[str, Any]], response.get("healthStatus", [])):
            state = str(item.get("healthState", "UNKNOWN")).strip() or "UNKNOWN"
            states[state] += 1

    healthy = states.get("HEALTHY", 0)
    unknown = states.get("UNKNOWN", 0)
    unhealthy = sum(count for key, count in states.items() if key not in ("HEALTHY", "UNKNOWN"))
    status = Status.WARNING if unhealthy > 0 else Status.OK
    summary = f"healthy={healthy}, unhealthy={unhealthy}, unknown={unknown}"
    return {
        "status": status.value,
        "summary": summary,
        "counts": dict(states),
        "reason": "",
    }


def _get_load_balancer_backend_health(
    *,
    service: Any | Callable[[], Any] | None,
    project: str,
    backend_service: str,
    scope: str,
    region: str,
    group: str,
    backend_services_client: Any | None = None,
    region_backend_services_client: Any | None = None,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    if scope == "region" and region:
        if region_backend_services_client is not None:
            try:
                response = region_backend_services_client.get_health(
                    project=project,
                    region=region,
                    backend_service=backend_service,
                    resource_group_reference_resource={"group": group},
                    timeout=max(1, timeout_seconds),
                )
                return protobuf_to_dict(response)
            except Exception:  # noqa: BLE001
                pass
    elif backend_services_client is not None:
        try:
            response = backend_services_client.get_health(
                project=project,
                backend_service=backend_service,
                resource_group_reference_resource={"group": group},
                timeout=max(1, timeout_seconds),
            )
            return protobuf_to_dict(response)
        except Exception:  # noqa: BLE001
            pass

    compute = resolve_fallback_service(service, None, "Compute discovery service unavailable")
    body = {"group": group}
    if scope == "region" and region:
        response = (
            compute.regionBackendServices()
            .getHealth(
                project=project,
                region=region,
                backendService=backend_service,
                body=body,
            )
            .execute()
        )
    else:
        response = (
            compute.backendServices()
            .getHealth(
                project=project,
                backendService=backend_service,
                body=body,
            )
            .execute()
        )
    return cast(dict[str, Any], response)
