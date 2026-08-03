from __future__ import annotations

from collections.abc import Callable
from typing import Any

import google_auth_httplib2
import httplib2
from google.cloud import container_v1, logging_v2, monitoring_v3
from google.cloud.compute_v1 import (
    BackendServicesClient,
    FirewallsClient,
    ForwardingRulesClient,
    InstancesClient,
    NetworksClient,
)
from google.cloud.compute_v1.services.region_backend_services import RegionBackendServicesClient
from google.cloud.dns import Client as DnsClient
from google.cloud.logging_v2.services.config_service_v2 import ConfigServiceV2Client
from google.cloud.pubsub_v1 import PublisherClient
from google.protobuf.json_format import MessageToDict
from googleapiclient.discovery import build

from opsbrief.gcp_auth import AuthBundle


def build_service(
    auth: AuthBundle,
    service_name: str,
    version: str,
    timeout_seconds: int = 60,
) -> Any:
    http = google_auth_httplib2.AuthorizedHttp(
        auth.credentials,
        http=httplib2.Http(timeout=max(1, timeout_seconds)),
    )
    return build(
        serviceName=service_name,
        version=version,
        http=http,
        cache_discovery=False,
    )


def lazy_service(
    auth: AuthBundle,
    service_name: str,
    version: str,
    timeout_seconds: int = 60,
) -> Callable[[], Any]:
    service: Any | None = None

    def resolve() -> Any:
        nonlocal service
        if service is None:
            service = build_service(auth, service_name, version, timeout_seconds)
        return service

    return resolve


def resolve_fallback_service(
    service: object | Callable[[], Any] | None,
    client_error: Exception | None,
    unavailable_message: str,
) -> Any:
    if callable(service):
        return service()
    if service is not None:
        return service
    if client_error is not None:
        raise client_error
    raise RuntimeError(unavailable_message)


def build_cloud_sql_admin_service(auth: AuthBundle, timeout_seconds: int = 60) -> Any:
    return build_service(auth, "sqladmin", "v1beta4", timeout_seconds)


def list_cloud_sql_instances(sqladmin: Any, project: str) -> list[dict[str, Any]]:
    response = sqladmin.instances().list(project=project).execute()
    return _response_items(response, "items")


def list_cloud_sql_backup_runs(
    sqladmin: Any,
    *,
    project: str,
    instance: str,
    max_results: int = 1,
) -> list[dict[str, Any]]:
    response = (
        sqladmin.backupRuns()
        .list(
            project=project,
            instance=instance,
            maxResults=max(1, max_results),
        )
        .execute()
    )
    return _response_items(response, "items")


def build_cluster_manager_client(auth: AuthBundle) -> container_v1.ClusterManagerClient:
    return container_v1.ClusterManagerClient(credentials=auth.credentials)


