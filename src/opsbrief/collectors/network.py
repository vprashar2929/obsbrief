from __future__ import annotations

from collections.abc import Callable
from typing import Any

from googleapiclient.errors import HttpError
from kubernetes import client as k8s_client
from kubernetes.client import exceptions as k8s_exceptions
from urllib3.exceptions import MaxRetryError, NewConnectionError

from opsbrief.cluster_discovery import candidate_projects, resolve_clusters
from opsbrief.config import EnvConfig
from opsbrief.gcp_api import (
    build_cluster_manager_client,
    build_compute_firewalls_client,
    build_compute_forwarding_rules_client,
    build_compute_networks_client,
    build_dns_client,
    build_service,
    get_gke_cluster,
    lazy_service,
    protobuf_to_dict,
    resolve_fallback_service,
)
from opsbrief.gcp_auth import AuthMode, get_auth_bundle
from opsbrief.k8s_api import access_token, api_client, cluster_ca_file, preferred_endpoints
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
    project_rows: list[dict[str, Any]] = []

    projects = candidate_projects(config)
    if not projects:
        return CheckResult(
            collector="network",
            status=Status.SKIPPED_CONFIG,
            summary="No projects configured for network collector",
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
        compute = lazy_service(auth, "compute", "v1", timeout_seconds)
        networks_client = None
        try:
            networks_client = build_compute_networks_client(auth)
        except Exception:  # noqa: BLE001
            networks_client = None
        firewalls_client = None
        try:
            firewalls_client = build_compute_firewalls_client(auth)
        except Exception:  # noqa: BLE001
            firewalls_client = None
        forwarding_rules_client = None
        try:
            forwarding_rules_client = build_compute_forwarding_rules_client(auth)
        except Exception:  # noqa: BLE001
            forwarding_rules_client = None
        dns = lazy_service(auth, "dns", "v1", timeout_seconds)
        try:
            container = build_service(auth, "container", "v1", timeout_seconds)
            cluster_client = None
            try:
                cluster_client = build_cluster_manager_client(auth)
            except Exception:  # noqa: BLE001
                cluster_client = None
        except Exception:  # noqa: BLE001
            container = None
            cluster_client = None
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            collector="network",
            status=Status.FAILED,
            summary="Unable to initialize network collector dependencies",
            details={},
            errors=[str(exc)],
            started_at=started_at,
            finished_at=now_utc_iso(),
        )

    required_internal_zones = _normalize_zone_list(config.network.required_internal_zones)
    for project in projects:
        try:
            networks = _list_networks(
                compute,
                project,
                networks_client=networks_client,
                timeout_seconds=timeout_seconds,
            )
            firewalls = _list_firewalls(
                compute,
                project,
                firewalls_client=firewalls_client,
                timeout_seconds=timeout_seconds,
            )
            forwarding_rules = _list_forwarding_rules(
                compute,
                project,
                forwarding_rules_client=forwarding_rules_client,
                timeout_seconds=timeout_seconds,
            )
            dns_client = None
            try:
                dns_client = build_dns_client(auth, project)
            except Exception:  # noqa: BLE001
                dns_client = None
            zones = _list_dns_zones(dns, project, dns_client=dns_client)
            zone_record_counts = {
                str(zone.get("name", "")): _count_dns_record_sets(
                    dns,
                    project,
                    str(zone.get("name", "")),
                    dns_client=dns_client,
                )
                for zone in zones
                if str(zone.get("name", ""))
            }
            response_policies = _list_response_policies(dns, project)
            response_policy_rules = _list_response_policy_rules(
                dns,
                project,
                response_policies,
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

        peering_rows: list[dict[str, Any]] = []
        for network in networks:
            network_name = str(network.get("name", ""))
            for peering in _as_dict_list(network.get("peerings")):
                peering_rows.append(
                    {
                        "network": network_name,
                        "name": str(peering.get("name", "")),
                        "state": str(peering.get("state", "")),
                        "peer_network": str(peering.get("network", "")),
                        "export_custom_routes": bool(peering.get("exportCustomRoutes", False)),
                        "import_custom_routes": bool(peering.get("importCustomRoutes", False)),
                    }
                )

        inactive_peerings = [
            row
            for row in peering_rows
            if row.get("state", "").upper() not in ("ACTIVE", "INACTIVE_WITH_CUSTOM_ROUTES")
        ]
        present_internal_zones = _normalize_zone_list(
            [str(zone.get("dnsName", "")) for zone in zones if str(zone.get("dnsName", ""))]
        )
        missing_required_internal_zones = sorted(
            zone for zone in required_internal_zones if zone not in present_internal_zones
        )
        dns_policy_findings: list[dict[str, Any]] = []
        if missing_required_internal_zones:
            dns_policy_findings.append(
                {
                    "severity": Status.WARNING.value,
                    "check": "required_internal_zones",
                    "message": (
                        "Required internal DNS zones are missing: "
                        + ", ".join(missing_required_internal_zones)
                    ),
                }
            )
        if inactive_peerings:
            status = max_status(status, Status.WARNING)
            row_status = Status.WARNING
        else:
            row_status = Status.OK
        if dns_policy_findings:
            status = max_status(status, Status.WARNING)
            row_status = Status.WARNING

        project_rows.append(
            {
                "project": project,
                "status": row_status.value,
                "network_count": len(networks),
                "networks": [_network_row(network) for network in networks[:50]],
                "firewall_rule_count": len(firewalls),
                "firewall_disabled_count": sum(
                    1 for fw in firewalls if bool(fw.get("disabled", False))
                ),
                "firewall_rules": [_firewall_row(firewall) for firewall in firewalls[:100]],
                "forwarding_rule_count": len(forwarding_rules),
                "peering_count": len(peering_rows),
                "peering_inactive_count": len(inactive_peerings),
                "peerings": peering_rows[:100],
                "dns_zone_count": len(zones),
                "dns_zones": [
                    {
                        "name": str(zone.get("name", "")),
                        "dns_name": str(zone.get("dnsName", "")),
                        "visibility": str(zone.get("visibility", "")),
                        "record_set_count": zone_record_counts.get(str(zone.get("name", ""))),
                        "network_count": len(
                            _as_dict_list(
                                _as_dict(zone.get("privateVisibilityConfig")).get("networks")
                            )
                        ),
                        "networks": [
                            str(network.get("networkUrl", ""))
                            for network in _as_dict_list(
                                _as_dict(zone.get("privateVisibilityConfig")).get("networks")
                            )
                        ],
                    }
                    for zone in zones[:50]
                ],
                "response_policy_count": len(response_policies),
                "response_policy_rule_count": len(response_policy_rules),
                "dns_policy_findings": dns_policy_findings,
                "required_internal_zones": required_internal_zones,
                "missing_required_internal_zones": missing_required_internal_zones,
                "response_policies": [
                    {
                        "name": str(policy.get("responsePolicyName", "")),
                        "description": str(policy.get("description", "")),
                        "network_count": len(_as_dict_list(policy.get("networks"))),
                    }
                    for policy in response_policies[:50]
                ],
                "response_policy_rules": response_policy_rules[:100],
            }
        )

    internal_dns = _collect_internal_dns_state(
        config=config,
        auth=auth,
        container=container,
        cluster_client=cluster_client,
        timeout_seconds=timeout_seconds,
    )
    internal_dns_status = _status_from_value(str(internal_dns.get("status", Status.OK.value)))
    if internal_dns_status != Status.SKIPPED_CONFIG:
        status = max_status(status, internal_dns_status)
    summary_parts = [f"Collected network posture for {len(project_rows)} project(s)"]
    checked_fqdns = _as_int(internal_dns.get("checked_fqdn_total", 0))
    failed_fqdns = _as_int(internal_dns.get("failed_fqdn_total", 0))
    if checked_fqdns > 0 or failed_fqdns > 0:
        summary_parts.append(f"in-cluster DNS checks={checked_fqdns}, failed={failed_fqdns}")
    summary = "; ".join(summary_parts)
    return CheckResult(
        collector="network",
        status=status,
        summary=summary,
        details={"projects": project_rows, "internal_dns": internal_dns},
        errors=errors,
        started_at=started_at,
        finished_at=now_utc_iso(),
    )


def _list_networks(
    compute: Any | Callable[[], Any] | None,
    project: str,
    *,
    networks_client: Any | None = None,
    timeout_seconds: int = 60,
) -> list[dict[str, Any]]:
    if networks_client is not None:
        try:
            return [
                protobuf_to_dict(network)
                for network in networks_client.list(
                    project=project,
                    timeout=max(1, timeout_seconds),
                )
            ]
        except Exception:  # noqa: BLE001
            pass
    return _list_networks_with_service(compute, project)


def _list_networks_with_service(compute: Any, project: str) -> list[dict[str, Any]]:
    service = resolve_fallback_service(compute, None, "Compute discovery service unavailable")
    rows: list[dict[str, Any]] = []
    page_token = ""
    while True:
        kwargs: dict[str, Any] = {"project": project}
        if page_token:
            kwargs["pageToken"] = page_token
        response = service.networks().list(**kwargs).execute()
        rows.extend(_as_dict_list(response.get("items")))
        page_token = str(response.get("nextPageToken", "")).strip()
        if not page_token:
            return rows


def _network_row(network: dict[str, Any]) -> dict[str, Any]:
    routing_config = _as_dict(network.get("routingConfig"))
    peerings = _as_dict_list(network.get("peerings"))
    return {
        "name": str(network.get("name", "")),
        "auto_create_subnetworks": bool(network.get("autoCreateSubnetworks", False)),
        "routing_mode": str(routing_config.get("routingMode", "")),
        "mtu": str(network.get("mtu", "")),
        "peering_count": len(peerings),
        "self_link": str(network.get("selfLink", "")),
    }


def _list_firewalls(
    compute: Any | Callable[[], Any] | None,
    project: str,
    *,
    firewalls_client: Any | None = None,
    timeout_seconds: int = 60,
) -> list[dict[str, Any]]:
    if firewalls_client is not None:
        try:
            return [
                protobuf_to_dict(firewall)
                for firewall in firewalls_client.list(
                    project=project,
                    timeout=max(1, timeout_seconds),
                )
            ]
        except Exception:  # noqa: BLE001
            pass
    return _list_firewalls_with_service(compute, project)


def _list_firewalls_with_service(compute: Any, project: str) -> list[dict[str, Any]]:
    service = resolve_fallback_service(compute, None, "Compute discovery service unavailable")
    rows: list[dict[str, Any]] = []
    page_token = ""
    while True:
        kwargs: dict[str, Any] = {"project": project}
        if page_token:
            kwargs["pageToken"] = page_token
        response = service.firewalls().list(**kwargs).execute()
        rows.extend(_as_dict_list(response.get("items")))
        page_token = str(response.get("nextPageToken", "")).strip()
        if not page_token:
            return rows


def _firewall_row(firewall: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(firewall.get("name", "")),
        "network": _last_url_segment(str(firewall.get("network", ""))),
        "direction": str(firewall.get("direction", "")),
        "priority": str(firewall.get("priority", "")),
        "disabled": bool(firewall.get("disabled", False)),
        "source_ranges": _str_list_join(firewall.get("sourceRanges")),
        "target_tags": _str_list_join(firewall.get("targetTags")),
        "target_service_accounts": _str_list_join(firewall.get("targetServiceAccounts")),
        "allowed": _firewall_ports(_as_dict_list(firewall.get("allowed"))),
        "denied": _firewall_ports(_as_dict_list(firewall.get("denied"))),
    }


def _firewall_ports(rules: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for rule in rules:
        protocol = str(rule.get("IPProtocol", ""))
        ports = [str(port) for port in rule.get("ports", []) if str(port)]
        parts.append(f"{protocol}:{','.join(ports)}" if ports else protocol)
    return "; ".join(part for part in parts if part)


def _str_list_join(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    return ", ".join(str(item) for item in value if str(item))


def _last_url_segment(value: str) -> str:
    return value.rstrip("/").rsplit("/", 1)[-1] if value else ""


def _list_forwarding_rules(
    compute: Any | Callable[[], Any] | None,
    project: str,
    *,
    forwarding_rules_client: Any | None = None,
    timeout_seconds: int = 60,
) -> list[dict[str, Any]]:
    if forwarding_rules_client is not None:
        try:
            return _list_forwarding_rules_with_client(
                forwarding_rules_client,
                project,
                timeout_seconds,
            )
        except Exception:  # noqa: BLE001
            pass
    return _list_forwarding_rules_with_service(compute, project)


def _list_forwarding_rules_with_client(
    client: Any,
    project: str,
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for _scope, payload in client.aggregated_list(
        request={"project": project},
        timeout=max(1, timeout_seconds),
    ):
        rows.extend(_as_dict_list(protobuf_to_dict(payload).get("forwardingRules")))
    return rows


def _list_forwarding_rules_with_service(compute: Any, project: str) -> list[dict[str, Any]]:
    service = resolve_fallback_service(compute, None, "Compute discovery service unavailable")
    rows: list[dict[str, Any]] = []
    page_token = ""
    while True:
        kwargs: dict[str, Any] = {"project": project}
        if page_token:
            kwargs["pageToken"] = page_token
        response = service.forwardingRules().aggregatedList(**kwargs).execute()
        for _scope, payload in _as_dict(response.get("items")).items():
            if isinstance(payload, dict):
                rows.extend(_as_dict_list(payload.get("forwardingRules")))
        page_token = str(response.get("nextPageToken", "")).strip()
        if not page_token:
            return rows


def _list_dns_zones(
    dns: Any | Callable[[], Any] | None,
    project: str,
    *,
    dns_client: Any | None = None,
) -> list[dict[str, Any]]:
    if dns_client is not None:
        try:
            return [_dns_zone_to_dict(zone) for zone in dns_client.list_zones()]
        except Exception:  # noqa: BLE001
            pass
    return _list_dns_zones_with_service(dns, project)


def _list_dns_zones_with_service(dns: Any, project: str) -> list[dict[str, Any]]:
    service = resolve_fallback_service(dns, None, "Cloud DNS discovery service unavailable")
    rows: list[dict[str, Any]] = []
    page_token = ""
    while True:
        kwargs: dict[str, Any] = {"project": project}
        if page_token:
            kwargs["pageToken"] = page_token
        response = service.managedZones().list(**kwargs).execute()
        rows.extend(_as_dict_list(response.get("managedZones")))
        page_token = str(response.get("nextPageToken", "")).strip()
        if not page_token:
            return rows


def _dns_zone_to_dict(zone: Any) -> dict[str, Any]:
    properties = getattr(zone, "_properties", None)
    if isinstance(properties, dict):
        payload = dict(properties)
    else:
        payload = {}
    payload["name"] = str(payload.get("name") or getattr(zone, "name", "") or "")
    payload["dnsName"] = str(
        payload.get("dnsName") or payload.get("dns_name") or getattr(zone, "dns_name", "") or ""
    )
    payload.pop("dns_name", None)
    description = str(payload.get("description") or getattr(zone, "description", "") or "")
    if description:
        payload["description"] = description
    else:
        payload.pop("description", None)
    return payload


def _count_dns_record_sets(
    dns: Any | Callable[[], Any] | None,
    project: str,
    zone_name: str,
    *,
    dns_client: Any | None = None,
) -> int | None:
    if dns_client is not None:
        try:
            zone = dns_client.zone(zone_name)
            return sum(1 for _record_set in zone.list_resource_record_sets(max_results=500))
        except Exception:  # noqa: BLE001
            pass
    if dns is None:
        return None
    try:
        service = resolve_fallback_service(dns, None, "Cloud DNS discovery service unavailable")
    except Exception:  # noqa: BLE001
        return None
    try:
        count = 0
        page_token = ""
        while True:
            kwargs: dict[str, Any] = {
                "project": project,
                "managedZone": zone_name,
                "maxResults": 500,
            }
            if page_token:
                kwargs["pageToken"] = page_token
            response = service.resourceRecordSets().list(**kwargs).execute()
            count += len(_as_dict_list(response.get("rrsets")))
            page_token = str(response.get("nextPageToken", "")).strip()
            if not page_token:
                return count
    except (AttributeError, HttpError):
        return None


def _list_response_policies(
    dns: Any | Callable[[], Any] | None, project: str
) -> list[dict[str, Any]]:
    if dns is None:
        return []
    try:
        service = resolve_fallback_service(dns, None, "Cloud DNS discovery service unavailable")
        resource = service.responsePolicies()
    except Exception:  # noqa: BLE001
        return []

    rows: list[dict[str, Any]] = []
    page_token = ""
    while True:
        kwargs: dict[str, Any] = {"project": project}
        if page_token:
            kwargs["pageToken"] = page_token
        response = resource.list(**kwargs).execute()
        rows.extend(_as_dict_list(response.get("responsePolicies")))
        page_token = str(response.get("nextPageToken", "")).strip()
        if not page_token:
            return rows


def _list_response_policy_rules(
    dns: Any | Callable[[], Any] | None,
    project: str,
    response_policies: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if dns is None:
        return []
    try:
        service = resolve_fallback_service(dns, None, "Cloud DNS discovery service unavailable")
        resource = service.responsePolicyRules()
    except Exception:  # noqa: BLE001
        return []

    rows: list[dict[str, Any]] = []
    for policy in response_policies:
        policy_name = str(policy.get("responsePolicyName", "")).strip()
        if not policy_name:
            continue
        page_token = ""
        while True:
            kwargs: dict[str, Any] = {
                "project": project,
                "responsePolicy": policy_name,
            }
            if page_token:
                kwargs["pageToken"] = page_token
            response = resource.list(**kwargs).execute()
            for rule in _as_dict_list(response.get("responsePolicyRules")):
                local_data = _as_dict(rule.get("localData"))
                rows.append(
                    {
                        "response_policy": policy_name,
                        "name": str(rule.get("ruleName", "")),
                        "dns_name": str(rule.get("dnsName", "")),
                        "behavior": str(rule.get("behavior", "")),
                        "local_data_record_count": 1 if local_data else 0,
                    }
                )
            page_token = str(response.get("nextPageToken", "")).strip()
            if not page_token:
                break
    return rows


def _collect_internal_dns_state(
    *,
    config: EnvConfig,
    auth: Any,
    container: Any | None,
    timeout_seconds: int,
    cluster_client: Any | None = None,
) -> dict[str, Any]:
    required_fqdns = _required_service_fqdns(config)
    if not required_fqdns:
        return {
            "status": Status.SKIPPED_CONFIG.value,
            "reason": "No in-cluster service FQDNs configured for DNS checks",
            "required_service_fqdns": [],
            "clusters": [],
            "checked_fqdn_total": 0,
            "failed_fqdn_total": 0,
        }

    if container is None:
        return {
            "status": Status.SKIPPED_CONFIG.value,
            "reason": "GKE Container API client unavailable for DNS checks",
            "required_service_fqdns": required_fqdns,
            "clusters": [],
            "checked_fqdn_total": 0,
            "failed_fqdn_total": 0,
        }

    try:
        clusters = resolve_clusters(
            config,
            container,
            cluster_client=cluster_client,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "status": Status.FAILED.value,
            "reason": str(exc),
            "required_service_fqdns": required_fqdns,
            "clusters": [],
            "checked_fqdn_total": 0,
            "failed_fqdn_total": 0,
        }

    if not clusters:
        return {
            "status": Status.SKIPPED_CONFIG.value,
            "reason": "No clusters configured or discovered for DNS checks",
            "required_service_fqdns": required_fqdns,
            "clusters": [],
            "checked_fqdn_total": 0,
            "failed_fqdn_total": 0,
        }

    row_status = Status.OK
    cluster_rows: list[dict[str, Any]] = []
    checked_total = 0
    failed_total = 0
    for cluster in clusters:
        cluster_row, cluster_status = _collect_cluster_dns_state(
            container=container,
            cluster_client=cluster_client,
            auth=auth,
            project=cluster.project,
            region=cluster.region,
            cluster_name=cluster.name,
            required_fqdns=required_fqdns,
            timeout_seconds=timeout_seconds,
        )
        cluster_rows.append(cluster_row)
        row_status = max_status(row_status, cluster_status)
        checked_total += _as_int(cluster_row.get("checked_fqdn_count", 0))
        failed_total += len(_as_str_list(cluster_row.get("failed_fqdns", [])))

    if failed_total > 0:
        row_status = max_status(row_status, Status.WARNING)

    return {
        "status": row_status.value,
        "required_service_fqdns": required_fqdns,
        "clusters": cluster_rows,
        "checked_fqdn_total": checked_total,
        "failed_fqdn_total": failed_total,
    }


def _collect_cluster_dns_state(
    *,
    container: Any,
    auth: Any,
    project: str,
    region: str,
    cluster_name: str,
    required_fqdns: list[str],
    timeout_seconds: int,
    cluster_client: Any | None = None,
) -> tuple[dict[str, Any], Status]:
    cluster_ref = f"projects/{project}/locations/{region}/clusters/{cluster_name}"
    try:
        cluster_payload = get_gke_cluster(
            container,
            cluster_ref,
            cluster_client=cluster_client,
            timeout_seconds=timeout_seconds,
        )
    except HttpError as exc:
        status = classify_http_error(exc)
        return {
            "cluster": cluster_name,
            "project": project,
            "region": region,
            "status": status.value,
            "error": str(exc),
            "checked_fqdn_count": len(required_fqdns),
            "failed_fqdns": required_fqdns,
        }, status
    except Exception as exc:  # noqa: BLE001
        return {
            "cluster": cluster_name,
            "project": project,
            "region": region,
            "status": Status.FAILED.value,
            "error": str(exc),
            "checked_fqdn_count": len(required_fqdns),
            "failed_fqdns": required_fqdns,
        }, Status.FAILED

    endpoint = str(cluster_payload.get("endpoint", "")).strip()
    dns_endpoint = str(
        (
            (cluster_payload.get("controlPlaneEndpointsConfig") or {})
            .get("dnsEndpointConfig", {})
            .get("endpoint", "")
        )
        or ""
    ).strip()
    cert_b64 = str((cluster_payload.get("masterAuth") or {}).get("clusterCaCertificate", "") or "")
    endpoints = preferred_endpoints(dns_endpoint=dns_endpoint, ip_endpoint=endpoint)
    if not endpoints or not cert_b64:
        return {
            "cluster": cluster_name,
            "project": project,
            "region": region,
            "status": Status.FAILED.value,
            "error": "missing cluster endpoint or CA certificate",
            "checked_fqdn_count": len(required_fqdns),
            "failed_fqdns": required_fqdns,
        }, Status.FAILED

    token = access_token(auth.credentials, allow_gcloud_fallback=True)
    if not token:
        return {
            "cluster": cluster_name,
            "project": project,
            "region": region,
            "status": Status.SKIPPED_PERMISSION.value,
            "error": "unable to obtain token for Kubernetes API",
            "checked_fqdn_count": len(required_fqdns),
            "failed_fqdns": required_fqdns,
        }, Status.SKIPPED_PERMISSION

    with cluster_ca_file(cert_b64) as ca_path:
        attempt_errors: list[str] = []
        for candidate, use_cluster_ca in endpoints:
            try:
                with api_client(
                    endpoint=candidate,
                    token=token,
                    ca_path=ca_path if use_cluster_ca else None,
                ) as client:
                    core_api = k8s_client.CoreV1Api(client)
                    services = core_api.list_service_for_all_namespaces(
                        _request_timeout=max(1, timeout_seconds)
                    )
                available_fqdns: set[str] = set()
                for service in services.items:
                    metadata = getattr(service, "metadata", None)
                    name = str(getattr(metadata, "name", "") or "").strip()
                    namespace = str(getattr(metadata, "namespace", "") or "").strip()
                    if name and namespace:
                        available_fqdns.add(f"{name}.{namespace}.svc.cluster.local")
                failed_fqdns = [name for name in required_fqdns if name not in available_fqdns]
                cluster_status = Status.WARNING if failed_fqdns else Status.OK
                return {
                    "cluster": cluster_name,
                    "project": project,
                    "region": region,
                    "status": cluster_status.value,
                    "endpoint": candidate,
                    "checked_fqdn_count": len(required_fqdns),
                    "resolved_fqdns": [name for name in required_fqdns if name in available_fqdns],
                    "failed_fqdns": failed_fqdns,
                }, cluster_status
            except (MaxRetryError, NewConnectionError, TimeoutError) as exc:
                attempt_errors.append(f"{candidate}: {exc}")
                continue
            except k8s_exceptions.ApiException as exc:
                status = _status_for_k8s_api_exception(exc)
                return {
                    "cluster": cluster_name,
                    "project": project,
                    "region": region,
                    "status": status.value,
                    "error": str(exc),
                    "checked_fqdn_count": len(required_fqdns),
                    "failed_fqdns": required_fqdns,
                }, status
        return {
            "cluster": cluster_name,
            "project": project,
            "region": region,
            "status": Status.SKIPPED_NETWORK.value,
            "error": (
                "; ".join(attempt_errors) if attempt_errors else "DNS check endpoint unreachable"
            ),
            "checked_fqdn_count": len(required_fqdns),
            "failed_fqdns": required_fqdns,
        }, Status.SKIPPED_NETWORK


def _status_for_k8s_api_exception(exc: k8s_exceptions.ApiException) -> Status:
    if exc.status in (401, 403):
        return Status.SKIPPED_PERMISSION
    if exc.status == 404:
        return Status.SKIPPED_CONFIG
    return Status.FAILED


def _required_service_fqdns(config: EnvConfig) -> list[str]:
    return _normalize_fqdn_list(config.network.required_service_fqdns)


def _normalize_fqdn_list(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        normalized = value.strip().rstrip(".").lower()
        if normalized and normalized not in out:
            out.append(normalized)
    return out


def _normalize_zone_list(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        normalized = value.strip().rstrip(".").lower()
        if not normalized:
            continue
        normalized = f"{normalized}."
        if normalized not in out:
            out.append(normalized)
    return out


def _status_from_value(value: str) -> Status:
    try:
        return Status(value)
    except ValueError:
        return Status.FAILED


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


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)]


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []
