from __future__ import annotations

from typing import Any

from opsbrief.config import ClusterConfig, EnvConfig


def candidate_projects(config: EnvConfig) -> list[str]:
    unique: list[str] = []
    for project in config.projects.values():
        if project and project not in unique:
            unique.append(project)
    for cluster in config.clusters:
        if cluster.project and cluster.project not in unique:
            unique.append(cluster.project)
    return unique


def resolve_clusters(
    config: EnvConfig,
    container_service: Any | None,
    *,
    cluster_client: Any | None = None,
    timeout_seconds: int = 60,
) -> list[ClusterConfig]:
    auto_discover = config.discovery.auto_discover_clusters
    if config.clusters and not auto_discover:
        return config.clusters
    if config.clusters and auto_discover and not config.discovery.include_discovered_clusters:
        return config.clusters

    projects = candidate_projects(config)
    if not projects:
        return list(config.clusters)

    if cluster_client is not None:
        try:
            discovered = _discover_clusters_with_client(
                cluster_client=cluster_client,
                projects=projects,
                timeout_seconds=timeout_seconds,
            )
        except Exception:  # noqa: BLE001
            if container_service is None:
                raise
            discovered = _discover_clusters_with_service(
                container_service=container_service,
                projects=projects,
            )
    elif container_service is not None:
        discovered = _discover_clusters_with_service(
            container_service=container_service,
            projects=projects,
        )
    else:
        discovered = []
    include_discovered = config.discovery.include_discovered_clusters
    if config.clusters and auto_discover and include_discovered:
        return _merge_clusters(config.clusters, discovered)
    return discovered


def _discover_clusters_with_client(
    cluster_client: Any,
    projects: list[str],
    timeout_seconds: int,
) -> list[ClusterConfig]:
    rows: list[ClusterConfig] = []
    seen: set[tuple[str, str, str]] = set()
    for project in projects:
        response = cluster_client.list_clusters(
            parent=f"projects/{project}/locations/-",
            timeout=max(1, timeout_seconds),
        )
        for item in getattr(response, "clusters", []):
            _append_cluster(rows=rows, seen=seen, project=project, item=item)
    return rows


def _discover_clusters_with_service(
    container_service: Any, projects: list[str]
) -> list[ClusterConfig]:
    rows: list[ClusterConfig] = []
    seen: set[tuple[str, str, str]] = set()
    clusters_api = container_service.projects().locations().clusters()
    for project in projects:
        next_page_token = ""
        while True:
            kwargs = {"parent": f"projects/{project}/locations/-"}
            if next_page_token:
                kwargs["pageToken"] = next_page_token
            response = clusters_api.list(**kwargs).execute()
            for item in response.get("clusters", []):
                _append_cluster(rows=rows, seen=seen, project=project, item=item)
            next_page_token = str(response.get("nextPageToken", "")).strip()
            if not next_page_token:
                break
    return rows


def _append_cluster(
    *,
    rows: list[ClusterConfig],
    seen: set[tuple[str, str, str]],
    project: str,
    item: Any,
) -> None:
    name = _cluster_field(item, "name")
    location = _cluster_field(item, "location") or _cluster_field(item, "zone")
    if not name or not location:
        return
    key = (project, location, name)
    if key in seen:
        return
    seen.add(key)
    rows.append(ClusterConfig(name=name, project=project, region=location))


def _cluster_field(item: Any, field: str) -> str:
    if isinstance(item, dict):
        return str(item.get(field, "")).strip()
    return str(getattr(item, field, "") or "").strip()


def _merge_clusters(
    configured: list[ClusterConfig], discovered: list[ClusterConfig]
) -> list[ClusterConfig]:
    rows: list[ClusterConfig] = []
    seen: set[tuple[str, str, str]] = set()
    for item in configured + discovered:
        key = (item.project, item.region, item.name)
        if key in seen:
            continue
        seen.add(key)
        rows.append(item)
    return rows
