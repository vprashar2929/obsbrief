from __future__ import annotations

from typing import Any

from googleapiclient.errors import HttpError

from opsbrief.cluster_discovery import resolve_clusters
from opsbrief.config import EnvConfig
from opsbrief.gcp_api import build_cluster_manager_client, build_service, get_gke_cluster
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
            collector="gke_inventory",
            status=Status.FAILED,
            summary="Unable to initialize GKE API client",
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
            collector="gke_inventory",
            status=Status.SKIPPED_CONFIG,
            summary="No clusters configured or discovered for gke_inventory",
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
            cluster_results.append(
                {
                    "name": cluster.name,
                    "project": cluster.project,
                    "region": cluster.region,
                    "status": failure_status.value,
                    "error": str(exc),
                }
            )
            errors.append(f"{cluster.name}: {exc}")
            status = max_status(status, failure_status)
            continue
        except Exception as exc:  # noqa: BLE001
            cluster_results.append(
                {
                    "name": cluster.name,
                    "project": cluster.project,
                    "region": cluster.region,
                    "status": Status.FAILED.value,
                    "error": str(exc),
                }
            )
            errors.append(f"{cluster.name}: {exc}")
            status = max_status(status, Status.FAILED)
            continue

        node_pools = payload.get("nodePools", [])
        cluster_results.append(
            {
                "name": cluster.name,
                "project": cluster.project,
                "region": cluster.region,
                "endpoint": payload.get("endpoint", ""),
                "status": payload.get("status", "UNKNOWN"),
                "master_version": payload.get("currentMasterVersion", "unknown"),
                "release_channel": (payload.get("releaseChannel") or {}).get(
                    "channel", "UNSPECIFIED"
                ),
                "private_nodes": (payload.get("privateClusterConfig") or {}).get(
                    "enablePrivateNodes", None
                ),
                "node_pool_count": len(node_pools),
                "node_pools": [
                    {
                        "name": node_pool.get("name", "unknown"),
                        "version": node_pool.get("version", "unknown"),
                        "initial_node_count": node_pool.get("initialNodeCount", 0),
                        "autoscaling_enabled": bool(
                            (node_pool.get("autoscaling") or {}).get("enabled", False)
                        ),
                        "autoscaling_min": (node_pool.get("autoscaling") or {}).get(
                            "minNodeCount", 0
                        ),
                        "autoscaling_max": (node_pool.get("autoscaling") or {}).get(
                            "maxNodeCount", 0
                        ),
                    }
                    for node_pool in node_pools
                ],
            }
        )

    summary = f"Collected GKE inventory for {len(cluster_results)} clusters"
    return CheckResult(
        collector="gke_inventory",
        status=status,
        summary=summary,
        details={
            "clusters": cluster_results,
            "autoscaling_policy": {
                "node_pool_autoscaling": {
                    "status": (config.report_expectations.autoscaling.node_pool_autoscaling.status),
                    "reason": (config.report_expectations.autoscaling.node_pool_autoscaling.reason),
                }
            },
        },
        errors=errors,
        started_at=started_at,
        finished_at=now_utc_iso(),
    )