def get_gke_cluster(
    container_service: Any,
    cluster_ref: str,
    *,
    cluster_client: Any | None = None,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    if cluster_client is not None:
        try:
            return protobuf_to_dict(
                cluster_client.get_cluster(
                    name=cluster_ref,
                    timeout=max(1, timeout_seconds),
                )
            )
        except Exception:  # noqa: BLE001
            pass
    payload = container_service.projects().locations().clusters().get(name=cluster_ref).execute()
    return payload if isinstance(payload, dict) else {}


def build_compute_instances_client(auth: AuthBundle) -> InstancesClient:
    return InstancesClient(credentials=auth.credentials)


def build_compute_networks_client(auth: AuthBundle) -> NetworksClient:
    return NetworksClient(credentials=auth.credentials)


def build_compute_firewalls_client(auth: AuthBundle) -> FirewallsClient:
    return FirewallsClient(credentials=auth.credentials)


def build_compute_forwarding_rules_client(auth: AuthBundle) -> ForwardingRulesClient:
    return ForwardingRulesClient(credentials=auth.credentials)


def build_compute_backend_services_client(auth: AuthBundle) -> BackendServicesClient:
    return BackendServicesClient(credentials=auth.credentials)


def build_compute_region_backend_services_client(auth: AuthBundle) -> RegionBackendServicesClient:
    return RegionBackendServicesClient(credentials=auth.credentials)


def build_dns_client(auth: AuthBundle, project: str) -> DnsClient:
    return DnsClient(project=project, credentials=auth.credentials)


def build_logging_config_service_client(auth: AuthBundle) -> ConfigServiceV2Client:
    return ConfigServiceV2Client(credentials=auth.credentials)


def build_logging_client(auth: AuthBundle) -> logging_v2.Client:
    return logging_v2.Client(  # type: ignore[no-untyped-call]
        credentials=auth.credentials,
        project=auth.default_project or None,
    )


def build_alert_policy_service_client(auth: AuthBundle) -> monitoring_v3.AlertPolicyServiceClient:
    return monitoring_v3.AlertPolicyServiceClient(credentials=auth.credentials)


def build_notification_channel_service_client(
    auth: AuthBundle,
) -> monitoring_v3.NotificationChannelServiceClient:
    return monitoring_v3.NotificationChannelServiceClient(credentials=auth.credentials)


def build_metric_service_client(auth: AuthBundle) -> monitoring_v3.MetricServiceClient:
    return monitoring_v3.MetricServiceClient(credentials=auth.credentials)


def build_query_service_client(auth: AuthBundle) -> monitoring_v3.QueryServiceClient:
    return monitoring_v3.QueryServiceClient(credentials=auth.credentials)


def list_monitoring_metric_descriptors(
    metric_client: Any,
    *,
    project: str,
    prefix: str,
    timeout_seconds: int,
    max_descriptors: int = 2000,
    page_size: int = 200,
) -> list[str]:
    rows: list[str] = []
    request = monitoring_v3.ListMetricDescriptorsRequest(
        name=f"projects/{project}",
        filter=f'metric.type = starts_with("{prefix}")',
        page_size=max(1, page_size),
    )
    response = metric_client.list_metric_descriptors(
        request=request,
        timeout=max(1, timeout_seconds),
    )
    descriptor_limit = max(1, max_descriptors)
    for item in response:
        metric_type = str(protobuf_to_dict(item).get("type", "")).strip()
        if metric_type:
            rows.append(metric_type)
        if len(rows) >= descriptor_limit:
            return rows[:descriptor_limit]
    return rows


def query_monitoring_time_series(
    query_client: Any,
    *,
    project: str,
    query: str,
    timeout_seconds: int = 60,
    max_pages: int = 5,
) -> dict[str, Any]:
    response = query_client.query_time_series(
        request={"name": f"projects/{project}", "query": query},
        timeout=max(1, timeout_seconds),
    )
    pages = getattr(response, "pages", None)
    if pages is None:
        return protobuf_to_dict(response)

    payload: dict[str, Any] = {"timeSeriesData": []}
    page_count = 0
    for page in pages:
        page_count += 1
        page_payload = protobuf_to_dict(page)
        descriptor = page_payload.get("timeSeriesDescriptor")
        if descriptor and "timeSeriesDescriptor" not in payload:
            payload["timeSeriesDescriptor"] = descriptor
        data = page_payload.get("timeSeriesData")
        if isinstance(data, list):
            payload["timeSeriesData"].extend(data)
        partial_errors = page_payload.get("partialErrors")
        if isinstance(partial_errors, list):
            payload.setdefault("partialErrors", []).extend(partial_errors)
        if page_count >= max(1, max_pages):
            break
    return payload


def list_monitoring_time_series(
    metric_client: Any,
    *,
    project: str,
    filter_text: str,
    start_text: str,
    end_text: str,
    aligner: str,
    alignment_period: str,
    reducer: str = "",
    group_by_fields: list[str] | None = None,
    page_size: int = 200,
    max_pages: int = 5,
    max_series: int = 300,
    timeout_seconds: int = 60,
) -> list[dict[str, Any]]:
    aggregation: dict[str, Any] = {
        "alignment_period": {"seconds": _duration_seconds(alignment_period)},
        "per_series_aligner": _monitoring_aligner(aligner),
    }
    if reducer:
        aggregation["cross_series_reducer"] = _monitoring_reducer(reducer)
    if group_by_fields:
        aggregation["group_by_fields"] = group_by_fields
    request = monitoring_v3.ListTimeSeriesRequest(
        name=f"projects/{project}",
        filter=filter_text,
        interval={"start_time": start_text, "end_time": end_text},
        view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
        page_size=max(1, page_size),
        aggregation=aggregation,
    )
    response = metric_client.list_time_series(
        request=request,
        timeout=max(1, timeout_seconds),
    )
    return _paged_protobuf_rows(response, max_pages=max_pages, max_series=max_series)


def build_pubsub_publisher_client(auth: AuthBundle) -> PublisherClient:
    return PublisherClient(credentials=auth.credentials)


def build_cloud_redis_client(auth: AuthBundle) -> Any:
    from google.cloud import redis_v1

    return redis_v1.CloudRedisClient(credentials=auth.credentials)


def build_managed_kafka_client(auth: AuthBundle) -> Any:
    from google.cloud import managedkafka_v1

    return managedkafka_v1.ManagedKafkaClient(credentials=auth.credentials)


def list_managed_kafka_clusters(
    service: Any,
    project: str,
    *,
    kafka_client: Any | None = None,
    timeout_seconds: int = 60,
) -> list[dict[str, Any]]:
    parent = f"projects/{project}/locations/-"
    if kafka_client is not None:
        try:
            return [
                protobuf_to_dict(cluster)
                for cluster in kafka_client.list_clusters(
                    parent=parent,
                    timeout=max(1, timeout_seconds),
                )
            ]
        except Exception:  # noqa: BLE001
            pass

    request = service.projects().locations().clusters().list(parent=parent, pageSize=100)
    clusters: list[dict[str, Any]] = []
    while request is not None:
        response = request.execute()
        clusters.extend(_response_items(response, "clusters"))
        request = service.projects().locations().clusters().list_next(request, response)
    return clusters


def build_gke_backup_client(auth: AuthBundle) -> Any:
    from google.cloud import gke_backup_v1

    return gke_backup_v1.BackupForGKEClient(credentials=auth.credentials)


def protobuf_to_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    message = getattr(item, "_pb", item)
    return dict(MessageToDict(message, preserving_proto_field_name=False))


def collect_paged(
    request: Any,
    *,
    item_key: str,
    next_request: Callable[[Any, dict[str, Any]], Any],
) -> list[dict[str, Any]]:
    """Collect dict items from google-api-python-client list pagination."""
    rows: list[dict[str, Any]] = []
    while request is not None:
        response = request.execute()
        if not isinstance(response, dict):
            break
        items = response.get(item_key, [])
        if isinstance(items, list):
            rows.extend(item for item in items if isinstance(item, dict))
        request = next_request(request, response)
    return rows


def _response_items(response: Any, key: str) -> list[dict[str, Any]]:
    if not isinstance(response, dict):
        return []
    items = response.get(key, [])
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _monitoring_aligner(value: str) -> Any:
    return getattr(monitoring_v3.Aggregation.Aligner, value, value)


def _monitoring_reducer(value: str) -> Any:
    return getattr(monitoring_v3.Aggregation.Reducer, value, value)


def _duration_seconds(value: str) -> int:
    text = value.strip().lower()
    if not text:
        return 1
    unit_factors = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    factor = unit_factors.get(text[-1], 1)
    number_text = text[:-1] if text[-1] in unit_factors else text
    try:
        return max(1, int(float(number_text) * factor))
    except ValueError:
        return 1


def _paged_protobuf_rows(
    response: Any,
    *,
    max_pages: int,
    max_series: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page_limit = max(1, max_pages)
    series_limit = max(1, max_series)
    pages = getattr(response, "pages", None)
    if pages is None:
        for item in response:
            rows.append(protobuf_to_dict(item))
            if len(rows) >= series_limit:
                return rows[:series_limit]
        return rows

    page_count = 0
    for page in pages:
        page_count += 1
        for item in page:
            rows.append(protobuf_to_dict(item))
            if len(rows) >= series_limit:
                return rows[:series_limit]
        if page_count >= page_limit:
            return rows[:series_limit]
    return rows[:series_limit]
