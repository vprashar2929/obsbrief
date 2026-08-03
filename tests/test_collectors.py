from __future__ import annotations

import ast
import base64
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from kubernetes.client import exceptions as k8s_exceptions

from opsbrief.collectors import (
    audit,
    backup,
    gke_inventory,
    kubernetes_health,
    mesh,
    monitoring,
    network,
    preflight,
    prometheus_monitoring,
    services,
    trend_metrics,
)
from opsbrief.collectors import (
    logging as logging_collector,
)
from opsbrief.config import (
    ClusterConfig,
    DiscoveryConfig,
    EnvConfig,
    PrometheusConfig,
    ServicesConfig,
)
from opsbrief.gcp_api import list_cloud_sql_backup_runs, list_cloud_sql_instances
from opsbrief.models import Status


class _FakeRequest:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def execute(self) -> dict[str, Any]:
        return self._payload


class _FakeClusters:
    def __init__(self, payloads: dict[str, dict[str, Any]]) -> None:
        self._payloads = payloads
        self.get_calls: list[str] = []

    def get(self, name: str) -> _FakeRequest:
        self.get_calls.append(name)
        return _FakeRequest(self._payloads[name])


class _FakeContainerService:
    def __init__(self, payloads: dict[str, dict[str, Any]]) -> None:
        self._clusters = _FakeClusters(payloads)

    def projects(self) -> _FakeContainerService:
        return self

    def locations(self) -> _FakeContainerService:
        return self

    def clusters(self) -> _FakeClusters:
        return self._clusters


class _FakeClusterManagerGetClient:
    def __init__(
        self,
        *,
        payload: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._payload = payload or {}
        self._error = error
        self.get_calls: list[tuple[str, int]] = []

    def get_cluster(self, *, name: str, timeout: int) -> dict[str, Any]:
        self.get_calls.append((name, timeout))
        if self._error is not None:
            raise self._error
        return self._payload


class _FakeClusterManagerListClient:
    def __init__(
        self,
        *,
        clusters: list[Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._clusters = clusters or []
        self._error = error
        self.calls: list[tuple[str, int]] = []

    def list_clusters(self, *, parent: str, timeout: int) -> SimpleNamespace:
        self.calls.append((parent, timeout))
        if self._error is not None:
            raise self._error
        return SimpleNamespace(clusters=self._clusters)


class _FakeClusterListDiscoveryService:
    def __init__(self, clusters: list[dict[str, Any]]) -> None:
        self._clusters = clusters
        self.list_calls: list[dict[str, Any]] = []

    def projects(self) -> _FakeClusterListDiscoveryService:
        return self

    def locations(self) -> _FakeClusterListDiscoveryService:
        return self

    def clusters(self) -> _FakeClusterListDiscoveryService:
        return self

    def list(self, **kwargs: Any) -> _FakeRequest:
        self.list_calls.append(dict(kwargs))
        return _FakeRequest({"clusters": self._clusters})

    def list_next(
        self,
        previous_request: _FakeRequest,
        previous_response: dict[str, Any],
    ) -> None:
        _ = previous_request
        _ = previous_response
        return None


class _FakeComputeService:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def backendServices(self) -> _FakeComputeService:
        return self

    def regionBackendServices(self) -> _FakeComputeService:
        return self

    def getHealth(self, **_kwargs) -> _FakeRequest:  # noqa: N802
        return _FakeRequest(self._payload)


class _FakeComputeInstancesDiscoveryService:
    def __init__(
        self,
        *,
        instance: dict[str, Any] | None = None,
        aggregated_items: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._instance = instance or {}
        self._aggregated_items = aggregated_items or {}
        self.get_calls: list[tuple[str, str, str]] = []
        self.aggregated_calls: list[tuple[str, int]] = []

    def instances(self) -> _FakeComputeInstancesDiscoveryService:
        return self

    def get(self, *, project: str, zone: str, instance: str) -> _FakeRequest:
        self.get_calls.append((project, zone, instance))
        return _FakeRequest(self._instance)

    def aggregatedList(self, *, project: str, maxResults: int) -> _FakeRequest:  # noqa: N802
        self.aggregated_calls.append((project, maxResults))
        return _FakeRequest({"items": self._aggregated_items})

    def aggregatedList_next(  # noqa: N802
        self,
        previous_request: _FakeRequest,
        previous_response: dict[str, Any],
    ) -> None:
        _ = previous_request
        _ = previous_response
        return None


class _FakeComputeInstancesClient:
    def __init__(
        self,
        *,
        instance: dict[str, Any] | None = None,
        aggregated_items: list[tuple[str, dict[str, Any]]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._instance = instance or {}
        self._aggregated_items = aggregated_items or []
        self._error = error
        self.get_calls: list[tuple[str, str, str, int]] = []
        self.aggregated_calls: list[tuple[dict[str, Any], int]] = []

    def get(self, *, project: str, zone: str, instance: str, timeout: int) -> dict[str, Any]:
        self.get_calls.append((project, zone, instance, timeout))
        if self._error is not None:
            raise self._error
        return self._instance

    def aggregated_list(
        self,
        *,
        request: dict[str, Any],
        timeout: int,
    ) -> list[tuple[str, dict[str, Any]]]:
        self.aggregated_calls.append((request, timeout))
        if self._error is not None:
            raise self._error
        return self._aggregated_items


class _FakeBackendServicesDiscoveryService:
    def __init__(
        self,
        *,
        global_backend: dict[str, Any] | None = None,
        regional_backend: dict[str, Any] | None = None,
        aggregated_items: dict[str, dict[str, Any]] | None = None,
        health: dict[str, Any] | None = None,
    ) -> None:
        self._global_backend = global_backend or {}
        self._regional_backend = regional_backend or {}
        self._aggregated_items = aggregated_items or {}
        self._health = health or {"healthStatus": [{"healthState": "HEALTHY"}]}
        self.global_get_calls: list[tuple[str, str]] = []
        self.regional_get_calls: list[tuple[str, str, str]] = []
        self.aggregated_calls: list[tuple[str, int]] = []
        self.health_calls: list[dict[str, Any]] = []

    def backendServices(self) -> _FakeBackendServicesDiscoveryService:  # noqa: N802
        return self

    def regionBackendServices(self) -> _FakeBackendServicesDiscoveryService:  # noqa: N802
        return self

    def get(self, **kwargs: Any) -> _FakeRequest:
        project = str(kwargs["project"])
        backend_service = str(kwargs["backendService"])
        region = str(kwargs.get("region", ""))
        if region:
            self.regional_get_calls.append((project, region, backend_service))
            return _FakeRequest(self._regional_backend)
        self.global_get_calls.append((project, backend_service))
        return _FakeRequest(self._global_backend)

    def getHealth(self, **kwargs: Any) -> _FakeRequest:  # noqa: N802
        self.health_calls.append(dict(kwargs))
        return _FakeRequest(self._health)

    def aggregatedList(self, *, project: str, maxResults: int) -> _FakeRequest:  # noqa: N802
        self.aggregated_calls.append((project, maxResults))
        return _FakeRequest({"items": self._aggregated_items})

    def aggregatedList_next(  # noqa: N802
        self,
        previous_request: _FakeRequest,
        previous_response: dict[str, Any],
    ) -> None:
        _ = previous_request
        _ = previous_response
        return None


class _FakeBackendServicesClient:
    def __init__(
        self,
        *,
        backend: dict[str, Any] | None = None,
        aggregated_items: list[tuple[str, dict[str, Any]]] | None = None,
        health: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._backend = backend or {}
        self._aggregated_items = aggregated_items or []
        self._health = health or {"healthStatus": [{"healthState": "HEALTHY"}]}
        self._error = error
        self.get_calls: list[tuple[str, str, int]] = []
        self.aggregated_calls: list[tuple[dict[str, Any], int]] = []
        self.health_calls: list[tuple[str, str, str, int]] = []

    def get(self, *, project: str, backend_service: str, timeout: int) -> dict[str, Any]:
        self.get_calls.append((project, backend_service, timeout))
        if self._error is not None:
            raise self._error
        return self._backend

    def get_health(
        self,
        *,
        project: str,
        backend_service: str,
        resource_group_reference_resource: dict[str, Any],
        timeout: int,
    ) -> dict[str, Any]:
        self.health_calls.append(
            (
                project,
                backend_service,
                str(resource_group_reference_resource.get("group", "")),
                timeout,
            )
        )
        if self._error is not None:
            raise self._error
        return self._health

    def aggregated_list(
        self,
        *,
        request: dict[str, Any],
        timeout: int,
    ) -> list[tuple[str, dict[str, Any]]]:
        self.aggregated_calls.append((request, timeout))
        if self._error is not None:
            raise self._error
        return self._aggregated_items


class _FakeRegionBackendServicesClient:
    def __init__(
        self,
        *,
        backend: dict[str, Any] | None = None,
        health: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._backend = backend or {}
        self._health = health or {"healthStatus": [{"healthState": "HEALTHY"}]}
        self._error = error
        self.get_calls: list[tuple[str, str, str, int]] = []
        self.health_calls: list[tuple[str, str, str, str, int]] = []

    def get(
        self,
        *,
        project: str,
        region: str,
        backend_service: str,
        timeout: int,
    ) -> dict[str, Any]:
        self.get_calls.append((project, region, backend_service, timeout))
        if self._error is not None:
            raise self._error
        return self._backend

    def get_health(
        self,
        *,
        project: str,
        region: str,
        backend_service: str,
        resource_group_reference_resource: dict[str, Any],
        timeout: int,
    ) -> dict[str, Any]:
        self.health_calls.append(
            (
                project,
                region,
                backend_service,
                str(resource_group_reference_resource.get("group", "")),
                timeout,
            )
        )
        if self._error is not None:
            raise self._error
        return self._health


class _FakeComputeListCollection:
    def __init__(self, item_key: str, items: list[dict[str, Any]]) -> None:
        self._item_key = item_key
        self._items = items
        self.list_calls: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> _FakeRequest:
        self.list_calls.append(dict(kwargs))
        return _FakeRequest({self._item_key: self._items})

    def list_next(
        self,
        previous_request: _FakeRequest,
        previous_response: dict[str, Any],
    ) -> None:
        _ = previous_request
        _ = previous_response
        return None


class _FakeComputeNetworkDiscoveryService:
    def __init__(
        self,
        *,
        networks: list[dict[str, Any]] | None = None,
        firewalls: list[dict[str, Any]] | None = None,
        forwarding_rule_items: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._networks = _FakeComputeListCollection("items", networks or [])
        self._firewalls = _FakeComputeListCollection("items", firewalls or [])
        self._forwarding_rule_items = forwarding_rule_items or {}
        self.forwarding_rule_calls: list[dict[str, Any]] = []

    def networks(self) -> _FakeComputeListCollection:
        return self._networks

    def firewalls(self) -> _FakeComputeListCollection:
        return self._firewalls

    def forwardingRules(self) -> _FakeComputeNetworkDiscoveryService:  # noqa: N802
        return self

    def aggregatedList(self, **kwargs: Any) -> _FakeRequest:  # noqa: N802
        self.forwarding_rule_calls.append(dict(kwargs))
        return _FakeRequest({"items": self._forwarding_rule_items})

    def aggregatedList_next(  # noqa: N802
        self,
        previous_request: _FakeRequest,
        previous_response: dict[str, Any],
    ) -> None:
        _ = previous_request
        _ = previous_response
        return None


class _FakeComputeListClient:
    def __init__(
        self,
        *,
        items: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._items = items or []
        self._error = error
        self.list_calls: list[tuple[str, int]] = []

    def list(self, *, project: str, timeout: int) -> list[dict[str, Any]]:
        self.list_calls.append((project, timeout))
        if self._error is not None:
            raise self._error
        return self._items


class _FakeComputeAggregatedListClient:
    def __init__(
        self,
        *,
        items: list[tuple[str, dict[str, Any]]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._items = items or []
        self._error = error
        self.aggregated_calls: list[tuple[dict[str, Any], int]] = []

    def aggregated_list(
        self,
        *,
        request: dict[str, Any],
        timeout: int,
    ) -> list[tuple[str, dict[str, Any]]]:
        self.aggregated_calls.append((request, timeout))
        if self._error is not None:
            raise self._error
        return self._items


class _FakeDnsZone:
    def __init__(
        self,
        *,
        name: str,
        dns_name: str,
        properties: dict[str, Any] | None = None,
        record_sets: list[object] | None = None,
    ) -> None:
        self.name = name
        self.dns_name = dns_name
        self.description = ""
        self._properties = properties
        self._record_sets = record_sets or []
        self.record_set_calls: list[int | None] = []

    def list_resource_record_sets(self, max_results: int | None = None) -> list[object]:
        self.record_set_calls.append(max_results)
        return self._record_sets


class _FakeDnsClient:
    def __init__(
        self,
        *,
        zones: list[_FakeDnsZone] | None = None,
        zone_records: dict[str, list[object]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._zones = zones or []
        self._zone_records = zone_records or {}
        self._error = error
        self.list_zone_calls: list[int | None] = []
        self.zone_calls: list[str] = []

    def list_zones(self, max_results: int | None = None) -> list[_FakeDnsZone]:
        self.list_zone_calls.append(max_results)
        if self._error is not None:
            raise self._error
        return self._zones

    def zone(self, name: str) -> _FakeDnsZone:
        self.zone_calls.append(name)
        if self._error is not None:
            raise self._error
        return _FakeDnsZone(
            name=name,
            dns_name="",
            record_sets=self._zone_records.get(name, []),
        )


class _FakeMonitoringCollectionApi:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def list(self, **_kwargs: Any) -> _FakeRequest:
        return _FakeRequest(self._payload)

    def list_next(
        self,
        previous_request: _FakeRequest,
        previous_response: dict[str, Any],
    ) -> None:
        _ = previous_request
        _ = previous_response
        return None


class _FakeMonitoringDiscoveryService:
    def __init__(
        self,
        *,
        alert_policies: list[dict[str, Any]] | None = None,
        notification_channels: list[dict[str, Any]] | None = None,
    ) -> None:
        self._alert_policies = _FakeMonitoringCollectionApi({"alertPolicies": alert_policies or []})
        self._notification_channels = _FakeMonitoringCollectionApi(
            {"notificationChannels": notification_channels or []}
        )

    def projects(self) -> _FakeMonitoringDiscoveryService:
        return self

    def alertPolicies(self) -> _FakeMonitoringCollectionApi:  # noqa: N802
        return self._alert_policies

    def notificationChannels(self) -> _FakeMonitoringCollectionApi:  # noqa: N802
        return self._notification_channels


class _FakeMonitoringResourceClient:
    def __init__(
        self,
        *,
        alert_policies: list[dict[str, Any]] | None = None,
        notification_channels: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._alert_policies = alert_policies or []
        self._notification_channels = notification_channels or []
        self._error = error
        self.alert_policy_calls: list[tuple[str, int]] = []
        self.notification_channel_calls: list[tuple[str, int]] = []

    def list_alert_policies(
        self,
        *,
        request: dict[str, Any] | None = None,
        name: str = "",
        timeout: int,
    ) -> list[dict[str, Any]]:
        if request is not None:
            name = str(request.get("name", ""))
        self.alert_policy_calls.append((name, timeout))
        if self._error is not None:
            raise self._error
        return self._alert_policies

    def list_notification_channels(self, *, name: str, timeout: int) -> list[dict[str, Any]]:
        self.notification_channel_calls.append((name, timeout))
        if self._error is not None:
            raise self._error
        return self._notification_channels


class _FakeMetricPager:
    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        self.pages = pages

    def __iter__(self) -> Any:
        for page in self.pages:
            yield from page


class _FakeMetricServiceClient:
    def __init__(
        self,
        *,
        time_series_pages: list[list[dict[str, Any]]] | None = None,
        metric_descriptors: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._time_series_pages = time_series_pages or []
        self._metric_descriptors = metric_descriptors or []
        self._error = error
        self.time_series_calls: list[tuple[Any, int]] = []
        self.metric_descriptor_calls: list[tuple[Any, int]] = []

    def list_time_series(self, *, request: Any, timeout: int) -> _FakeMetricPager:
        self.time_series_calls.append((request, timeout))
        if self._error is not None:
            raise self._error
        return _FakeMetricPager(self._time_series_pages)

    def list_metric_descriptors(self, *, request: Any, timeout: int) -> list[dict[str, Any]]:
        self.metric_descriptor_calls.append((request, timeout))
        if self._error is not None:
            raise self._error
        return self._metric_descriptors


class _FakeQueryTimeSeriesPager:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages


class _FakeQueryServiceClient:
    def __init__(
        self,
        *,
        pages: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._pages = pages or []
        self._error = error
        self.query_calls: list[tuple[Any, int]] = []

    def query_time_series(self, *, request: Any, timeout: int) -> _FakeQueryTimeSeriesPager:
        self.query_calls.append((request, timeout))
        if self._error is not None:
            raise self._error
        return _FakeQueryTimeSeriesPager(self._pages)


class _FakeCloudSqlInstancesApi:
    def __init__(self, instances: list[dict[str, Any]]) -> None:
        self._instances = instances
        self.list_calls: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> _FakeRequest:
        self.list_calls.append(dict(kwargs))
        return _FakeRequest({"items": self._instances})


class _FakeCloudSqlBackupRunsApi:
    def __init__(self, backup_runs: list[dict[str, Any]]) -> None:
        self._backup_runs = backup_runs
        self.list_calls: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> _FakeRequest:
        self.list_calls.append(dict(kwargs))
        return _FakeRequest({"items": self._backup_runs})


class _FakeCloudSqlAdminService:
    def __init__(
        self,
        *,
        instances: list[dict[str, Any]] | None = None,
        backup_runs: list[dict[str, Any]] | None = None,
    ) -> None:
        self._instances = _FakeCloudSqlInstancesApi(instances or [])
        self._backup_runs = _FakeCloudSqlBackupRunsApi(backup_runs or [])

    def instances(self) -> _FakeCloudSqlInstancesApi:
        return self._instances

    def backupRuns(self) -> _FakeCloudSqlBackupRunsApi:  # noqa: N802
        return self._backup_runs


class _FakeLoggingCollectionApi:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.list_calls: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> _FakeRequest:
        self.list_calls.append(dict(kwargs))
        return _FakeRequest(self._payload)

    def list_next(
        self,
        previous_request: _FakeRequest,
        previous_response: dict[str, Any],
    ) -> None:
        _ = previous_request
        _ = previous_response
        return None


class _FakeLoggingDiscoveryService:
    def __init__(
        self,
        *,
        sinks: list[dict[str, Any]] | None = None,
        buckets: list[dict[str, Any]] | None = None,
        entries: list[dict[str, Any]] | None = None,
        entries_next_page_token: str = "",
    ) -> None:
        self._sinks = _FakeLoggingCollectionApi({"sinks": sinks or []})
        self._buckets = _FakeLoggingCollectionApi({"buckets": buckets or []})
        self._entries = _FakeLoggingCollectionApi(
            {"entries": entries or [], "nextPageToken": entries_next_page_token}
        )

    def projects(self) -> _FakeLoggingDiscoveryService:
        return self

    def locations(self) -> _FakeLoggingDiscoveryService:
        return self

    def sinks(self) -> _FakeLoggingCollectionApi:
        return self._sinks

    def buckets(self) -> _FakeLoggingCollectionApi:
        return self._buckets

    def entries(self) -> _FakeLoggingCollectionApi:
        return self._entries


class _FakeLoggingConfigClient:
    def __init__(
        self,
        *,
        sinks: list[dict[str, Any]] | None = None,
        buckets: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._sinks = sinks or []
        self._buckets = buckets or []
        self._error = error
        self.sink_calls: list[tuple[str, int]] = []
        self.bucket_calls: list[tuple[str, int]] = []

    def list_sinks(self, *, parent: str, timeout: int) -> list[dict[str, Any]]:
        self.sink_calls.append((parent, timeout))
        if self._error is not None:
            raise self._error
        return self._sinks

    def list_buckets(self, *, parent: str, timeout: int) -> list[dict[str, Any]]:
        self.bucket_calls.append((parent, timeout))
        if self._error is not None:
            raise self._error
        return self._buckets


class _FakeLoggingEntryResource:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def to_api_repr(self) -> dict[str, Any]:
        return self._payload


class _FakeLoggingEntriesClient:
    def __init__(
        self,
        *,
        entries: list[Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._entries = entries or []
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def list_entries(self, **kwargs: Any) -> list[Any]:
        self.calls.append(dict(kwargs))
        if self._error is not None:
            raise self._error
        return self._entries


class _FakePubsubDiscoveryService:
    def __init__(
        self,
        *,
        topic: dict[str, Any] | None = None,
        subscriptions: list[str] | None = None,
    ) -> None:
        self._topic = topic or {}
        self._subscriptions = subscriptions or []

    def projects(self) -> _FakePubsubDiscoveryService:
        return self

    def topics(self) -> _FakePubsubDiscoveryService:
        return self

    def subscriptions(self) -> _FakePubsubDiscoveryService:
        return self

    def get(self, *, topic: str) -> _FakeRequest:
        _ = topic
        return _FakeRequest(self._topic)

    def list(self, *, topic: str, pageSize: int | None = None) -> _FakeRequest:  # noqa: N803
        _ = topic
        _ = pageSize
        return _FakeRequest({"subscriptions": self._subscriptions})

    def list_next(
        self,
        previous_request: _FakeRequest,
        previous_response: dict[str, Any],
    ) -> None:
        _ = previous_request
        _ = previous_response
        return None


class _FakePubsubPublisherClient:
    def __init__(
        self,
        *,
        topic: dict[str, Any] | None = None,
        subscriptions: list[str] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._topic = topic or {}
        self._subscriptions = subscriptions or []
        self._error = error
        self.topic_calls: list[tuple[str, int]] = []
        self.subscription_calls: list[tuple[str, int]] = []

    def get_topic(self, *, topic: str, timeout: int) -> dict[str, Any]:
        self.topic_calls.append((topic, timeout))
        if self._error is not None:
            raise self._error
        return self._topic

    def list_topic_subscriptions(self, *, topic: str, timeout: int) -> list[str]:
        self.subscription_calls.append((topic, timeout))
        if self._error is not None:
            raise self._error
        return self._subscriptions


class _FakeRedisDiscoveryService:
    def __init__(self, instances: list[dict[str, Any]]) -> None:
        self._instances = instances
        self.list_calls: list[dict[str, Any]] = []

    def projects(self) -> _FakeRedisDiscoveryService:
        return self

    def locations(self) -> _FakeRedisDiscoveryService:
        return self

    def instances(self) -> _FakeRedisDiscoveryService:
        return self

    def list(self, **kwargs: Any) -> _FakeRequest:
        self.list_calls.append(dict(kwargs))
        return _FakeRequest({"instances": self._instances})

    def list_next(
        self,
        previous_request: _FakeRequest,
        previous_response: dict[str, Any],
    ) -> None:
        _ = previous_request
        _ = previous_response
        return None


class _FakeRedisClient:
    def __init__(
        self,
        *,
        instances: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._instances = instances or []
        self._error = error
        self.calls: list[tuple[str, int]] = []

    def list_instances(self, *, parent: str, timeout: int) -> list[dict[str, Any]]:
        self.calls.append((parent, timeout))
        if self._error is not None:
            raise self._error
        return self._instances


class _FakeManagedKafkaDiscoveryService:
    def __init__(self, clusters: list[dict[str, Any]]) -> None:
        self._clusters = clusters
        self.list_calls: list[dict[str, Any]] = []

    def projects(self) -> _FakeManagedKafkaDiscoveryService:
        return self

    def locations(self) -> _FakeManagedKafkaDiscoveryService:
        return self

    def clusters(self) -> _FakeManagedKafkaDiscoveryService:
        return self

    def list(self, **kwargs: Any) -> _FakeRequest:
        self.list_calls.append(dict(kwargs))
        return _FakeRequest({"clusters": self._clusters})

    def list_next(
        self,
        previous_request: _FakeRequest,
        previous_response: dict[str, Any],
    ) -> None:
        _ = previous_request
        _ = previous_response
        return None


class _FakeManagedKafkaClient:
    def __init__(
        self,
        *,
        clusters: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._clusters = clusters or []
        self._error = error
        self.calls: list[tuple[str, int]] = []

    def list_clusters(self, *, parent: str, timeout: int) -> list[dict[str, Any]]:
        self.calls.append((parent, timeout))
        if self._error is not None:
            raise self._error
        return self._clusters


class _FakeGkeBackupDiscoveryService:
    def __init__(self, plans: list[dict[str, Any]]) -> None:
        self._plans = plans
        self.list_calls: list[dict[str, Any]] = []

    def projects(self) -> _FakeGkeBackupDiscoveryService:
        return self

    def locations(self) -> _FakeGkeBackupDiscoveryService:
        return self

    def backupPlans(self) -> _FakeGkeBackupDiscoveryService:  # noqa: N802
        return self

    def list(self, **kwargs: Any) -> _FakeRequest:
        self.list_calls.append(dict(kwargs))
        return _FakeRequest({"backupPlans": self._plans})

    def list_next(
        self,
        previous_request: _FakeRequest,
        previous_response: dict[str, Any],
    ) -> None:
        _ = previous_request
        _ = previous_response
        return None


class _FakeGkeBackupClient:
    def __init__(
        self,
        *,
        plans: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._plans = plans or []
        self._error = error
        self.calls: list[tuple[str, int]] = []

    def list_backup_plans(self, *, parent: str, timeout: int) -> list[dict[str, Any]]:
        self.calls.append((parent, timeout))
        if self._error is not None:
            raise self._error
        return self._plans


def _auth_bundle(token: str = "token") -> SimpleNamespace:
    credentials = SimpleNamespace(token=token, refresh=lambda _req: None)
    return SimpleNamespace(
        credentials=credentials,
        default_project="example-dev-project",
        principal_hint="dev-user@example.com",
    )


def _default_config() -> EnvConfig:
    return EnvConfig(
        environment="dev",
        default_region="us-central1",
        projects={"component": "example-dev-project"},
        collectors={
            "preflight": True,
            "gke_inventory": True,
            "kubernetes_health": True,
            "monitoring": True,
            "prometheus_monitoring": True,
            "logging": True,
            "audit": True,
            "network": True,
            "mesh": True,
            "trend_metrics": True,
            "backup": True,
            "services": True,
        },
        clusters=[
            ClusterConfig(
                name="dev-cluster",
                project="example-dev-project",
                region="us-central1",
            )
        ],
        services=ServicesConfig(redis=False, managed_kafka=False),
        discovery=DiscoveryConfig(
            auto_discover_clusters=False,
            auto_discover_compute_instances=False,
            auto_discover_load_balancers=False,
        ),
    )


def _cluster_request_name(cluster: ClusterConfig) -> str:
    return f"projects/{cluster.project}/locations/{cluster.region}/clusters/{cluster.name}"


def test_preflight_collect_offline(monkeypatch, tmp_path) -> None:
    config = _default_config()
    request_name = _cluster_request_name(config.clusters[0])
    payloads = {
        request_name: {
            "endpoint": "10.0.0.5",
            "masterAuth": {
                "clusterCaCertificate": base64.b64encode(b"dummy-ca-cert").decode("utf-8")
            },
        }
    }

    monkeypatch.setattr(preflight, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(
        preflight, "build_service", lambda *_args, **_kwargs: _FakeContainerService(payloads)
    )
    monkeypatch.setattr(preflight, "build_cluster_manager_client", lambda _auth: None)
    monkeypatch.setattr(preflight, "_probe_api_for_collector", lambda *_args, **_kwargs: Status.OK)
    monkeypatch.setattr(
        preflight,
        "_probe_kubernetes_permissions",
        lambda **_kwargs: (
            Status.OK,
            [
                {
                    "name": "k8s:dev-cluster:nodes:list",
                    "status": Status.OK.value,
                    "message": "allowed",
                }
            ],
        ),
    )

    result = preflight.collect(config=config, output_dir=str(tmp_path))

    assert result.status == Status.OK
    checks = result.details["checks"]
    assert any(item["name"] == "credentials" for item in checks)
    assert any(item["name"] == "cluster:dev-cluster" for item in checks)


def test_preflight_gets_cluster_with_cluster_manager_client(monkeypatch) -> None:
    config = _default_config()
    request_name = _cluster_request_name(config.clusters[0])
    service = _FakeContainerService(
        {request_name: _kubernetes_cluster_payload(endpoint="10.0.0.5", dns_endpoint="")}
    )
    client = _FakeClusterManagerGetClient(
        payload=_kubernetes_cluster_payload(endpoint="10.0.0.6", dns_endpoint="")
    )

    monkeypatch.setattr(preflight, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(preflight, "build_service", lambda *_args, **_kwargs: service)
    monkeypatch.setattr(preflight, "build_cluster_manager_client", lambda _auth: client)
    monkeypatch.setattr(preflight, "_probe_api_for_collector", lambda *_args, **_kwargs: Status.OK)
    monkeypatch.setattr(
        preflight,
        "_probe_kubernetes_permissions",
        lambda **_kwargs: (
            Status.OK,
            [
                {
                    "name": "k8s:dev-cluster:nodes:list",
                    "status": Status.OK.value,
                    "message": "allowed",
                }
            ],
        ),
    )

    result = preflight.collect(config=config, timeout_seconds=17)

    assert result.status == Status.OK
    cluster_check = next(
        item for item in result.details["checks"] if item["name"] == "cluster:dev-cluster"
    )
    assert cluster_check["message"] == "10.0.0.6"
    assert client.get_calls == [(request_name, 17)]
    assert service.clusters().get_calls == []


def test_preflight_falls_back_to_discovery_when_cluster_manager_client_fails(
    monkeypatch,
) -> None:
    config = _default_config()
    request_name = _cluster_request_name(config.clusters[0])
    service = _FakeContainerService(
        {request_name: _kubernetes_cluster_payload(endpoint="10.0.0.5", dns_endpoint="")}
    )
    client = _FakeClusterManagerGetClient(error=RuntimeError("client unavailable"))

    monkeypatch.setattr(preflight, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(preflight, "build_service", lambda *_args, **_kwargs: service)
    monkeypatch.setattr(preflight, "build_cluster_manager_client", lambda _auth: client)
    monkeypatch.setattr(preflight, "_probe_api_for_collector", lambda *_args, **_kwargs: Status.OK)
    monkeypatch.setattr(
        preflight,
        "_probe_kubernetes_permissions",
        lambda **_kwargs: (
            Status.OK,
            [
                {
                    "name": "k8s:dev-cluster:nodes:list",
                    "status": Status.OK.value,
                    "message": "allowed",
                }
            ],
        ),
    )

    result = preflight.collect(config=config, timeout_seconds=17)

    assert result.status == Status.OK
    cluster_check = next(
        item for item in result.details["checks"] if item["name"] == "cluster:dev-cluster"
    )
    assert cluster_check["message"] == "10.0.0.5"
    assert client.get_calls == [(request_name, 17)]
    assert service.clusters().get_calls == [request_name]


def test_preflight_probes_compute_networks_with_compute_client(monkeypatch) -> None:
    client = _FakeComputeListClient(items=[{"name": "dev-vpc"}])

    def unexpected_build_service(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("discovery fallback should not be called")

    monkeypatch.setattr(preflight, "build_compute_networks_client", lambda _auth: client)
    monkeypatch.setattr(preflight, "build_service", unexpected_build_service)

    preflight._probe_compute_networks_api(_auth_bundle(), "example-dev-project", 17)

    assert client.list_calls == [("example-dev-project", 17)]


def test_preflight_falls_back_to_discovery_for_compute_networks(monkeypatch) -> None:
    service = _FakeComputeNetworkDiscoveryService(networks=[{"name": "fallback-vpc"}])
    client = _FakeComputeListClient(error=RuntimeError("client unavailable"))

    monkeypatch.setattr(preflight, "build_compute_networks_client", lambda _auth: client)
    monkeypatch.setattr(preflight, "build_service", lambda *_args, **_kwargs: service)

    preflight._probe_compute_networks_api(_auth_bundle(), "example-dev-project", 17)

    assert client.list_calls == [("example-dev-project", 17)]
    assert service.networks().list_calls == [{"project": "example-dev-project"}]


def test_preflight_monitoring_api_uses_alert_policy_client(monkeypatch) -> None:
    config = _default_config()
    client = _FakeMonitoringResourceClient(alert_policies=[{"name": "client-policy"}])
    checks: list[dict[str, str]] = []
    errors: list[str] = []

    def unexpected_build_service(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("discovery fallback should not be called")

    monkeypatch.setattr(preflight, "build_alert_policy_service_client", lambda _auth: client)
    monkeypatch.setattr(preflight, "build_service", unexpected_build_service)

    status = preflight._probe_api_for_collector(
        config,
        "monitoring",
        _auth_bundle(),
        checks,
        errors,
        17,
    )

    assert status == Status.OK
    assert checks == [{"name": "api:monitoring", "status": "ok", "message": "reachable"}]
    assert errors == []
    assert client.alert_policy_calls == [("projects/example-dev-project", 17)]


def test_preflight_trend_metrics_api_uses_metric_client(monkeypatch) -> None:
    config = _default_config()
    client = _FakeMetricServiceClient(metric_descriptors=[{"type": "compute.googleapis.com/foo"}])
    checks: list[dict[str, str]] = []
    errors: list[str] = []
    probed: list[str] = []

    def unexpected_build_service(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("discovery fallback should not be called")

    monkeypatch.setattr(preflight, "build_metric_service_client", lambda _auth: client)
    monkeypatch.setattr(preflight, "build_service", unexpected_build_service)
    monkeypatch.setattr(
        preflight,
        "_probe_cloud_sql_api",
        lambda *_args, **_kwargs: probed.append("cloud_sql"),
    )
    monkeypatch.setattr(
        preflight,
        "_probe_compute_instances_api",
        lambda *_args, **_kwargs: probed.append("compute"),
    )

    status = preflight._probe_api_for_collector(
        config,
        "trend_metrics",
        _auth_bundle(),
        checks,
        errors,
        17,
    )

    assert status == Status.OK
    assert checks == [{"name": "api:trend_metrics", "status": "ok", "message": "reachable"}]
    assert errors == []
    assert probed == ["cloud_sql", "compute"]
    assert len(client.metric_descriptor_calls) == 1
    request, timeout = client.metric_descriptor_calls[0]
    assert request == {"name": "projects/example-dev-project", "page_size": 1}
    assert timeout == 17


def test_preflight_mesh_api_uses_cluster_manager_client(monkeypatch) -> None:
    config = _default_config()
    client = _FakeClusterManagerListClient(clusters=[{"name": "dev-cluster"}])
    checks: list[dict[str, str]] = []
    errors: list[str] = []

    def unexpected_build_service(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("discovery fallback should not be called")

    monkeypatch.setattr(preflight, "build_cluster_manager_client", lambda _auth: client)
    monkeypatch.setattr(preflight, "build_service", unexpected_build_service)

    status = preflight._probe_api_for_collector(
        config,
        "mesh",
        _auth_bundle(),
        checks,
        errors,
        17,
    )

    assert status == Status.OK
    assert checks == [{"name": "api:mesh", "status": "ok", "message": "reachable"}]
    assert errors == []
    assert client.calls == [("projects/example-dev-project/locations/-", 17)]


def test_preflight_probes_dns_with_dns_client(monkeypatch) -> None:
    client = _FakeDnsClient(
        zones=[
            _FakeDnsZone(
                name="internal-zone",
                dns_name="internal.example.",
                properties={"name": "internal-zone", "dnsName": "internal.example."},
            )
        ]
    )

    def unexpected_build_service(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("discovery fallback should not be called")

    monkeypatch.setattr(preflight, "build_dns_client", lambda _auth, _project: client)
    monkeypatch.setattr(preflight, "build_service", unexpected_build_service)

    preflight._probe_dns_zones_api(_auth_bundle(), "example-dev-project", 17)

    assert client.list_zone_calls == [1]


def test_preflight_logging_api_uses_logging_config_client(monkeypatch) -> None:
    config = _default_config()
    client = _FakeLoggingConfigClient(sinks=[{"name": "client-sink"}])
    checks: list[dict[str, str]] = []
    errors: list[str] = []

    def unexpected_build_service(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("discovery fallback should not be called")

    monkeypatch.setattr(preflight, "build_logging_config_service_client", lambda _auth: client)
    monkeypatch.setattr(preflight, "build_service", unexpected_build_service)

    status = preflight._probe_api_for_collector(
        config,
        "logging",
        _auth_bundle(),
        checks,
        errors,
        17,
    )

    assert status == Status.OK
    assert checks == [{"name": "api:logging", "status": "ok", "message": "reachable"}]
    assert errors == []
    assert client.sink_calls == [("projects/example-dev-project", 17)]


def test_preflight_logging_api_falls_back_to_discovery_when_config_client_fails(
    monkeypatch,
) -> None:
    config = _default_config()
    client = _FakeLoggingConfigClient(error=RuntimeError("client unavailable"))
    service = _FakeLoggingDiscoveryService(sinks=[{"name": "fallback-sink"}])
    checks: list[dict[str, str]] = []
    errors: list[str] = []

    monkeypatch.setattr(preflight, "build_logging_config_service_client", lambda _auth: client)
    monkeypatch.setattr(preflight, "build_service", lambda *_args, **_kwargs: service)

    status = preflight._probe_api_for_collector(
        config,
        "logging",
        _auth_bundle(),
        checks,
        errors,
        17,
    )

    assert status == Status.OK
    assert checks == [{"name": "api:logging", "status": "ok", "message": "reachable"}]
    assert errors == []
    assert client.sink_calls == [("projects/example-dev-project", 17)]
    assert service.sinks().list_calls == [{"parent": "projects/example-dev-project", "pageSize": 1}]


def test_preflight_audit_api_uses_logging_client(monkeypatch) -> None:
    config = _default_config()
    client = _FakeLoggingEntriesClient(entries=[{"timestamp": "2026-05-14T00:00:00Z"}])
    checks: list[dict[str, str]] = []
    errors: list[str] = []

    def unexpected_build_service(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("discovery fallback should not be called")

    monkeypatch.setattr(preflight, "build_logging_client", lambda _auth: client)
    monkeypatch.setattr(preflight, "build_service", unexpected_build_service)

    status = preflight._probe_api_for_collector(
        config,
        "audit",
        _auth_bundle(),
        checks,
        errors,
        17,
    )

    assert status == Status.OK
    assert checks == [{"name": "api:audit", "status": "ok", "message": "reachable"}]
    assert errors == []
    assert client.calls == [
        {
            "resource_names": ["projects/example-dev-project"],
            "order_by": "timestamp desc",
            "max_results": 1,
            "page_size": 1,
        }
    ]


def test_preflight_audit_api_falls_back_to_discovery_when_logging_client_fails(
    monkeypatch,
) -> None:
    config = _default_config()
    client = _FakeLoggingEntriesClient(error=RuntimeError("client unavailable"))
    service = _FakeLoggingDiscoveryService(entries=[{"timestamp": "2026-05-14T00:00:00Z"}])
    checks: list[dict[str, str]] = []
    errors: list[str] = []

    monkeypatch.setattr(preflight, "build_logging_client", lambda _auth: client)
    monkeypatch.setattr(preflight, "build_service", lambda *_args, **_kwargs: service)

    status = preflight._probe_api_for_collector(
        config,
        "audit",
        _auth_bundle(),
        checks,
        errors,
        17,
    )

    assert status == Status.OK
    assert checks == [{"name": "api:audit", "status": "ok", "message": "reachable"}]
    assert errors == []
    assert client.calls == [
        {
            "resource_names": ["projects/example-dev-project"],
            "order_by": "timestamp desc",
            "max_results": 1,
            "page_size": 1,
        }
    ]
    assert service.entries().list_calls == [
        {
            "body": {
                "resourceNames": ["projects/example-dev-project"],
                "orderBy": "timestamp desc",
                "pageSize": 1,
            }
        }
    ]


def test_preflight_probes_cloud_sql_with_admin_adapter(monkeypatch) -> None:
    service = _FakeCloudSqlAdminService(instances=[{"name": "sql-1"}])

    monkeypatch.setattr(
        preflight,
        "build_cloud_sql_admin_service",
        lambda _auth, timeout_seconds: service,
    )

    preflight._probe_cloud_sql_api(_auth_bundle(), "example-dev-project", 17)

    assert service.instances().list_calls == [{"project": "example-dev-project"}]


def test_preflight_services_api_uses_cloud_sql_admin_adapter(monkeypatch) -> None:
    config = _default_config()
    service = _FakeCloudSqlAdminService(instances=[{"name": "sql-1"}])

    monkeypatch.setattr(
        preflight,
        "build_cloud_sql_admin_service",
        lambda _auth, timeout_seconds: service,
    )

    status = preflight._probe_services_api(config, _auth_bundle(), "example-dev-project", 17)

    assert status == Status.OK
    assert service.instances().list_calls == [{"project": "example-dev-project"}]


def test_preflight_services_api_uses_compute_client_for_instances(monkeypatch) -> None:
    config = _default_config()
    config.services.cloud_sql = False
    config.discovery.auto_discover_compute_instances = True
    client = _FakeComputeAggregatedListClient(items=[("zones/us-central1-a", {})])

    def unexpected_build_service(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("discovery fallback should not be called")

    monkeypatch.setattr(preflight, "build_compute_instances_client", lambda _auth: client)
    monkeypatch.setattr(preflight, "build_service", unexpected_build_service)

    status = preflight._probe_services_api(config, _auth_bundle(), "example-dev-project", 17)

    assert status == Status.OK
    assert client.aggregated_calls == [({"project": "example-dev-project", "max_results": 1}, 17)]


def test_preflight_services_api_uses_compute_client_for_load_balancers(monkeypatch) -> None:
    config = _default_config()
    config.services.cloud_sql = False
    config.discovery.auto_discover_load_balancers = True
    client = _FakeComputeAggregatedListClient(items=[("global", {})])

    def unexpected_build_service(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("discovery fallback should not be called")

    monkeypatch.setattr(preflight, "build_compute_backend_services_client", lambda _auth: client)
    monkeypatch.setattr(preflight, "build_service", unexpected_build_service)

    status = preflight._probe_services_api(config, _auth_bundle(), "example-dev-project", 17)

    assert status == Status.OK
    assert client.aggregated_calls == [({"project": "example-dev-project", "max_results": 1}, 17)]


def test_preflight_services_api_uses_redis_client(monkeypatch) -> None:
    config = _default_config()
    config.services.cloud_sql = False
    config.services.redis = True
    client = _FakeRedisClient(instances=[{"name": "client-redis"}])

    def unexpected_build_service(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("discovery fallback should not be called")

    monkeypatch.setattr(preflight, "build_cloud_redis_client", lambda _auth: client)
    monkeypatch.setattr(preflight, "build_service", unexpected_build_service)

    status = preflight._probe_services_api(config, _auth_bundle(), "example-dev-project", 17)

    assert status == Status.OK
    assert client.calls == [("projects/example-dev-project/locations/-", 17)]


def test_preflight_services_api_uses_managed_kafka_client(monkeypatch) -> None:
    config = _default_config()
    config.services.cloud_sql = False
    config.services.managed_kafka = True
    client = _FakeManagedKafkaClient(clusters=[{"name": "client-kafka"}])

    def unexpected_build_service(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("discovery fallback should not be called")

    monkeypatch.setattr(preflight, "build_managed_kafka_client", lambda _auth: client)
    monkeypatch.setattr(preflight, "build_service", unexpected_build_service)

    status = preflight._probe_services_api(config, _auth_bundle(), "example-dev-project", 17)

    assert status == Status.OK
    assert client.calls == [("projects/example-dev-project/locations/-", 17)]


def test_gke_inventory_collect_offline(monkeypatch) -> None:
    config = _default_config()
    request_name = _cluster_request_name(config.clusters[0])
    payloads = {
        request_name: {
            "endpoint": "10.0.0.5",
            "status": "RUNNING",
            "currentMasterVersion": "1.30.8-gke.100",
            "releaseChannel": {"channel": "REGULAR"},
            "privateClusterConfig": {"enablePrivateNodes": True},
            "nodePools": [
                {
                    "name": "primary",
                    "version": "1.30.8-gke.100",
                    "initialNodeCount": 3,
                    "autoscaling": {"enabled": True, "minNodeCount": 1, "maxNodeCount": 5},
                }
            ],
        }
    }

    monkeypatch.setattr(gke_inventory, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(
        gke_inventory, "build_service", lambda *_args, **_kwargs: _FakeContainerService(payloads)
    )

    result = gke_inventory.collect(config=config)

    assert result.status == Status.OK
    cluster = result.details["clusters"][0]
    assert cluster["name"] == "dev-cluster"
    assert cluster["node_pool_count"] == 1
    assert cluster["release_channel"] == "REGULAR"


def test_gke_inventory_gets_cluster_with_cluster_manager_client(monkeypatch) -> None:
    config = _default_config()
    request_name = _cluster_request_name(config.clusters[0])
    service = _FakeContainerService(
        {
            request_name: {
                "endpoint": "10.0.0.5",
                "status": "STOPPED",
                "nodePools": [],
            }
        }
    )
    client = _FakeClusterManagerGetClient(
        payload={
            "endpoint": "10.0.0.6",
            "status": "RUNNING",
            "currentMasterVersion": "1.30.8-gke.100",
            "releaseChannel": {"channel": "REGULAR"},
            "privateClusterConfig": {"enablePrivateNodes": True},
            "nodePools": [
                {
                    "name": "primary",
                    "version": "1.30.8-gke.100",
                    "initialNodeCount": 3,
                    "autoscaling": {"enabled": True, "minNodeCount": 1, "maxNodeCount": 5},
                }
            ],
        }
    )

    monkeypatch.setattr(gke_inventory, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(gke_inventory, "build_service", lambda *_args, **_kwargs: service)
    monkeypatch.setattr(gke_inventory, "build_cluster_manager_client", lambda _auth: client)

    result = gke_inventory.collect(config=config, timeout_seconds=17)

    assert result.status == Status.OK
    cluster = result.details["clusters"][0]
    assert cluster["endpoint"] == "10.0.0.6"
    assert cluster["node_pool_count"] == 1
    assert client.get_calls == [(request_name, 17)]
    assert service.clusters().get_calls == []


def test_gke_inventory_falls_back_to_discovery_when_cluster_manager_client_fails(
    monkeypatch,
) -> None:
    config = _default_config()
    request_name = _cluster_request_name(config.clusters[0])
    service = _FakeContainerService(
        {
            request_name: {
                "endpoint": "10.0.0.5",
                "status": "RUNNING",
                "currentMasterVersion": "1.30.8-gke.100",
                "releaseChannel": {"channel": "REGULAR"},
                "privateClusterConfig": {"enablePrivateNodes": True},
                "nodePools": [],
            }
        }
    )
    client = _FakeClusterManagerGetClient(error=RuntimeError("client unavailable"))

    monkeypatch.setattr(gke_inventory, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(gke_inventory, "build_service", lambda *_args, **_kwargs: service)
    monkeypatch.setattr(gke_inventory, "build_cluster_manager_client", lambda _auth: client)

    result = gke_inventory.collect(config=config, timeout_seconds=17)

    assert result.status == Status.OK
    cluster = result.details["clusters"][0]
    assert cluster["endpoint"] == "10.0.0.5"
    assert client.get_calls == [(request_name, 17)]
    assert service.clusters().get_calls == [request_name]


def test_monitoring_collect_uses_alert_history(monkeypatch) -> None:
    config = _default_config()
    monkeypatch.setattr(monitoring, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(monitoring, "build_service", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        monitoring,
        "_list_alert_policies",
        lambda _service, _project, **_kwargs: [
            {
                "name": "projects/p/alertPolicies/p1",
                "displayName": "CPU High",
                "enabled": True,
                "notificationChannels": ["projects/p/notificationChannels/n1"],
            }
        ],
    )
    monkeypatch.setattr(
        monitoring,
        "_list_notification_channels",
        lambda _service, _project, **_kwargs: [
            {
                "name": "projects/p/notificationChannels/n1",
                "displayName": "Email",
                "enabled": True,
                "verificationStatus": "VERIFIED",
                "type": "email",
            }
        ],
    )
    monkeypatch.setattr(
        monitoring,
        "_list_alerts",
        lambda _service, _project, limit: (
            [
                {
                    "name": "projects/p/alerts/a1",
                    "state": "OPEN",
                    "openTime": "2026-05-14T00:00:00Z",
                    "policy": {"displayName": "CPU High"},
                }
            ],
            True,
            "",
        ),
    )

    result = monitoring.collect(config=config)

    assert result.status == Status.WARNING
    project = result.details["projects"][0]
    assert project["alert_policy_total"] == 1
    assert project["alerts"]["open_alerts"] == 1
    assert project["notification_channels"]["missing_channels"] == 0


def test_monitoring_zero_policies_ok_when_not_required(monkeypatch) -> None:
    config = _default_config()

    monkeypatch.setattr(monitoring, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(monitoring, "build_service", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        monitoring,
        "_list_alert_policies",
        lambda _service, _project, **_kwargs: [],
    )
    monkeypatch.setattr(
        monitoring,
        "_list_notification_channels",
        lambda _service, _project, **_kwargs: [],
    )
    monkeypatch.setattr(
        monitoring, "_list_alerts", lambda _service, _project, limit: ([], True, "")
    )

    result = monitoring.collect(config=config)

    assert result.status == Status.OK
    project = result.details["projects"][0]
    assert project["alert_policy_total"] == 0
    assert project["status"] == Status.OK.value


def test_monitoring_output_excludes_policy_expectation_fields(monkeypatch) -> None:
    config = _default_config()

    monkeypatch.setattr(monitoring, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(monitoring, "build_service", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        monitoring,
        "_list_alert_policies",
        lambda _service, _project, **_kwargs: [],
    )
    monkeypatch.setattr(
        monitoring,
        "_list_notification_channels",
        lambda _service, _project, **_kwargs: [],
    )
    monkeypatch.setattr(
        monitoring,
        "_list_alerts",
        lambda _service, _project, limit: ([], True, ""),
    )

    result = monitoring.collect(config=config)

    assert result.status == Status.OK
    project = result.details["projects"][0]
    assert "alert_policy_required" not in project
    assert project["alert_policy_total"] == 0
    assert project["status"] == Status.OK.value


def test_monitoring_collect_keeps_policy_summary_when_alert_history_unavailable(
    monkeypatch,
) -> None:
    config = _default_config()
    built_services: list[str] = []
    client = _FakeMonitoringResourceClient(
        alert_policies=[
            {
                "name": "projects/p/alertPolicies/p1",
                "displayName": "CPU High",
                "enabled": True,
                "notificationChannels": ["projects/p/notificationChannels/n1"],
            }
        ],
        notification_channels=[
            {
                "name": "projects/p/notificationChannels/n1",
                "displayName": "Email",
                "enabled": True,
                "verificationStatus": "VERIFIED",
                "type": "email",
            }
        ],
    )

    def build_service(
        _auth: Any,
        service_name: str,
        _version: str,
        _timeout_seconds: int,
    ) -> object:
        built_services.append(service_name)
        raise RuntimeError("discovery unavailable")

    monkeypatch.setattr(monitoring, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(monitoring, "build_service", build_service)
    monkeypatch.setattr(monitoring, "build_alert_policy_service_client", lambda _auth: client)
    monkeypatch.setattr(
        monitoring,
        "build_notification_channel_service_client",
        lambda _auth: client,
    )

    result = monitoring.collect(config=config, timeout_seconds=17)

    assert result.status == Status.OK
    project = result.details["projects"][0]
    assert project["alert_policy_total"] == 1
    assert project["notification_channels"]["missing_channels"] == 0
    assert project["alerts"]["api_available"] is False
    assert project["alerts"]["error"] == "discovery unavailable"
    assert client.alert_policy_calls == [("projects/example-dev-project", 17)]
    assert client.notification_channel_calls == [("projects/example-dev-project", 17)]
    assert built_services == ["monitoring"]


def test_monitoring_lists_alert_policies_with_cloud_client() -> None:
    service = _FakeMonitoringDiscoveryService(
        alert_policies=[{"name": "projects/p/alertPolicies/fallback"}]
    )
    client = _FakeMonitoringResourceClient(
        alert_policies=[
            {
                "name": "projects/p/alertPolicies/p1",
                "displayName": "CPU High",
                "enabled": True,
            }
        ]
    )

    policies = monitoring._list_alert_policies(
        service,
        "example-project",
        alert_policy_client=client,
        timeout_seconds=17,
    )

    assert policies == [
        {
            "name": "projects/p/alertPolicies/p1",
            "displayName": "CPU High",
            "enabled": True,
        }
    ]
    assert client.alert_policy_calls == [("projects/example-project", 17)]


def test_monitoring_lists_notification_channels_with_cloud_client() -> None:
    service = _FakeMonitoringDiscoveryService(
        notification_channels=[{"name": "projects/p/notificationChannels/fallback"}]
    )
    client = _FakeMonitoringResourceClient(
        notification_channels=[
            {
                "name": "projects/p/notificationChannels/n1",
                "displayName": "Email",
                "enabled": True,
                "verificationStatus": "VERIFIED",
                "type": "email",
            }
        ]
    )

    channels = monitoring._list_notification_channels(
        service,
        "example-project",
        notification_channel_client=client,
        timeout_seconds=17,
    )

    assert channels == [
        {
            "name": "projects/p/notificationChannels/n1",
            "displayName": "Email",
            "enabled": True,
            "verificationStatus": "VERIFIED",
            "type": "email",
        }
    ]
    assert client.notification_channel_calls == [("projects/example-project", 17)]


def test_monitoring_falls_back_to_discovery_when_cloud_client_fails() -> None:
    service = _FakeMonitoringDiscoveryService(
        alert_policies=[
            {
                "name": "projects/p/alertPolicies/fallback",
                "displayName": "Fallback Policy",
                "enabled": True,
            }
        ]
    )
    client = _FakeMonitoringResourceClient(error=RuntimeError("client unavailable"))

    policies = monitoring._list_alert_policies(
        service,
        "example-project",
        alert_policy_client=client,
        timeout_seconds=17,
    )

    assert policies == [
        {
            "name": "projects/p/alertPolicies/fallback",
            "displayName": "Fallback Policy",
            "enabled": True,
        }
    ]
    assert client.alert_policy_calls == [("projects/example-project", 17)]


class _FakePrometheusClient:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.range_queries: list[str] = []

    def status_buildinfo(self) -> dict[str, Any]:
        return {"version": "3.7.2", "revision": "abc123"}

    def query(self, query: str) -> list[dict[str, Any]]:
        self.queries.append(query)
        if "kube_horizontalpodautoscaler_info" in query:
            return [
                {
                    "metric": {"cluster": "dev-cluster", "environment": "dev"},
                    "value": [1779890957.0, "4"],
                }
            ]
        if "max_over_time" in query and "ScalingLimited" in query:
            return [
                {
                    "metric": {"cluster": "dev-cluster", "environment": "dev"},
                    "value": [1779890957.0, "3"],
                }
            ]
        if "max_over_time" in query and "ScalingActive" in query:
            return [
                {
                    "metric": {
                        "cluster": "dev-cluster",
                        "environment": "dev",
                        "condition": "ScalingActive",
                        "status": "false",
                    },
                    "value": [1779890957.0, "2"],
                }
            ]
        if "up" in query and "== 0" in query:
            return [
                {
                    "metric": {
                        "cluster": "dev-cluster",
                        "environment": "dev",
                        "job": "coredns",
                    },
                    "value": [1779890957.0, "1"],
                }
            ]
        if "count by (cluster, environment) (up" in query:
            return [
                {
                    "metric": {"cluster": "dev-cluster", "environment": "dev"},
                    "value": [1779890957.0, "12"],
                }
            ]
        if "condition=~" in query and "status=~" in query:
            return []
        if "condition=~" in query:
            return [
                {
                    "metric": {
                        "cluster": "dev-cluster",
                        "environment": "dev",
                        "condition": "ScalingLimited",
                        "status": "true",
                    },
                    "value": [1779890957.0, "1"],
                }
            ]
        return []

    def query_range(
        self,
        query: str,
        *,
        start: float,
        end: float,
        step_seconds: int,
    ) -> list[dict[str, Any]]:
        self.range_queries.append(query)
        _ = start, end, step_seconds
        if "node_cpu_seconds_total" in query:
            return [
                {
                    "metric": {"cluster": "dev-cluster"},
                    "values": [[1779880000.0, "12.5"], [1779890000.0, "18.25"]],
                }
            ]
        if "node_memory_MemTotal_bytes" in query:
            return [
                {
                    "metric": {"cluster": "dev-cluster"},
                    "values": [[1779880000.0, "61.0"], [1779890000.0, "66.5"]],
                }
            ]
        if "kube_horizontalpodautoscaler_info" in query:
            return [
                {
                    "metric": {"cluster": "dev-cluster", "environment": "dev"},
                    "values": [[1779880000.0, "4"], [1779890000.0, "4"]],
                }
            ]
        if "up" in query and "== 0" in query:
            return [
                {
                    "metric": {"cluster": "dev-cluster", "environment": "dev"},
                    "values": [[1779880000.0, "0"], [1779890000.0, "1"]],
                }
            ]
        return []


def test_prometheus_monitoring_collects_scoped_hpa_and_target_evidence(monkeypatch) -> None:
    config = _default_config()
    config.prometheus = PrometheusConfig(
        url="https://prometheus.example",
        labels={"environment": ["dev"], "cluster": ["dev-cluster"]},
    )
    fake = _FakePrometheusClient()
    monkeypatch.setattr(
        prometheus_monitoring,
        "_client_from_config",
        lambda _config, _timeout_seconds: fake,
    )

    result = prometheus_monitoring.collect(config=config)

    assert result.status == Status.WARNING
    assert result.details["build"]["version"] == "3.7.2"
    assert result.details["scope"]["labels"] == {
        "environment": ["dev"],
        "cluster": ["dev-cluster"],
    }
    cluster = result.details["clusters"][0]
    assert cluster["cluster"] == "dev-cluster"
    assert cluster["hpa_total"] == 4
    assert cluster["hpa_failure_condition_series_7d"] == 2
    assert cluster["hpa_scaling_limited_7d"] == 3
    assert cluster["target_total"] == 12
    assert cluster["target_down"] == 1
    assert cluster["down_jobs"] == [{"job": "coredns", "down_targets": 1}]
    assert all('environment="dev"' in query for query in fake.queries)
    assert all('cluster="dev-cluster"' in query for query in fake.queries)
    assert any("node_cpu_seconds_total" in query for query in fake.range_queries)
    assert all('cluster="dev-cluster"' in query for query in fake.range_queries)
    time_series = {
        str(block["name"]): block for block in result.details["time_series"] if block["series"]
    }
    assert time_series["cluster_cpu_utilization_percent"]["series"][0]["label"] == (
        "dev/dev-cluster"
    )
    assert time_series["cluster_cpu_utilization_percent"]["series"][0]["values"] == [
        [1779880000.0, 12.5],
        [1779890000.0, 18.25],
    ]
    assert time_series["hpa_total"]["series"][0]["values"][-1] == [1779890000.0, 4.0]


def test_prometheus_hpa_signals_can_be_informational(monkeypatch) -> None:
    class _HpaOnlyPrometheusClient(_FakePrometheusClient):
        def query(self, query: str) -> list[dict[str, Any]]:
            if "up" in query and "== 0" in query:
                self.queries.append(query)
                return []
            return super().query(query)

    config = _default_config()
    config.report_expectations.autoscaling.workload_hpa.status = "informational"
    config.report_expectations.autoscaling.platform_hpa.status = "informational"
    config.prometheus = PrometheusConfig(
        url="https://prometheus.example",
        labels={"environment": ["dev"], "cluster": ["dev-cluster"]},
    )
    fake = _HpaOnlyPrometheusClient()
    monkeypatch.setattr(
        prometheus_monitoring,
        "_client_from_config",
        lambda _config, _timeout_seconds: fake,
    )

    result = prometheus_monitoring.collect(config=config)

    assert result.status == Status.OK
    assert result.details["autoscaling_policy"]["hpa_signals_scored"] is False
    assert "informational" in result.summary


def test_prometheus_monitoring_requires_scoped_url() -> None:
    config = _default_config()
    config.clusters = []

    result = prometheus_monitoring.collect(config=config)

    assert result.status == Status.SKIPPED_CONFIG
    assert "prometheus.url is required" in result.errors[0]


def test_prometheus_monitoring_rejects_credentials_in_url() -> None:
    with pytest.raises(prometheus_monitoring.PrometheusConfigError, match="must not include"):
        prometheus_monitoring._normalize_base_url("https://user:pass@prometheus.example")


def test_prometheus_monitoring_rejects_url_query_or_fragment() -> None:
    with pytest.raises(prometheus_monitoring.PrometheusConfigError, match="query strings"):
        prometheus_monitoring._normalize_base_url("https://prometheus.example?token=secret")


def test_prometheus_client_uses_httpx_transport(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_get(
        url: str,
        headers: dict[str, str],
        timeout: int,
        follow_redirects: bool,
    ) -> httpx.Response:
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout"] = timeout
        captured["follow_redirects"] = follow_redirects
        return httpx.Response(
            200,
            json={"status": "success", "data": {"result": []}},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(prometheus_monitoring.httpx, "get", fake_get)

    client = prometheus_monitoring.PrometheusClient(
        base_url="https://prometheus.example",
        timeout_seconds=5,
        token="token-1",
    )

    assert client.query("up") == []
    assert captured["url"] == "https://prometheus.example/api/v1/query?query=up"
    assert captured["headers"]["Authorization"] == "Bearer token-1"
    assert captured["timeout"] == 5
    assert captured["follow_redirects"] is True


def test_logging_collect_pubsub_pipeline(monkeypatch) -> None:
    config = _default_config()
    pubsub_metric_calls: list[list[str]] = []
    monkeypatch.setattr(logging_collector, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(logging_collector, "build_service", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(logging_collector, "build_metric_service_client", lambda _auth: None)
    monkeypatch.setattr(
        logging_collector,
        "_list_sinks",
        lambda _service, _project, **_kwargs: [
            {
                "name": "splunk-forwarder",
                "destination": (
                    "pubsub.googleapis.com/projects/example-dev-project/topics/splunk-topic"
                ),
                "filter": "",
            }
        ],
    )
    monkeypatch.setattr(
        logging_collector,
        "_list_buckets",
        lambda _service, _project, **_kwargs: [
            {"name": "projects/example-dev-project/locations/us-central1/buckets/app"}
        ],
    )
    monkeypatch.setattr(
        logging_collector,
        "_get_topic",
        lambda _service, _topic, **_kwargs: {"labels": {}},
    )
    monkeypatch.setattr(
        logging_collector,
        "_list_topic_subscriptions",
        lambda _service, _topic, **_kwargs: [
            "projects/example-dev-project/subscriptions/splunk-subscription"
        ],
    )
    monkeypatch.setattr(
        logging_collector,
        "_collect_logging_metrics",
        lambda **_kwargs: (
            {
                "bytes_ingested_total": 2048.0,
                "bytes_ingested_peak_hour": 1024.0,
                "bytes_stored_peak": 4096.0,
                "bytes_stored_metric_status": "available",
                "bucket_ingestion": [
                    {
                        "bucket": "app",
                        "location": "us-central1",
                        "bytes_ingested_total": 2048.0,
                        "bytes_ingested_peak_hour": 1024.0,
                        "series_count": 1,
                    }
                ],
                "bucket_storage": [
                    {
                        "bucket": "app",
                        "location": "us-central1",
                        "data_type": "CHARGED",
                        "bytes_stored_peak": 4096.0,
                    }
                ],
            },
            "",
        ),
    )

    def _collect_pubsub_metrics(**kwargs: Any) -> tuple[list[dict[str, Any]], str]:
        pubsub_metric_calls.append(kwargs["subscriptions"])
        return (
            [
                {
                    "subscription": (
                        "projects/example-dev-project/subscriptions/splunk-subscription"
                    ),
                    "subscription_id": "splunk-subscription",
                    "num_unacked_messages_peak": 2.0,
                    "oldest_unacked_message_age_peak_seconds": 10.0,
                    "delivery_latency_health_score_min": 1.0,
                    "dead_letter_message_count_total": 0.0,
                }
            ],
            "",
        )

    monkeypatch.setattr(logging_collector, "_collect_pubsub_metrics", _collect_pubsub_metrics)

    result = logging_collector.collect(config=config)

    assert result.status == Status.OK
    project = result.details["projects"][0]
    assert project["pubsub_topic_count"] == 1
    assert project["pubsub_subscription_total"] == 1
    assert project["pubsub_metric_subscriptions_checked"] == 1
    assert project["pubsub_metric_subscription_sample_limit"] == 10
    assert project["pubsub_metric_subscriptions_sampled"] is False
    assert project["splunk_hints"]["matched"] is True
    assert project["buckets"][0]["location"] == "us-central1"
    assert project["logging_metrics"]["bytes_ingested_total"] == 2048.0
    assert project["logging_metrics"]["bucket_storage"][0]["bucket"] == "app"
    assert project["pubsub_metrics"][0]["subscription_id"] == "splunk-subscription"
    assert pubsub_metric_calls == [
        ["projects/example-dev-project/subscriptions/splunk-subscription"]
    ]


def test_logging_collect_labels_pubsub_metric_sample(monkeypatch) -> None:
    config = _default_config()
    subscriptions = [
        f"projects/example-dev-project/subscriptions/splunk-subscription-{index}"
        for index in range(12)
    ]
    pubsub_metric_calls: list[list[str]] = []
    monkeypatch.setattr(logging_collector, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(logging_collector, "build_service", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(logging_collector, "build_metric_service_client", lambda _auth: None)
    monkeypatch.setattr(
        logging_collector,
        "_list_sinks",
        lambda _service, _project, **_kwargs: [
            {
                "name": "splunk-forwarder",
                "destination": (
                    "pubsub.googleapis.com/projects/example-dev-project/topics/splunk-topic"
                ),
                "filter": "",
            }
        ],
    )
    monkeypatch.setattr(
        logging_collector,
        "_list_buckets",
        lambda _service, _project, **_kwargs: [
            {"name": "projects/example-dev-project/locations/us-central1/buckets/app"}
        ],
    )
    monkeypatch.setattr(
        logging_collector,
        "_get_topic",
        lambda _service, _topic, **_kwargs: {"labels": {}},
    )
    monkeypatch.setattr(
        logging_collector,
        "_list_topic_subscriptions",
        lambda _service, _topic, **_kwargs: subscriptions,
    )
    monkeypatch.setattr(logging_collector, "_collect_logging_metrics", lambda **_kwargs: ({}, ""))

    def _collect_pubsub_metrics(**kwargs: Any) -> tuple[list[dict[str, Any]], str]:
        pubsub_metric_calls.append(kwargs["subscriptions"])
        return [], ""

    monkeypatch.setattr(logging_collector, "_collect_pubsub_metrics", _collect_pubsub_metrics)

    result = logging_collector.collect(config=config)

    assert result.status == Status.OK
    project = result.details["projects"][0]
    assert project["pubsub_subscription_total"] == 12
    assert project["pubsub_metric_subscriptions_checked"] == 10
    assert project["pubsub_metric_subscription_sample_limit"] == 10
    assert project["pubsub_metric_subscriptions_sampled"] is True
    assert project["pubsub_topics"][0]["sample_subscriptions"] == subscriptions[:10]
    assert pubsub_metric_calls == [subscriptions[:10]]


def test_logging_collect_uses_cloud_clients_without_discovery_init(monkeypatch) -> None:
    config = _default_config()
    built_services: list[str] = []
    config_client = _FakeLoggingConfigClient(
        sinks=[
            {
                "name": "projects/example-dev-project/sinks/splunk-forwarder",
                "destination": (
                    "pubsub.googleapis.com/projects/example-dev-project/topics/splunk-topic"
                ),
                "filter": "",
                "disabled": False,
            }
        ],
        buckets=[
            {
                "name": "projects/example-dev-project/locations/us-central1/buckets/app",
                "retentionDays": 30,
                "locked": False,
            }
        ],
    )
    pubsub_client = _FakePubsubPublisherClient(
        topic={"labels": {"owner": "platform"}},
        subscriptions=["projects/example-dev-project/subscriptions/splunk-subscription"],
    )

    def build_service(
        _auth: Any,
        service_name: str,
        _version: str,
        _timeout_seconds: int,
    ) -> object:
        built_services.append(service_name)
        if service_name in {"logging", "pubsub"}:
            raise RuntimeError("discovery unavailable")
        return object()

    monkeypatch.setattr(logging_collector, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(logging_collector, "build_service", build_service)
    monkeypatch.setattr(
        logging_collector,
        "build_logging_config_service_client",
        lambda _auth: config_client,
    )
    monkeypatch.setattr(
        logging_collector,
        "build_pubsub_publisher_client",
        lambda _auth: pubsub_client,
    )
    monkeypatch.setattr(logging_collector, "build_metric_service_client", lambda _auth: None)
    monkeypatch.setattr(logging_collector, "_collect_logging_metrics", lambda **_kwargs: ({}, ""))
    monkeypatch.setattr(logging_collector, "_collect_pubsub_metrics", lambda **_kwargs: ({}, ""))

    result = logging_collector.collect(config=config, timeout_seconds=17)

    assert result.status == Status.OK
    project = result.details["projects"][0]
    assert project["sink_count"] == 1
    assert project["bucket_count"] == 1
    assert project["pubsub_subscription_total"] == 1
    assert config_client.sink_calls == [("projects/example-dev-project", 17)]
    assert config_client.bucket_calls == [("projects/example-dev-project/locations/-", 17)]
    assert pubsub_client.topic_calls == [("projects/example-dev-project/topics/splunk-topic", 17)]
    assert pubsub_client.subscription_calls == [
        ("projects/example-dev-project/topics/splunk-topic", 17)
    ]
    assert built_services == []


def test_logging_lists_sinks_with_cloud_logging_client() -> None:
    service = _FakeLoggingDiscoveryService(
        sinks=[{"name": "projects/p/sinks/fallback", "destination": "storage.googleapis.com/b"}]
    )
    client = _FakeLoggingConfigClient(
        sinks=[
            {
                "name": "projects/p/sinks/splunk-forwarder",
                "destination": "pubsub.googleapis.com/projects/p/topics/splunk-topic",
                "filter": "",
                "disabled": False,
            }
        ]
    )

    sinks = logging_collector._list_sinks(
        service,
        "example-project",
        config_client=client,
        timeout_seconds=17,
    )

    assert sinks == [
        {
            "name": "projects/p/sinks/splunk-forwarder",
            "destination": "pubsub.googleapis.com/projects/p/topics/splunk-topic",
            "filter": "",
            "disabled": False,
        }
    ]
    assert client.sink_calls == [("projects/example-project", 17)]


def test_logging_lists_buckets_with_cloud_logging_client() -> None:
    service = _FakeLoggingDiscoveryService(
        buckets=[{"name": "projects/p/locations/global/buckets/fallback"}]
    )
    client = _FakeLoggingConfigClient(
        buckets=[
            {
                "name": "projects/p/locations/us-central1/buckets/app",
                "retentionDays": 31,
                "locked": False,
            }
        ]
    )

    buckets = logging_collector._list_buckets(
        service,
        "example-project",
        config_client=client,
        timeout_seconds=17,
    )

    assert buckets == [
        {
            "name": "projects/p/locations/us-central1/buckets/app",
            "retentionDays": 31,
            "locked": False,
        }
    ]
    assert client.bucket_calls == [("projects/example-project/locations/-", 17)]


def test_logging_falls_back_to_discovery_when_cloud_logging_client_fails() -> None:
    service = _FakeLoggingDiscoveryService(
        sinks=[
            {
                "name": "projects/p/sinks/fallback",
                "destination": "storage.googleapis.com/fallback",
            }
        ]
    )
    client = _FakeLoggingConfigClient(error=RuntimeError("client unavailable"))

    sinks = logging_collector._list_sinks(
        service,
        "example-project",
        config_client=client,
        timeout_seconds=17,
    )

    assert sinks == [
        {
            "name": "projects/p/sinks/fallback",
            "destination": "storage.googleapis.com/fallback",
        }
    ]
    assert client.sink_calls == [("projects/example-project", 17)]


def test_logging_gets_topic_with_pubsub_client() -> None:
    service = _FakePubsubDiscoveryService(topic={"labels": {"fallback": "true"}})
    client = _FakePubsubPublisherClient(
        topic={
            "labels": {"owner": "platform"},
            "kmsKeyName": "projects/p/locations/l/keyRings/r/cryptoKeys/k",
        }
    )

    topic = logging_collector._get_topic(
        service,
        "projects/example-project/topics/splunk-topic",
        pubsub_client=client,
        timeout_seconds=17,
    )

    assert topic == {
        "labels": {"owner": "platform"},
        "kmsKeyName": "projects/p/locations/l/keyRings/r/cryptoKeys/k",
    }
    assert client.topic_calls == [("projects/example-project/topics/splunk-topic", 17)]


def test_logging_lists_topic_subscriptions_with_pubsub_client() -> None:
    service = _FakePubsubDiscoveryService(
        subscriptions=["projects/example-project/subscriptions/fallback"]
    )
    client = _FakePubsubPublisherClient(
        subscriptions=[
            "projects/example-project/subscriptions/splunk-primary",
            "projects/example-project/subscriptions/splunk-secondary",
        ]
    )

    subscriptions = logging_collector._list_topic_subscriptions(
        service,
        "projects/example-project/topics/splunk-topic",
        pubsub_client=client,
        timeout_seconds=17,
    )

    assert subscriptions == [
        "projects/example-project/subscriptions/splunk-primary",
        "projects/example-project/subscriptions/splunk-secondary",
    ]
    assert client.subscription_calls == [("projects/example-project/topics/splunk-topic", 17)]


def test_logging_falls_back_to_discovery_when_pubsub_client_fails() -> None:
    service = _FakePubsubDiscoveryService(
        topic={"labels": {"fallback": "true"}},
        subscriptions=["projects/example-project/subscriptions/fallback"],
    )
    client = _FakePubsubPublisherClient(error=RuntimeError("client unavailable"))

    topic = logging_collector._get_topic(
        service,
        "projects/example-project/topics/splunk-topic",
        pubsub_client=client,
        timeout_seconds=17,
    )
    subscriptions = logging_collector._list_topic_subscriptions(
        service,
        "projects/example-project/topics/splunk-topic",
        pubsub_client=client,
        timeout_seconds=17,
    )

    assert topic == {"labels": {"fallback": "true"}}
    assert subscriptions == ["projects/example-project/subscriptions/fallback"]
    assert client.topic_calls == [("projects/example-project/topics/splunk-topic", 17)]
    assert client.subscription_calls == [("projects/example-project/topics/splunk-topic", 17)]


def test_logging_metrics_groups_stored_volume_by_bucket() -> None:
    stored_series = [
        {
            "metric": {
                "labels": {
                    "log_bucket_id": "app",
                    "log_bucket_location": "us-central1",
                    "data_type": "CHARGED",
                }
            },
            "points": [
                {"interval": {"endTime": "2026-05-01T01:00:00Z"}, "value": {"int64Value": "10"}},
                {"interval": {"endTime": "2026-05-01T02:00:00Z"}, "value": {"int64Value": "30"}},
            ],
        },
        {
            "metric": {
                "labels": {
                    "log_bucket_id": "app",
                    "log_bucket_location": "us-central1",
                    "data_type": "CHARGED",
                }
            },
            "points": [
                {"interval": {"endTime": "2026-05-01T01:00:00Z"}, "value": {"int64Value": "5"}},
                {"interval": {"endTime": "2026-05-01T02:00:00Z"}, "value": {"int64Value": "7"}},
            ],
        },
    ]

    rows = logging_collector._bucket_storage_rows(stored_series)

    assert rows == [
        {
            "bucket": "app",
            "location": "us-central1",
            "data_type": "CHARGED",
            "bytes_stored_peak": 37.0,
        }
    ]
    assert logging_collector._peak_sum_by_timestamp(stored_series) == 37.0


def test_logging_bytes_stored_metric_status_is_not_applicable_without_extended_retention() -> None:
    buckets = [
        {
            "name": "projects/example/locations/global/buckets/_Required",
            "retentionDays": 400,
        },
        {
            "name": "projects/example/locations/global/buckets/_Default",
            "retentionDays": 30,
        },
        {
            "name": "projects/example/locations/us-central1/buckets/app",
            "retentionDays": 7,
        },
    ]

    status = logging_collector._bytes_stored_metric_status([], buckets)

    assert status == "not_applicable"


def test_logging_bytes_stored_metric_status_marks_missing_for_extended_retention() -> None:
    buckets = [
        {
            "name": "projects/example/locations/us-central1/buckets/app",
            "retentionDays": 90,
        }
    ]

    status = logging_collector._bytes_stored_metric_status([], buckets)

    assert status == "metric_not_returned"


def test_logging_metrics_groups_ingestion_volume_by_bucket() -> None:
    ingested_series = [
        {
            "metric": {
                "labels": {
                    "log_bucket_id": "app",
                    "log_bucket_location": "us-central1",
                    "resource_type": "cloudsql_database",
                    "log_source": "projects/123",
                }
            },
            "points": [
                {"interval": {"endTime": "2026-05-01T01:00:00Z"}, "value": {"int64Value": "10"}},
                {"interval": {"endTime": "2026-05-01T02:00:00Z"}, "value": {"int64Value": "30"}},
            ],
        },
        {
            "metric": {
                "labels": {
                    "log_bucket_id": "app",
                    "log_bucket_location": "us-central1",
                    "resource_type": "gke_container",
                    "log_source": "projects/123",
                }
            },
            "points": [
                {"interval": {"endTime": "2026-05-01T01:00:00Z"}, "value": {"int64Value": "5"}},
                {"interval": {"endTime": "2026-05-01T02:00:00Z"}, "value": {"int64Value": "7"}},
            ],
        },
    ]

    rows = logging_collector._bucket_ingestion_rows(ingested_series)

    assert rows == [
        {
            "bucket": "app",
            "location": "us-central1",
            "bytes_ingested_total": 52.0,
            "bytes_ingested_peak_hour": 37.0,
            "series_count": 2,
        }
    ]


def test_logging_fetch_time_series_uses_metric_service_client() -> None:
    client = _FakeMetricServiceClient(
        time_series_pages=[
            [
                {
                    "resource": {"labels": {"subscription_id": "sub-a"}},
                    "points": [{"value": {"doubleValue": 1.0}}],
                }
            ],
            [
                {
                    "resource": {"labels": {"subscription_id": "sub-b"}},
                    "points": [{"value": {"doubleValue": 2.0}}],
                }
            ],
        ]
    )
    monitoring_clients = logging_collector._MonitoringClients(
        service=None,
        metric_client=client,
        timeout_seconds=17,
    )

    rows = logging_collector._fetch_time_series(
        monitoring=monitoring_clients,
        project="example-dev-project",
        metric_type="pubsub.googleapis.com/subscription/num_unacked_messages_by_region",
        aligner="ALIGN_MAX",
        alignment_period="300s",
        reducer="REDUCE_SUM",
        group_by_fields=["resource.labels.subscription_id"],
        page_size=50,
        max_pages=1,
        max_series=10,
    )

    request, timeout = client.time_series_calls[0]
    assert timeout == 17
    assert len(rows) == 1
    assert request.name == "projects/example-dev-project"
    assert (
        request.filter
        == 'metric.type="pubsub.googleapis.com/subscription/num_unacked_messages_by_region"'
    )
    assert request.page_size == 50
    assert request.aggregation.alignment_period.seconds == 300
    assert list(request.aggregation.group_by_fields) == ["resource.labels.subscription_id"]


def test_logging_fetch_time_series_resolves_lazy_discovery_fallback() -> None:
    service_calls: list[str] = []
    list_calls: list[dict[str, Any]] = []

    class _FakeTimeSeriesApi:
        def list(self, **kwargs: Any) -> _FakeRequest:
            list_calls.append(dict(kwargs))
            return _FakeRequest(
                {
                    "timeSeries": [
                        {
                            "resource": {"labels": {"subscription_id": "sub-a"}},
                            "points": [{"value": {"doubleValue": 1.0}}],
                        }
                    ]
                }
            )

    class _FakeMonitoringService:
        def projects(self) -> _FakeMonitoringService:
            return self

        def timeSeries(self) -> _FakeTimeSeriesApi:  # noqa: N802
            return _FakeTimeSeriesApi()

    def service_factory() -> _FakeMonitoringService:
        service_calls.append("monitoring")
        return _FakeMonitoringService()

    monitoring_clients = logging_collector._MonitoringClients(
        service=service_factory,
        metric_client=None,
        timeout_seconds=17,
    )

    rows = logging_collector._fetch_time_series(
        monitoring=monitoring_clients,
        project="example-dev-project",
        metric_type="logging.googleapis.com/billing/bytes_ingested",
        aligner="ALIGN_SUM",
        alignment_period="3600s",
        page_size=50,
        max_pages=1,
        max_series=10,
    )

    assert service_calls == ["monitoring"]
    assert len(rows) == 1
    assert len(list_calls) == 1
    assert list_calls[0]["name"] == "projects/example-dev-project"
    assert list_calls[0]["filter"] == 'metric.type="logging.googleapis.com/billing/bytes_ingested"'
    assert list_calls[0]["view"] == "FULL"
    assert list_calls[0]["pageSize"] == 50
    assert list_calls[0]["aggregation_alignmentPeriod"] == "3600s"
    assert list_calls[0]["aggregation_perSeriesAligner"] == "ALIGN_SUM"
    assert "interval_startTime" in list_calls[0]
    assert "interval_endTime" in list_calls[0]


def test_collectors_pass_configured_timeout_to_gcp_clients() -> None:
    collectors_dir = Path(__file__).resolve().parents[1] / "src" / "opsbrief" / "collectors"
    missing_timeout: list[str] = []
    for path in sorted(collectors_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name) or node.func.id != "build_service":
                continue
            has_timeout = len(node.args) >= 4 or any(
                keyword.arg == "timeout_seconds" for keyword in node.keywords
            )
            if not has_timeout:
                missing_timeout.append(f"{path.name}:{node.lineno}")

    assert missing_timeout == []


def test_gcp_api_lists_cloud_sql_instances_with_admin_discovery_service() -> None:
    service = _FakeCloudSqlAdminService(
        instances=[
            {
                "name": "sql-1",
                "state": "RUNNABLE",
                "databaseVersion": "POSTGRES_15",
            }
        ]
    )

    rows = list_cloud_sql_instances(service, "example-dev-project")

    assert rows == [
        {
            "name": "sql-1",
            "state": "RUNNABLE",
            "databaseVersion": "POSTGRES_15",
        }
    ]
    assert service.instances().list_calls == [{"project": "example-dev-project"}]


def test_gcp_api_lists_cloud_sql_backup_runs_with_admin_discovery_service() -> None:
    service = _FakeCloudSqlAdminService(
        backup_runs=[
            {
                "id": "backup-1",
                "status": "SUCCESSFUL",
                "type": "AUTOMATED",
            }
        ]
    )

    rows = list_cloud_sql_backup_runs(
        service,
        project="example-dev-project",
        instance="sql-1",
        max_results=0,
    )

    assert rows == [
        {
            "id": "backup-1",
            "status": "SUCCESSFUL",
            "type": "AUTOMATED",
        }
    ]
    assert service.backupRuns().list_calls == [
        {"project": "example-dev-project", "instance": "sql-1", "maxResults": 1}
    ]


def test_backup_lists_gke_backup_plans_with_cloud_client() -> None:
    service = _FakeGkeBackupDiscoveryService(
        [
            {
                "name": "projects/p/locations/us-central1/backupPlans/fallback-plan",
                "state": "DELETING",
            }
        ]
    )
    client = _FakeGkeBackupClient(
        plans=[
            {
                "name": "projects/p/locations/us-central1/backupPlans/client-plan",
                "state": "ACTIVE",
            }
        ]
    )

    plans = backup._list_gke_backup_plans(
        service,
        "projects/example-dev-project/locations/us-central1",
        backup_client=client,
        timeout_seconds=17,
    )

    assert plans == [
        {
            "name": "projects/p/locations/us-central1/backupPlans/client-plan",
            "state": "ACTIVE",
        }
    ]
    assert client.calls == [("projects/example-dev-project/locations/us-central1", 17)]
    assert service.list_calls == []


def test_backup_falls_back_to_discovery_when_gke_backup_client_fails() -> None:
    service = _FakeGkeBackupDiscoveryService(
        [
            {
                "name": "projects/p/locations/us-central1/backupPlans/fallback-plan",
                "state": "ACTIVE",
            }
        ]
    )
    client = _FakeGkeBackupClient(error=RuntimeError("client unavailable"))

    plans = backup._list_gke_backup_plans(
        service,
        "projects/example-dev-project/locations/us-central1",
        backup_client=client,
        timeout_seconds=17,
    )

    assert plans == [
        {
            "name": "projects/p/locations/us-central1/backupPlans/fallback-plan",
            "state": "ACTIVE",
        }
    ]
    assert client.calls == [("projects/example-dev-project/locations/us-central1", 17)]
    assert service.list_calls == [
        {"parent": "projects/example-dev-project/locations/us-central1", "pageSize": 100}
    ]


def test_preflight_backup_api_uses_gke_backup_client(monkeypatch) -> None:
    config = _default_config()
    client = _FakeGkeBackupClient(plans=[{"name": "client-plan"}])
    checks: list[dict[str, str]] = []
    errors: list[str] = []

    def unexpected_build_service(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("discovery fallback should not be called")

    monkeypatch.setattr(preflight, "build_gke_backup_client", lambda _auth: client)
    monkeypatch.setattr(preflight, "build_service", unexpected_build_service)

    status = preflight._probe_api_for_collector(
        config,
        "backup",
        _auth_bundle(),
        checks,
        errors,
        17,
    )

    assert status == Status.OK
    assert checks == [{"name": "api:backup", "status": "ok", "message": "reachable"}]
    assert errors == []
    assert client.calls == [("projects/example-dev-project/locations/us-central1", 17)]


def test_preflight_backup_api_falls_back_to_discovery_when_gke_backup_client_fails(
    monkeypatch,
) -> None:
    config = _default_config()
    client = _FakeGkeBackupClient(error=RuntimeError("client unavailable"))
    service = _FakeGkeBackupDiscoveryService([{"name": "fallback-plan"}])
    checks: list[dict[str, str]] = []
    errors: list[str] = []

    monkeypatch.setattr(preflight, "build_gke_backup_client", lambda _auth: client)
    monkeypatch.setattr(preflight, "build_service", lambda *_args, **_kwargs: service)

    status = preflight._probe_api_for_collector(
        config,
        "backup",
        _auth_bundle(),
        checks,
        errors,
        17,
    )

    assert status == Status.OK
    assert checks == [{"name": "api:backup", "status": "ok", "message": "reachable"}]
    assert errors == []
    assert client.calls == [("projects/example-dev-project/locations/us-central1", 17)]
    assert service.list_calls == [
        {"parent": "projects/example-dev-project/locations/us-central1", "pageSize": 1}
    ]


def test_backup_collect_avoids_gke_backup_discovery_when_client_succeeds(monkeypatch) -> None:
    config = _default_config()
    client = _FakeGkeBackupClient(plans=[{"name": "client-plan", "state": "ACTIVE"}])
    built_services: list[str] = []

    def build_service(
        _auth: object, service_name: str, _version: str, _timeout_seconds: int
    ) -> object:
        built_services.append(service_name)
        if service_name == "gkebackup":
            raise AssertionError("GKE Backup discovery fallback should not be called")
        return object()

    monkeypatch.setattr(backup, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(backup, "build_service", build_service)
    monkeypatch.setattr(backup, "build_gke_backup_client", lambda _auth: client)
    monkeypatch.setattr(backup, "build_cloud_sql_admin_service", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        backup,
        "_list_sql_instances",
        lambda _service, _project: [
            {
                "name": "sql-1",
                "state": "RUNNABLE",
                "databaseVersion": "POSTGRES_15",
                "settings": {"backupConfiguration": {"enabled": True}},
            }
        ],
    )
    monkeypatch.setattr(backup, "_latest_sql_backup_run", lambda _service, _p, _i: None)

    result = backup.collect(config=config, timeout_seconds=17)

    assert result.status == Status.OK
    assert built_services == ["container"]
    assert client.calls == [("projects/example-dev-project/locations/us-central1", 17)]
    assert result.details["gke_backup"][0]["plan_count"] == 1


def test_backup_collect_reports_expected_reference_without_forcing_warning(monkeypatch) -> None:
    config = _default_config()
    monkeypatch.setattr(backup, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(backup, "build_service", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(backup, "build_cloud_sql_admin_service", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(backup, "_list_gke_backup_plans", lambda _service, _parent, **_kwargs: [])
    monkeypatch.setattr(
        backup,
        "_list_sql_instances",
        lambda _service, _project: [
            {
                "name": "sql-1",
                "state": "RUNNABLE",
                "databaseVersion": "POSTGRES_15",
                "settings": {"backupConfiguration": {"enabled": False}},
            }
        ],
    )
    monkeypatch.setattr(backup, "_latest_sql_backup_run", lambda _service, _p, _i: None)

    result = backup.collect(config=config)

    assert result.status == Status.WARNING
    assert "GKE backup plans=0" in result.summary
    assert "expected_without_detection=2" in result.summary


def test_backup_out_of_scope_policy_skips_backup_assessment(monkeypatch) -> None:
    config = _default_config()
    config.report_expectations.backup_policy.status = "out_of_scope"
    config.report_expectations.backup_policy.reason = "Backup policy is out of scope for dev."
    monkeypatch.setattr(
        backup,
        "build_service",
        lambda *_args, **_kwargs: pytest.fail("backup APIs should not be initialized"),
    )

    result = backup.collect(config=config)

    assert result.status == Status.OK
    assert result.details["policy"]["status"] == "out_of_scope"
    assert result.details["gke_backup"][0]["status"] == Status.SKIPPED_CONFIG.value
    assert "out of scope" in result.summary


def test_backup_uses_default_region_when_clusters_not_configured(monkeypatch) -> None:
    config = _default_config()
    config.clusters = []

    visited_parents: list[str] = []

    monkeypatch.setattr(backup, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(backup, "build_service", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(backup, "build_cloud_sql_admin_service", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        backup,
        "resolve_clusters",
        lambda _config, _container_service: [],
    )
    monkeypatch.setattr(
        backup,
        "_list_gke_backup_plans",
        lambda _service, parent, **_kwargs: visited_parents.append(parent) or [],
    )
    monkeypatch.setattr(backup, "_list_sql_instances", lambda _service, _project: [])
    monkeypatch.setattr(backup, "_latest_sql_backup_run", lambda _service, _p, _i: None)

    result = backup.collect(config=config)

    assert result.status == Status.OK
    assert visited_parents == []
    assert result.details["gke_backup"][0]["status"] == Status.SKIPPED_CONFIG.value


def test_backup_elasticsearch_unreachable_maps_to_skipped_network(monkeypatch) -> None:
    config = _default_config()
    config.clusters = []
    config.services.elasticsearch_backup_checks = [
        {
            "name": "dev-elasticsearch",
            "snapshot_url": "https://elasticsearch.example.com/_snapshot/_all",
            "ilm_url": "https://elasticsearch.example.com/_ilm/policy",
        }
    ]
    monkeypatch.setattr(backup, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(backup, "build_service", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(backup, "build_cloud_sql_admin_service", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(backup, "resolve_clusters", lambda _config, _service: [])
    monkeypatch.setattr(backup, "_list_gke_backup_plans", lambda _service, _parent, **_kwargs: [])
    monkeypatch.setattr(backup, "_list_sql_instances", lambda _service, _project: [])
    monkeypatch.setattr(backup, "_latest_sql_backup_run", lambda _service, _p, _i: None)
    monkeypatch.setattr(
        backup,
        "_http_json_get",
        lambda **_kwargs: (_ for _ in ()).throw(
            httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known")
        ),
    )

    result = backup.collect(config=config)

    assert result.status == Status.SKIPPED_NETWORK
    elastic_row = result.details["elasticsearch_backup"][0]
    assert elastic_row["status"] == Status.SKIPPED_NETWORK.value


def test_backup_http_json_get_uses_httpx_and_parses_response(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_get(
        url: str,
        headers: dict[str, str],
        timeout: int,
        follow_redirects: bool,
    ) -> httpx.Response:
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout"] = timeout
        captured["follow_redirects"] = follow_redirects
        return httpx.Response(503, json={"error": "unavailable"})

    monkeypatch.setattr(backup.httpx, "get", fake_get)

    status, payload = backup._http_json_get(
        url="https://elasticsearch.example.com/_snapshot/_all",
        headers={"Authorization": "Bearer token-1", "X-Retry": 1},
        timeout_seconds=0,
    )

    assert status == 503
    assert payload == {"error": "unavailable"}
    assert captured["url"] == "https://elasticsearch.example.com/_snapshot/_all"
    assert captured["headers"]["X-Retry"] == "1"
    assert captured["timeout"] == 1
    assert captured["follow_redirects"] is True


def test_services_collect_sql_only(monkeypatch) -> None:
    config = _default_config()
    monkeypatch.setattr(services, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(services, "build_service", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        services,
        "build_cloud_sql_admin_service",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        services,
        "_list_sql_instances",
        lambda _service, _project: [
            {
                "name": "sql-1",
                "state": "RUNNABLE",
                "databaseVersion": "POSTGRES_15",
                "settings": {"tier": "db-custom-2-7680", "availabilityType": "ZONAL"},
            }
        ],
    )

    result = services.collect(config=config)

    assert result.status == Status.OK
    assert result.details["cloud_sql"][0]["instance_count"] == 1
    assert result.details["redis"][0]["status"] == Status.SKIPPED_CONFIG.value
    assert result.details["managed_kafka"][0]["status"] == Status.SKIPPED_CONFIG.value


def test_services_respects_cloud_sql_disabled(monkeypatch) -> None:
    config = _default_config()
    config.services.cloud_sql = False
    config.services.redis = True
    built_services: list[str] = []

    def _build_service(
        _auth: object, service_name: str, _version: str, timeout_seconds: int
    ) -> object:
        assert timeout_seconds == 45
        built_services.append(service_name)
        return object()

    monkeypatch.setattr(services, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(services, "build_service", _build_service)
    monkeypatch.setattr(
        services,
        "_collect_redis",
        lambda _service, projects, **_kwargs: [
            {
                "project": projects[0],
                "status": Status.OK.value,
                "instance_count": 0,
                "instances": [],
            }
        ],
    )

    result = services.collect(config=config)

    assert "sqladmin" not in built_services
    assert built_services == ["redis"]
    assert result.details["cloud_sql"][0]["status"] == Status.SKIPPED_CONFIG.value
    assert result.details["cloud_sql"][0]["reason"] == "cloud_sql disabled in config"
    assert result.details["redis"][0]["status"] == Status.OK.value


def test_services_collects_redis_with_cloud_redis_client() -> None:
    service = _FakeRedisDiscoveryService(
        [
            {
                "name": "projects/p/locations/us-central1/instances/fallback-redis",
                "state": "DISABLED",
                "memorySizeGb": 2,
                "redisVersion": "REDIS_6_X",
            }
        ]
    )
    client = _FakeRedisClient(
        instances=[
            {
                "name": "projects/p/locations/us-central1/instances/client-redis",
                "state": "READY",
                "memorySizeGb": 4,
                "redisVersion": "REDIS_7_0",
            }
        ]
    )

    rows = services._collect_redis(
        service,
        ["example-dev-project"],
        redis_client=client,
        timeout_seconds=17,
    )

    assert rows == [
        {
            "project": "example-dev-project",
            "status": Status.OK.value,
            "instance_count": 1,
            "instances": [
                {
                    "name": "projects/p/locations/us-central1/instances/client-redis",
                    "state": "READY",
                    "memory_size_gb": 4,
                    "redis_version": "REDIS_7_0",
                }
            ],
        }
    ]
    assert client.calls == [("projects/example-dev-project/locations/-", 17)]
    assert service.list_calls == []


def test_services_falls_back_to_discovery_when_cloud_redis_client_fails() -> None:
    service = _FakeRedisDiscoveryService(
        [
            {
                "name": "projects/p/locations/us-central1/instances/fallback-redis",
                "state": "READY",
                "memorySizeGb": 2,
                "redisVersion": "REDIS_6_X",
            }
        ]
    )
    client = _FakeRedisClient(error=RuntimeError("client unavailable"))

    rows = services._collect_redis(
        service,
        ["example-dev-project"],
        redis_client=client,
        timeout_seconds=17,
    )

    assert rows == [
        {
            "project": "example-dev-project",
            "status": Status.OK.value,
            "instance_count": 1,
            "instances": [
                {
                    "name": "projects/p/locations/us-central1/instances/fallback-redis",
                    "state": "READY",
                    "memory_size_gb": 2,
                    "redis_version": "REDIS_6_X",
                }
            ],
        }
    ]
    assert client.calls == [("projects/example-dev-project/locations/-", 17)]
    assert service.list_calls == [
        {"parent": "projects/example-dev-project/locations/-", "pageSize": 100}
    ]


def test_services_collects_managed_kafka_with_cloud_client() -> None:
    service = _FakeManagedKafkaDiscoveryService(
        [
            {
                "name": "projects/p/locations/us-central1/clusters/fallback-kafka",
                "state": "DELETING",
                "capacityConfig": {"vcpuCount": 3},
            }
        ]
    )
    client = _FakeManagedKafkaClient(
        clusters=[
            {
                "name": "projects/p/locations/us-central1/clusters/client-kafka",
                "state": "ACTIVE",
                "capacityConfig": {"vcpuCount": 6},
            }
        ]
    )

    rows = services._collect_managed_kafka(
        service,
        ["example-dev-project"],
        kafka_client=client,
        timeout_seconds=17,
    )

    assert rows == [
        {
            "project": "example-dev-project",
            "status": Status.OK.value,
            "cluster_count": 1,
            "clusters": [
                {
                    "name": "projects/p/locations/us-central1/clusters/client-kafka",
                    "state": "ACTIVE",
                    "capacity": {"vcpuCount": 6},
                }
            ],
        }
    ]
    assert client.calls == [("projects/example-dev-project/locations/-", 17)]
    assert service.list_calls == []


def test_services_falls_back_to_discovery_when_managed_kafka_client_fails() -> None:
    service = _FakeManagedKafkaDiscoveryService(
        [
            {
                "name": "projects/p/locations/us-central1/clusters/fallback-kafka",
                "state": "ACTIVE",
                "capacityConfig": {"vcpuCount": 3},
            }
        ]
    )
    client = _FakeManagedKafkaClient(error=RuntimeError("client unavailable"))

    rows = services._collect_managed_kafka(
        service,
        ["example-dev-project"],
        kafka_client=client,
        timeout_seconds=17,
    )

    assert rows == [
        {
            "project": "example-dev-project",
            "status": Status.OK.value,
            "cluster_count": 1,
            "clusters": [
                {
                    "name": "projects/p/locations/us-central1/clusters/fallback-kafka",
                    "state": "ACTIVE",
                    "capacity": {"vcpuCount": 3},
                }
            ],
        }
    ]
    assert client.calls == [("projects/example-dev-project/locations/-", 17)]
    assert service.list_calls == [
        {"parent": "projects/example-dev-project/locations/-", "pageSize": 100}
    ]


def test_services_collects_compute_instance_with_compute_client() -> None:
    client = _FakeComputeInstancesClient(
        instance={
            "name": "app-vm",
            "status": "RUNNING",
            "machineType": "zones/us-central1-a/machineTypes/e2-standard-2",
            "networkInterfaces": [{"name": "nic0"}],
        }
    )

    rows = services._collect_compute_instances(
        None,
        [
            {
                "project": "example-dev-project",
                "zone": "us-central1-a",
                "name": "app-vm",
            }
        ],
        instances_client=client,
        timeout_seconds=17,
    )

    assert rows == [
        {
            "status": Status.OK.value,
            "project": "example-dev-project",
            "zone": "us-central1-a",
            "name": "app-vm",
            "instance_status": "RUNNING",
            "machine_type": "e2-standard-2",
            "network_interfaces": 1,
        }
    ]
    assert client.get_calls == [("example-dev-project", "us-central1-a", "app-vm", 17)]


def test_services_discovers_compute_instances_with_compute_client() -> None:
    client = _FakeComputeInstancesClient(
        aggregated_items=[
            (
                "zones/us-central1-a",
                {
                    "instances": [
                        {
                            "name": "app-vm",
                            "status": "RUNNING",
                            "machineType": "zones/us-central1-a/machineTypes/e2-standard-2",
                            "networkInterfaces": [{"name": "nic0"}],
                        }
                    ]
                },
            )
        ]
    )

    rows = services._discover_compute_instances(
        None,
        ["example-dev-project"],
        instances_client=client,
        timeout_seconds=17,
    )

    assert rows == [
        {
            "status": Status.OK.value,
            "project": "example-dev-project",
            "zone": "us-central1-a",
            "name": "app-vm",
            "instance_status": "RUNNING",
            "machine_type": "e2-standard-2",
            "network_interfaces": 1,
        }
    ]
    assert client.aggregated_calls == [({"project": "example-dev-project", "max_results": 500}, 17)]


def test_services_falls_back_to_discovery_when_compute_client_fails() -> None:
    service = _FakeComputeInstancesDiscoveryService(
        instance={
            "name": "fallback-vm",
            "status": "RUNNING",
            "machineType": "zones/us-central1-a/machineTypes/e2-standard-2",
            "networkInterfaces": [{"name": "nic0"}],
        },
        aggregated_items={
            "zones/us-central1-a": {
                "instances": [
                    {
                        "name": "fallback-vm",
                        "status": "RUNNING",
                        "machineType": "zones/us-central1-a/machineTypes/e2-standard-2",
                        "networkInterfaces": [{"name": "nic0"}],
                    }
                ]
            }
        },
    )
    client = _FakeComputeInstancesClient(error=RuntimeError("client unavailable"))

    explicit_rows = services._collect_compute_instances(
        service,
        [
            {
                "project": "example-dev-project",
                "zone": "us-central1-a",
                "name": "fallback-vm",
            }
        ],
        instances_client=client,
        timeout_seconds=17,
    )
    discovered_rows = services._discover_compute_instances(
        service,
        ["example-dev-project"],
        instances_client=client,
        timeout_seconds=17,
    )

    expected_row = {
        "status": Status.OK.value,
        "project": "example-dev-project",
        "zone": "us-central1-a",
        "name": "fallback-vm",
        "instance_status": "RUNNING",
        "machine_type": "e2-standard-2",
        "network_interfaces": 1,
    }
    assert explicit_rows == [expected_row]
    assert discovered_rows == [expected_row]
    assert client.get_calls == [("example-dev-project", "us-central1-a", "fallback-vm", 17)]
    assert client.aggregated_calls == [({"project": "example-dev-project", "max_results": 500}, 17)]
    assert service.get_calls == [("example-dev-project", "us-central1-a", "fallback-vm")]
    assert service.aggregated_calls == [("example-dev-project", 500)]


def test_services_collects_load_balancers_with_compute_clients() -> None:
    global_client = _FakeBackendServicesClient(
        backend={
            "name": "global-backend",
            "protocol": "TCP",
            "loadBalancingScheme": "INTERNAL",
            "healthChecks": ["healthChecks/hc-global"],
            "backends": [{"group": "projects/p/zones/z/instanceGroups/ig-global"}],
        }
    )
    regional_client = _FakeRegionBackendServicesClient(
        backend={
            "name": "regional-backend",
            "protocol": "TCP",
            "loadBalancingScheme": "INTERNAL",
            "healthChecks": ["regions/us-central1/healthChecks/hc-regional"],
            "backends": [{"group": "projects/p/zones/z/instanceGroups/ig-regional"}],
        }
    )

    rows = services._collect_load_balancers(
        None,
        [
            {
                "project": "example-dev-project",
                "backend_service": "global-backend",
            },
            {
                "project": "example-dev-project",
                "scope": "region",
                "region": "us-central1",
                "backend_service": "regional-backend",
            },
        ],
        backend_services_client=global_client,
        region_backend_services_client=regional_client,
        timeout_seconds=17,
    )

    assert rows == [
        {
            "status": Status.OK.value,
            "project": "example-dev-project",
            "scope": "global",
            "region": "",
            "backend_service": "global-backend",
            "protocol": "TCP",
            "scheme": "INTERNAL",
            "health_checks": 1,
            "backends": 1,
            "backend_health": "healthy=1, unhealthy=0, unknown=0",
            "backend_health_counts": {"HEALTHY": 1},
            "reason": "",
        },
        {
            "status": Status.OK.value,
            "project": "example-dev-project",
            "scope": "region",
            "region": "us-central1",
            "backend_service": "regional-backend",
            "protocol": "TCP",
            "scheme": "INTERNAL",
            "health_checks": 1,
            "backends": 1,
            "backend_health": "healthy=1, unhealthy=0, unknown=0",
            "backend_health_counts": {"HEALTHY": 1},
            "reason": "",
        },
    ]
    assert global_client.get_calls == [("example-dev-project", "global-backend", 17)]
    assert regional_client.get_calls == [
        ("example-dev-project", "us-central1", "regional-backend", 17)
    ]
    assert global_client.health_calls == [
        ("example-dev-project", "global-backend", "projects/p/zones/z/instanceGroups/ig-global", 17)
    ]
    assert regional_client.health_calls == [
        (
            "example-dev-project",
            "us-central1",
            "regional-backend",
            "projects/p/zones/z/instanceGroups/ig-regional",
            17,
        )
    ]


def test_services_discovers_load_balancers_with_compute_client() -> None:
    client = _FakeBackendServicesClient(
        aggregated_items=[
            (
                "regions/us-central1",
                {
                    "backendServices": [
                        {
                            "name": "regional-backend",
                            "protocol": "TCP",
                            "loadBalancingScheme": "INTERNAL",
                            "healthChecks": ["regions/us-central1/healthChecks/hc-regional"],
                            "backends": [
                                {"group": "projects/p/zones/z/instanceGroups/ig-regional"}
                            ],
                        }
                    ]
                },
            )
        ]
    )
    regional_client = _FakeRegionBackendServicesClient()

    rows = services._discover_load_balancers(
        None,
        ["example-dev-project"],
        backend_services_client=client,
        region_backend_services_client=regional_client,
        timeout_seconds=17,
    )

    assert rows == [
        {
            "status": Status.OK.value,
            "project": "example-dev-project",
            "scope": "us-central1",
            "region": "us-central1",
            "backend_service": "regional-backend",
            "protocol": "TCP",
            "scheme": "INTERNAL",
            "health_checks": 1,
            "backends": 1,
            "backend_health": "healthy=1, unhealthy=0, unknown=0",
            "backend_health_counts": {"HEALTHY": 1},
            "reason": "",
        }
    ]
    assert client.aggregated_calls == [({"project": "example-dev-project", "max_results": 500}, 17)]
    assert regional_client.health_calls == [
        (
            "example-dev-project",
            "us-central1",
            "regional-backend",
            "projects/p/zones/z/instanceGroups/ig-regional",
            17,
        )
    ]


def test_services_falls_back_to_discovery_when_backend_service_clients_fail() -> None:
    backend = {
        "name": "fallback-backend",
        "protocol": "TCP",
        "loadBalancingScheme": "INTERNAL",
        "healthChecks": ["regions/us-central1/healthChecks/hc-regional"],
        "backends": [{"group": "projects/p/zones/z/instanceGroups/ig-regional"}],
    }
    service = _FakeBackendServicesDiscoveryService(
        global_backend=backend,
        regional_backend=backend,
        aggregated_items={"regions/us-central1": {"backendServices": [backend]}},
    )
    global_client = _FakeBackendServicesClient(error=RuntimeError("client unavailable"))
    regional_client = _FakeRegionBackendServicesClient(error=RuntimeError("client unavailable"))

    explicit_rows = services._collect_load_balancers(
        service,
        [
            {
                "project": "example-dev-project",
                "backend_service": "fallback-backend",
            },
            {
                "project": "example-dev-project",
                "scope": "region",
                "region": "us-central1",
                "backend_service": "fallback-backend",
            },
        ],
        backend_services_client=global_client,
        region_backend_services_client=regional_client,
        timeout_seconds=17,
    )
    discovered_rows = services._discover_load_balancers(
        service,
        ["example-dev-project"],
        backend_services_client=global_client,
        timeout_seconds=17,
    )

    expected_row = {
        "status": Status.OK.value,
        "project": "example-dev-project",
        "scope": "us-central1",
        "region": "us-central1",
        "backend_service": "fallback-backend",
        "protocol": "TCP",
        "scheme": "INTERNAL",
        "health_checks": 1,
        "backends": 1,
        "backend_health": "healthy=1, unhealthy=0, unknown=0",
        "backend_health_counts": {"HEALTHY": 1},
        "reason": "",
    }
    assert explicit_rows == [
        {**expected_row, "scope": "global", "region": ""},
        {**expected_row, "scope": "region"},
    ]
    assert discovered_rows == [expected_row]
    assert global_client.get_calls == [("example-dev-project", "fallback-backend", 17)]
    assert regional_client.get_calls == [
        ("example-dev-project", "us-central1", "fallback-backend", 17)
    ]
    assert global_client.aggregated_calls == [
        ({"project": "example-dev-project", "max_results": 500}, 17)
    ]
    assert service.regional_get_calls == [
        ("example-dev-project", "us-central1", "fallback-backend")
    ]
    assert service.global_get_calls == [("example-dev-project", "fallback-backend")]
    assert service.aggregated_calls == [("example-dev-project", 500)]


def test_kubernetes_health_collect_offline(monkeypatch) -> None:
    config = _default_config()
    request_name = _cluster_request_name(config.clusters[0])
    payloads = {
        request_name: {
            "endpoint": "10.0.0.5",
            "controlPlaneEndpointsConfig": {"dnsEndpointConfig": {"endpoint": "gke.dev.internal"}},
            "masterAuth": {
                "clusterCaCertificate": base64.b64encode(b"dummy-ca-cert").decode("utf-8")
            },
        }
    }

    node_ready = SimpleNamespace(
        metadata=SimpleNamespace(
            name="node-ready",
            creation_timestamp="2026-05-01T00:00:00+00:00",
        ),
        status=SimpleNamespace(conditions=[SimpleNamespace(type="Ready", status="True")]),
    )
    node_not_ready = SimpleNamespace(
        metadata=SimpleNamespace(
            name="node-not-ready",
            creation_timestamp="2026-05-02T00:00:00+00:00",
        ),
        status=SimpleNamespace(conditions=[SimpleNamespace(type="Ready", status="False")]),
    )
    pod_with_wait = SimpleNamespace(
        metadata=SimpleNamespace(namespace="apps", name="api-0"),
        status=SimpleNamespace(
            phase="Running",
            container_statuses=[
                SimpleNamespace(
                    name="api",
                    restart_count=5,
                    state=SimpleNamespace(
                        waiting=SimpleNamespace(
                            reason="CrashLoopBackOff",
                            message="back-off restarting failed container",
                        )
                    ),
                    last_state=SimpleNamespace(
                        terminated=SimpleNamespace(reason="OOMKilled", exit_code=137)
                    ),
                )
            ],
        ),
    )
    pod_ok = SimpleNamespace(
        metadata=SimpleNamespace(namespace="apps", name="api-1"),
        status=SimpleNamespace(phase="Running", container_statuses=[]),
    )
    pod_recovered = SimpleNamespace(
        metadata=SimpleNamespace(namespace="apps", name="api-2"),
        status=SimpleNamespace(
            phase="Running",
            container_statuses=[
                SimpleNamespace(
                    name="api",
                    restart_count=4,
                    state=SimpleNamespace(
                        running=SimpleNamespace(started_at="2026-05-02T00:00:00Z")
                    ),
                    last_state=SimpleNamespace(
                        terminated=SimpleNamespace(
                            reason="Error",
                            exit_code=1,
                            finished_at="2026-05-02T00:00:00Z",
                        )
                    ),
                )
            ],
        ),
    )

    snapshot = {
        "nodes": SimpleNamespace(items=[node_ready, node_not_ready]),
        "pods": SimpleNamespace(items=[pod_with_wait, pod_ok, pod_recovered]),
        "deployments": SimpleNamespace(
            items=[
                SimpleNamespace(
                    spec=SimpleNamespace(replicas=2),
                    status=SimpleNamespace(available_replicas=1),
                )
            ]
        ),
        "statefulsets": SimpleNamespace(items=[]),
        "daemonsets": SimpleNamespace(items=[]),
        "hpas": SimpleNamespace(
            items=[
                SimpleNamespace(
                    spec=SimpleNamespace(max_replicas=3),
                    status=SimpleNamespace(
                        current_replicas=3,
                        conditions=[SimpleNamespace(type="ScalingLimited", status="True")],
                    ),
                )
            ]
        ),
        "hpa_error": "",
        "node_metrics": [{"usage": {"cpu": "250m", "memory": "1Gi"}}],
        "pod_metrics": [
            {
                "metadata": {"name": "pod-1", "namespace": "apps"},
                "containers": [{"usage": {"cpu": "50m", "memory": "128Mi"}}],
            }
        ],
        "metrics_error": "",
        "events": SimpleNamespace(
            items=[
                SimpleNamespace(type="Warning", reason="BackOff"),
                SimpleNamespace(type="Normal", reason="Pulled"),
            ]
        ),
        "events_error": "",
    }

    monkeypatch.setattr(kubernetes_health, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(
        kubernetes_health,
        "build_service",
        lambda *_args, **_kwargs: _FakeContainerService(payloads),
    )
    monkeypatch.setattr(kubernetes_health, "build_cluster_manager_client", lambda _auth: None)
    monkeypatch.setattr(kubernetes_health, "_query_cluster", lambda **_kwargs: snapshot)

    result = kubernetes_health.collect(config=config)

    assert result.status == Status.WARNING
    cluster = result.details["clusters"][0]
    assert cluster["node_total"] == 2
    assert cluster["node_ready"] == 1
    assert cluster["node_inventory"] == [
        {
            "created_at": "2026-05-02T00:00:00+00:00",
            "name": "node-not-ready",
            "ready": False,
            "status": "NotReady",
        },
        {
            "created_at": "2026-05-01T00:00:00+00:00",
            "name": "node-ready",
            "ready": True,
            "status": "Ready",
        },
    ]
    assert cluster["workloads"]["deployments_unavailable"] == 1
    assert cluster["hpa"]["hpas_at_max"] == 1
    assert cluster["utilization"]["metrics_server_available"] is True
    assert cluster["namespace_utilization"][0]["namespace"] == "apps"
    assert cluster["events"]["warning_total"] == 1
    assert cluster["pod_issues"][0]["probable_cause"] == "oom_killed"
    assert any(issue["symptom"] == "HighRestartCount" for issue in cluster["pod_issues"])
    recovered_issue = next(
        issue for issue in cluster["pod_issues"] if issue["symptom"] == "HighRestartCount"
    )
    assert recovered_issue["evidence"]["current_state"] == "Running"
    assert recovered_issue["probable_cause"] == "non_zero_exit_code"


def test_kubernetes_health_gets_cluster_with_cluster_manager_client(monkeypatch) -> None:
    config = _default_config()
    request_name = _cluster_request_name(config.clusters[0])
    service = _FakeContainerService(
        {
            request_name: _kubernetes_cluster_payload(
                endpoint="10.0.0.5",
                dns_endpoint="gke.fallback.internal",
            )
        }
    )
    client = _FakeClusterManagerGetClient(
        payload=_kubernetes_cluster_payload(
            endpoint="10.0.0.6",
            dns_endpoint="gke.client.internal",
        )
    )
    snapshot = _minimal_kubernetes_snapshot()

    monkeypatch.setattr(kubernetes_health, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(kubernetes_health, "build_service", lambda *_args, **_kwargs: service)
    monkeypatch.setattr(kubernetes_health, "build_cluster_manager_client", lambda _auth: client)
    monkeypatch.setattr(kubernetes_health, "_query_cluster", lambda **_kwargs: snapshot)

    result = kubernetes_health.collect(config=config, timeout_seconds=17)

    assert result.status == Status.OK
    cluster = result.details["clusters"][0]
    assert cluster["endpoint"] == "gke.client.internal"
    assert client.get_calls == [(request_name, 17)]
    assert service.clusters().get_calls == []


def test_kubernetes_health_falls_back_to_discovery_when_cluster_manager_client_fails(
    monkeypatch,
) -> None:
    config = _default_config()
    request_name = _cluster_request_name(config.clusters[0])
    service = _FakeContainerService(
        {
            request_name: _kubernetes_cluster_payload(
                endpoint="10.0.0.5",
                dns_endpoint="gke.fallback.internal",
            )
        }
    )
    client = _FakeClusterManagerGetClient(error=RuntimeError("client unavailable"))
    snapshot = _minimal_kubernetes_snapshot()

    monkeypatch.setattr(kubernetes_health, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(kubernetes_health, "build_service", lambda *_args, **_kwargs: service)
    monkeypatch.setattr(kubernetes_health, "build_cluster_manager_client", lambda _auth: client)
    monkeypatch.setattr(kubernetes_health, "_query_cluster", lambda **_kwargs: snapshot)

    result = kubernetes_health.collect(config=config, timeout_seconds=17)

    assert result.status == Status.OK
    cluster = result.details["clusters"][0]
    assert cluster["endpoint"] == "gke.fallback.internal"
    assert client.get_calls == [(request_name, 17)]
    assert service.clusters().get_calls == [request_name]


def _kubernetes_cluster_payload(*, endpoint: str, dns_endpoint: str) -> dict[str, Any]:
    return {
        "endpoint": endpoint,
        "controlPlaneEndpointsConfig": {"dnsEndpointConfig": {"endpoint": dns_endpoint}},
        "masterAuth": {"clusterCaCertificate": base64.b64encode(b"dummy-ca-cert").decode("utf-8")},
    }


def _minimal_kubernetes_snapshot() -> dict[str, Any]:
    return {
        "nodes": SimpleNamespace(
            items=[
                SimpleNamespace(
                    metadata=SimpleNamespace(
                        name="node-ready",
                        creation_timestamp="2026-05-01T00:00:00+00:00",
                    ),
                    status=SimpleNamespace(
                        conditions=[SimpleNamespace(type="Ready", status="True")]
                    ),
                )
            ]
        ),
        "pods": SimpleNamespace(items=[]),
        "deployments": SimpleNamespace(items=[]),
        "statefulsets": SimpleNamespace(items=[]),
        "daemonsets": SimpleNamespace(items=[]),
        "hpas": SimpleNamespace(items=[]),
        "hpa_error": "",
        "node_metrics": [],
        "pod_metrics": [],
        "metrics_error": "",
        "events": SimpleNamespace(items=[]),
        "events_error": "",
    }


def test_kubernetes_hpa_summary_classifies_autoscaling_layers() -> None:
    config = _default_config()
    config.report_expectations.autoscaling.workload_hpa.status = "informational"
    config.report_expectations.autoscaling.platform_hpa.status = "assessed"
    hpas = SimpleNamespace(
        items=[
            SimpleNamespace(
                metadata=SimpleNamespace(namespace="apps", name="web"),
                spec=SimpleNamespace(min_replicas=2, max_replicas=4),
                status=SimpleNamespace(
                    current_replicas=4,
                    desired_replicas=4,
                    conditions=[],
                ),
            ),
            SimpleNamespace(
                metadata=SimpleNamespace(namespace="istio-system", name="istiod"),
                spec=SimpleNamespace(min_replicas=2, max_replicas=3),
                status=SimpleNamespace(
                    current_replicas=3,
                    desired_replicas=3,
                    conditions=[],
                ),
            ),
        ]
    )

    summary = kubernetes_health._hpa_summary({"hpas": hpas, "events": None}, config)

    assert summary["workload_hpa_total"] == 1
    assert summary["platform_hpa_total"] == 1
    assert summary["hpas_at_max"] == 2
    assert summary["scored_hpas_at_max"] == 1
    assert summary["details"][0]["autoscaling_layer"] == "workload_hpa"
    assert summary["details"][0]["policy_status"] == "informational"
    assert summary["details"][1]["autoscaling_layer"] == "platform_hpa"
    assert summary["details"][1]["policy_status"] == "assessed"


def test_audit_collect_warns_on_high_risk_events(monkeypatch) -> None:
    config = _default_config()
    monkeypatch.setattr(audit, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(audit, "build_service", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        audit,
        "_list_entries",
        lambda **_kwargs: (
            [
                {
                    "timestamp": "2026-05-14T00:00:00Z",
                    "protoPayload": {
                        "methodName": "google.iam.admin.v1.SetIamPolicy",
                        "authenticationInfo": {"principalEmail": "user@example.com"},
                        "resourceName": "projects/p/serviceAccounts/sa",
                    },
                },
                {
                    "timestamp": "2026-05-14T01:00:00Z",
                    "protoPayload": {
                        "methodName": (
                            "google.cloud.secretmanager.v1.SecretManagerService.CreateSecret"
                        ),
                        "authenticationInfo": {"principalEmail": "user@example.com"},
                        "resourceName": "projects/p/secrets/s1",
                    },
                },
                {
                    "timestamp": "2026-05-14T02:00:00Z",
                    "protoPayload": {
                        "methodName": "v1.compute.firewalls.delete",
                        "authenticationInfo": {"principalEmail": "netops@example.com"},
                        "resourceName": "projects/p/global/firewalls/allow-old",
                    },
                },
            ],
            False,
        ),
    )

    result = audit.collect(config=config)

    assert result.status == Status.WARNING
    project = result.details["projects"][0]
    assert project["audit_change_count"] == 3
    assert project["audit_entries_limited"] is False
    assert project["high_risk_events"] == 2
    assert project["new_secret_events"] == 1
    assert project["delete_events"] == 1
    assert project["recent_events"][2]["action"] == "delete"
    assert project["meaningful_change_events"] == 3
    assert project["meaningful_secret_events"] == 1
    assert project["meaningful_recent_events"][1]["resource_type"] == "Secret"
    assert project["meaningful_recent_events"][1]["resource_name"] == "s1"


def test_audit_collect_filters_routine_system_configmap_updates(monkeypatch) -> None:
    config = _default_config()
    monkeypatch.setattr(audit, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(audit, "build_service", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        audit,
        "_list_entries",
        lambda **_kwargs: (
            [
                {
                    "timestamp": "2026-05-14T00:00:00Z",
                    "protoPayload": {
                        "methodName": "io.k8s.core.v1.configmaps.update",
                        "authenticationInfo": {"principalEmail": "system:cluster-autoscaler"},
                        "resourceName": (
                            "core/v1/namespaces/kube-system/configmaps/cluster-autoscaler-status"
                        ),
                    },
                },
                {
                    "timestamp": "2026-05-14T00:01:00Z",
                    "protoPayload": {
                        "methodName": "io.k8s.core.v1.configmaps.update",
                        "authenticationInfo": {"principalEmail": "user@example.com"},
                        "resourceName": "core/v1/namespaces/apps/configmaps/app-config",
                    },
                },
                {
                    "timestamp": "2026-05-14T00:02:00Z",
                    "protoPayload": {
                        "methodName": "io.k8s.core.v1.secrets.update",
                        "authenticationInfo": {"principalEmail": "user@example.com"},
                        "resourceName": "core/v1/namespaces/apps/secrets/api-token",
                    },
                },
            ],
            False,
        ),
    )

    result = audit.collect(config=config)

    project = result.details["projects"][0]
    assert project["noisy_events_filtered"] == 1
    assert project["meaningful_change_events"] == 2
    assert project["meaningful_configmap_events"] == 1
    assert project["meaningful_secret_events"] == 1
    assert [event["resource_name"] for event in project["meaningful_recent_events"]] == [
        "app-config",
        "api-token",
    ]


def test_audit_collect_uses_targeted_review_candidate_query(monkeypatch) -> None:
    config = _default_config()
    monkeypatch.setattr(audit, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(audit, "build_service", lambda *_args, **_kwargs: object())
    filters: list[str] = []

    noisy_entry = {
        "timestamp": "2026-05-14T00:00:00Z",
        "protoPayload": {
            "methodName": "io.k8s.coordination.v1.leases.update",
            "authenticationInfo": {"principalEmail": "system:kube-controller-manager"},
            "resourceName": (
                "coordination.k8s.io/v1/namespaces/kube-system/leases/kube-controller-manager"
            ),
        },
    }
    candidate_entry = {
        "timestamp": "2026-05-14T00:01:00Z",
        "protoPayload": {
            "methodName": "io.k8s.core.v1.configmaps.update",
            "authenticationInfo": {"principalEmail": "user@example.com"},
            "resourceName": "core/v1/namespaces/apps/configmaps/payment-config",
        },
    }

    def fake_list_entries(**kwargs) -> tuple[list[dict[str, Any]], bool]:
        filter_text = str(kwargs["filter_text"])
        filters.append(filter_text)
        if 'protoPayload.methodName:"configmaps"' in filter_text:
            return [candidate_entry], False
        if "SetIamPolicy" in filter_text:
            return [], False
        return [noisy_entry] * 1000, True

    monkeypatch.setattr(audit, "_list_entries", fake_list_entries)

    result = audit.collect(config=config)

    project = result.details["projects"][0]
    assert project["audit_change_count"] == 1000
    assert project["audit_entries_limited"] is True
    assert project["noisy_events_filtered"] == 1000
    assert project["review_candidate_entry_count"] == 1
    assert project["meaningful_change_events"] == 1
    assert project["meaningful_configmap_events"] == 1
    assert project["meaningful_recent_events"][0]["resource_name"] == "payment-config"
    assert len(filters) == 3
    assert all(
        'logName="projects/example-dev-project/logs/cloudaudit.googleapis.com%2Factivity"'
        in filter_text
        for filter_text in filters
    )
    assert 'resource.type!="k8s_cluster"' in filters[2]


def test_audit_collect_uses_logging_client_without_discovery_init(monkeypatch) -> None:
    config = _default_config()
    entry = {
        "timestamp": "2026-05-14T00:00:00Z",
        "protoPayload": {
            "methodName": "io.k8s.core.v1.secrets.create",
            "authenticationInfo": {"principalEmail": "user@example.com"},
            "resourceName": "core/v1/namespaces/default/secrets/app-secret",
        },
    }
    client = _FakeLoggingEntriesClient(entries=[_FakeLoggingEntryResource(entry)])
    built_services: list[str] = []

    def build_service(
        _auth: Any,
        service_name: str,
        _version: str,
        _timeout_seconds: int,
    ) -> object:
        built_services.append(service_name)
        raise RuntimeError("discovery unavailable")

    monkeypatch.setattr(audit, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(audit, "build_logging_client", lambda _auth: client)
    monkeypatch.setattr(audit, "build_service", build_service)

    result = audit.collect(config=config)

    assert result.status == Status.OK
    project = result.details["projects"][0]
    assert project["audit_change_count"] == 1
    assert project["review_candidate_entry_count"] == 1
    assert project["meaningful_secret_events"] == 1
    assert len(client.calls) == 3
    assert built_services == []


def test_audit_lists_entries_with_cloud_logging_client() -> None:
    fallback_entry = {
        "timestamp": "2026-05-14T00:00:00Z",
        "protoPayload": {"methodName": "fallback.method"},
    }
    client_entry = {
        "timestamp": "2026-05-14T01:00:00Z",
        "protoPayload": {"methodName": "client.method"},
    }
    extra_entry = {
        "timestamp": "2026-05-14T02:00:00Z",
        "protoPayload": {"methodName": "extra.method"},
    }
    service = _FakeLoggingDiscoveryService(entries=[fallback_entry])
    client = _FakeLoggingEntriesClient(
        entries=[_FakeLoggingEntryResource(client_entry), extra_entry]
    )

    rows, limited = audit._list_entries(
        service,
        "example-dev-project",
        'protoPayload.methodName:"client"',
        max_entries=1,
        logging_client=client,
    )

    assert rows == [client_entry]
    assert limited is True
    assert client.calls == [
        {
            "resource_names": ["projects/example-dev-project"],
            "filter_": 'protoPayload.methodName:"client"',
            "order_by": "timestamp desc",
            "max_results": 2,
            "page_size": 2,
        }
    ]
    assert service.entries().list_calls == []


def test_audit_falls_back_to_discovery_when_cloud_logging_client_fails() -> None:
    fallback_entry = {
        "timestamp": "2026-05-14T00:00:00Z",
        "protoPayload": {"methodName": "fallback.method"},
    }
    service = _FakeLoggingDiscoveryService(
        entries=[fallback_entry],
        entries_next_page_token="next",
    )
    client = _FakeLoggingEntriesClient(error=RuntimeError("client unavailable"))

    rows, limited = audit._list_entries(
        service,
        "example-dev-project",
        'protoPayload.methodName:"fallback"',
        max_entries=1,
        logging_client=client,
    )

    assert rows == [fallback_entry]
    assert limited is True
    assert client.calls == [
        {
            "resource_names": ["projects/example-dev-project"],
            "filter_": 'protoPayload.methodName:"fallback"',
            "order_by": "timestamp desc",
            "max_results": 2,
            "page_size": 2,
        }
    ]
    assert service.entries().list_calls == [
        {
            "body": {
                "resourceNames": ["projects/example-dev-project"],
                "filter": 'protoPayload.methodName:"fallback"',
                "orderBy": "timestamp desc",
                "pageSize": 1,
            }
        }
    ]


def test_network_collect_reports_inactive_peering(monkeypatch) -> None:
    config = _default_config()
    monkeypatch.setattr(network, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(network, "build_service", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        network,
        "_list_networks",
        lambda _compute, _project, **_kwargs: [
            {
                "name": "vpc-a",
                "peerings": [
                    {"name": "peer-a", "state": "ACTIVE"},
                    {"name": "peer-b", "state": "INACTIVE"},
                ],
            }
        ],
    )
    monkeypatch.setattr(
        network,
        "_list_firewalls",
        lambda _compute, _project, **_kwargs: [{"disabled": False}],
    )
    monkeypatch.setattr(
        network,
        "_list_forwarding_rules",
        lambda _compute, _project, **_kwargs: [{"name": "fr-1"}],
    )
    monkeypatch.setattr(
        network,
        "_list_dns_zones",
        lambda _dns, _project, **_kwargs: [{"name": "zone-1"}],
    )
    monkeypatch.setattr(
        network,
        "_list_response_policies",
        lambda _dns, _project: [
            {
                "responsePolicyName": "private-overrides",
                "description": "shared services overrides",
                "networks": [{"networkUrl": "projects/p/global/networks/vpc-a"}],
            }
        ],
    )
    monkeypatch.setattr(
        network,
        "_list_response_policy_rules",
        lambda _dns, _project, _policies: [
            {
                "response_policy": "private-overrides",
                "name": "prometheus",
                "dns_name": "prometheus.internal.",
                "behavior": "",
                "local_data_record_count": 1,
            }
        ],
    )

    result = network.collect(config=config)

    assert result.status == Status.WARNING
    project = result.details["projects"][0]
    assert project["network_count"] == 1
    assert project["peering_count"] == 2
    assert project["peering_inactive_count"] == 1
    assert project["response_policy_count"] == 1
    assert project["response_policy_rules"][0]["dns_name"] == "prometheus.internal."


def test_network_collect_does_not_warn_when_response_policies_are_missing(monkeypatch) -> None:
    config = _default_config()
    monkeypatch.setattr(network, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(network, "build_service", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(network, "_list_networks", lambda _compute, _project, **_kwargs: [])
    monkeypatch.setattr(network, "_list_firewalls", lambda _compute, _project, **_kwargs: [])
    monkeypatch.setattr(network, "_list_forwarding_rules", lambda _compute, _project, **_kwargs: [])
    monkeypatch.setattr(
        network,
        "_list_dns_zones",
        lambda _dns, _project, **_kwargs: [{"name": "zone-1"}],
    )
    monkeypatch.setattr(network, "_list_response_policies", lambda _dns, _project: [])
    monkeypatch.setattr(
        network,
        "_list_response_policy_rules",
        lambda _dns, _project, _policies: [],
    )

    result = network.collect(config=config)

    assert result.status == Status.OK
    project = result.details["projects"][0]
    assert project["status"] == Status.OK.value
    assert project["response_policy_count"] == 0
    assert project["dns_policy_findings"] == []


def test_network_cluster_dns_uses_cluster_manager_client(monkeypatch) -> None:
    config = _default_config()
    cluster = config.clusters[0]
    request_name = _cluster_request_name(cluster)
    service = _FakeContainerService(
        {
            request_name: _kubernetes_cluster_payload(
                endpoint="10.0.0.5",
                dns_endpoint="",
            )
        }
    )
    client = _FakeClusterManagerGetClient(
        payload=_kubernetes_cluster_payload(endpoint="10.0.0.6", dns_endpoint="")
    )
    _patch_network_service_dns_probe(monkeypatch)

    row, status = network._collect_cluster_dns_state(
        container=service,
        cluster_client=client,
        auth=_auth_bundle(),
        project=cluster.project,
        region=cluster.region,
        cluster_name=cluster.name,
        required_fqdns=["api.apps.svc.cluster.local"],
        timeout_seconds=17,
    )

    assert status == Status.OK
    assert row["endpoint"] == "10.0.0.6"
    assert row["resolved_fqdns"] == ["api.apps.svc.cluster.local"]
    assert client.get_calls == [(request_name, 17)]
    assert service.clusters().get_calls == []


def test_network_cluster_dns_falls_back_when_cluster_manager_client_fails(monkeypatch) -> None:
    config = _default_config()
    cluster = config.clusters[0]
    request_name = _cluster_request_name(cluster)
    service = _FakeContainerService(
        {
            request_name: _kubernetes_cluster_payload(
                endpoint="10.0.0.5",
                dns_endpoint="",
            )
        }
    )
    client = _FakeClusterManagerGetClient(error=RuntimeError("client unavailable"))
    _patch_network_service_dns_probe(monkeypatch)

    row, status = network._collect_cluster_dns_state(
        container=service,
        cluster_client=client,
        auth=_auth_bundle(),
        project=cluster.project,
        region=cluster.region,
        cluster_name=cluster.name,
        required_fqdns=["api.apps.svc.cluster.local"],
        timeout_seconds=17,
    )

    assert status == Status.OK
    assert row["endpoint"] == "10.0.0.5"
    assert row["resolved_fqdns"] == ["api.apps.svc.cluster.local"]
    assert client.get_calls == [(request_name, 17)]
    assert service.clusters().get_calls == [request_name]


def _patch_network_service_dns_probe(monkeypatch) -> None:
    @contextmanager
    def fake_cluster_ca_file(_cert: str) -> Any:
        yield Path("ca.pem")

    @contextmanager
    def fake_api_client(**_kwargs: Any) -> Any:
        yield object()

    class FakeCoreV1Api:
        def __init__(self, _client: Any) -> None:
            pass

        def list_service_for_all_namespaces(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                items=[
                    SimpleNamespace(
                        metadata=SimpleNamespace(name="api", namespace="apps"),
                    )
                ]
            )

    monkeypatch.setattr(network, "access_token", lambda *_args, **_kwargs: "token")
    monkeypatch.setattr(network, "cluster_ca_file", fake_cluster_ca_file)
    monkeypatch.setattr(network, "api_client", fake_api_client)
    monkeypatch.setattr(network.k8s_client, "CoreV1Api", FakeCoreV1Api)


def test_network_lists_networks_with_compute_client() -> None:
    client = _FakeComputeListClient(
        items=[
            {
                "name": "dev-vpc",
                "autoCreateSubnetworks": False,
                "routingConfig": {"routingMode": "GLOBAL"},
            }
        ]
    )

    rows = network._list_networks(
        None,
        "example-dev-project",
        networks_client=client,
        timeout_seconds=17,
    )

    assert rows == [
        {
            "name": "dev-vpc",
            "autoCreateSubnetworks": False,
            "routingConfig": {"routingMode": "GLOBAL"},
        }
    ]
    assert client.list_calls == [("example-dev-project", 17)]


def test_network_lists_firewalls_with_compute_client() -> None:
    client = _FakeComputeListClient(
        items=[
            {
                "name": "allow-internal",
                "network": "projects/p/global/networks/dev-vpc",
                "direction": "INGRESS",
            }
        ]
    )

    rows = network._list_firewalls(
        None,
        "example-dev-project",
        firewalls_client=client,
        timeout_seconds=17,
    )

    assert rows == [
        {
            "name": "allow-internal",
            "network": "projects/p/global/networks/dev-vpc",
            "direction": "INGRESS",
        }
    ]
    assert client.list_calls == [("example-dev-project", 17)]


def test_network_falls_back_to_discovery_when_compute_clients_fail() -> None:
    service = _FakeComputeNetworkDiscoveryService(
        networks=[
            {
                "name": "fallback-vpc",
                "autoCreateSubnetworks": True,
                "routingConfig": {"routingMode": "REGIONAL"},
            }
        ],
        firewalls=[
            {
                "name": "fallback-firewall",
                "network": "projects/p/global/networks/fallback-vpc",
            }
        ],
    )
    networks_client = _FakeComputeListClient(error=RuntimeError("client unavailable"))
    firewalls_client = _FakeComputeListClient(error=RuntimeError("client unavailable"))

    networks = network._list_networks(
        service,
        "example-dev-project",
        networks_client=networks_client,
        timeout_seconds=17,
    )
    firewalls = network._list_firewalls(
        service,
        "example-dev-project",
        firewalls_client=firewalls_client,
        timeout_seconds=17,
    )

    assert networks == [
        {
            "name": "fallback-vpc",
            "autoCreateSubnetworks": True,
            "routingConfig": {"routingMode": "REGIONAL"},
        }
    ]
    assert firewalls == [
        {
            "name": "fallback-firewall",
            "network": "projects/p/global/networks/fallback-vpc",
        }
    ]
    assert networks_client.list_calls == [("example-dev-project", 17)]
    assert firewalls_client.list_calls == [("example-dev-project", 17)]
    assert service.networks().list_calls == [{"project": "example-dev-project"}]
    assert service.firewalls().list_calls == [{"project": "example-dev-project"}]


def test_network_lists_forwarding_rules_with_compute_client() -> None:
    client = _FakeComputeAggregatedListClient(
        items=[
            (
                "regions/us-central1",
                {
                    "forwardingRules": [
                        {
                            "name": "app-forwarding-rule",
                            "IPAddress": "10.0.0.20",
                        }
                    ]
                },
            )
        ]
    )

    rows = network._list_forwarding_rules(
        None,
        "example-dev-project",
        forwarding_rules_client=client,
        timeout_seconds=17,
    )

    assert rows == [{"name": "app-forwarding-rule", "IPAddress": "10.0.0.20"}]
    assert client.aggregated_calls == [({"project": "example-dev-project"}, 17)]


def test_network_falls_back_to_discovery_when_forwarding_rules_client_fails() -> None:
    service = _FakeComputeNetworkDiscoveryService(
        forwarding_rule_items={
            "regions/us-central1": {
                "forwardingRules": [
                    {
                        "name": "fallback-forwarding-rule",
                        "IPAddress": "10.0.0.10",
                    }
                ]
            }
        }
    )
    client = _FakeComputeAggregatedListClient(error=RuntimeError("client unavailable"))

    rows = network._list_forwarding_rules(
        service,
        "example-dev-project",
        forwarding_rules_client=client,
        timeout_seconds=17,
    )

    assert rows == [{"name": "fallback-forwarding-rule", "IPAddress": "10.0.0.10"}]
    assert client.aggregated_calls == [({"project": "example-dev-project"}, 17)]
    assert service.forwarding_rule_calls == [{"project": "example-dev-project"}]


def test_network_lists_dns_zones_with_dns_client() -> None:
    client = _FakeDnsClient(
        zones=[
            _FakeDnsZone(
                name="fallback-zone",
                dns_name="fallback.example.",
                properties={"name": "internal-zone", "dns_name": "internal.example."},
            )
        ]
    )

    rows = network._list_dns_zones(
        None,
        "example-dev-project",
        dns_client=client,
    )

    assert rows == [{"name": "internal-zone", "dnsName": "internal.example."}]
    assert client.list_zone_calls == [None]


def test_network_counts_dns_record_sets_with_dns_client() -> None:
    client = _FakeDnsClient(zone_records={"internal-zone": [object(), object()]})

    count = network._count_dns_record_sets(
        None,
        "example-dev-project",
        "internal-zone",
        dns_client=client,
    )

    assert count == 2
    assert client.zone_calls == ["internal-zone"]


def test_network_falls_back_to_discovery_when_dns_client_fails() -> None:
    class FakeDnsDiscoveryService:
        def __init__(self) -> None:
            self.zone_calls: list[dict[str, Any]] = []
            self.record_calls: list[dict[str, Any]] = []

        def managedZones(self) -> FakeDnsDiscoveryService:  # noqa: N802
            return self

        def resourceRecordSets(self) -> FakeDnsDiscoveryService:  # noqa: N802
            return self

        def list(self, **kwargs: Any) -> _FakeRequest:
            if "managedZone" in kwargs:
                self.record_calls.append(dict(kwargs))
                return _FakeRequest({"rrsets": [{"name": "a"}, {"name": "b"}]})
            self.zone_calls.append(dict(kwargs))
            return _FakeRequest({"managedZones": [{"name": "fallback-zone"}]})

    service = FakeDnsDiscoveryService()
    client = _FakeDnsClient(error=RuntimeError("client unavailable"))

    zones = network._list_dns_zones(
        service,
        "example-dev-project",
        dns_client=client,
    )
    count = network._count_dns_record_sets(
        service,
        "example-dev-project",
        "fallback-zone",
        dns_client=client,
    )

    assert zones == [{"name": "fallback-zone"}]
    assert count == 2
    assert client.list_zone_calls == [None]
    assert client.zone_calls == ["fallback-zone"]
    assert service.zone_calls == [{"project": "example-dev-project"}]
    assert service.record_calls == [
        {"project": "example-dev-project", "managedZone": "fallback-zone", "maxResults": 500}
    ]


def test_mesh_collect_offline(monkeypatch, tmp_path: Path) -> None:
    config = _default_config()
    request_name = _cluster_request_name(config.clusters[0])
    payloads = {
        request_name: {
            "endpoint": "10.0.0.5",
            "controlPlaneEndpointsConfig": {"dnsEndpointConfig": {"endpoint": "gke.dev.internal"}},
            "masterAuth": {
                "clusterCaCertificate": base64.b64encode(b"dummy-ca-cert").decode("utf-8")
            },
        }
    }

    monkeypatch.setattr(mesh, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(
        mesh,
        "build_service",
        lambda *_args, **_kwargs: _FakeContainerService(payloads),
    )
    monkeypatch.setattr(mesh, "build_cluster_manager_client", lambda _auth: None)
    monkeypatch.setattr(
        mesh,
        "resolve_clusters",
        lambda _config, _service, **_kwargs: config.clusters,
    )
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("dummy", encoding="utf-8")

    @contextmanager
    def fake_cluster_ca_file(_cert: str) -> Any:
        yield ca_file

    monkeypatch.setattr(mesh, "cluster_ca_file", fake_cluster_ca_file)
    monkeypatch.setattr(
        mesh,
        "_query_mesh_health",
        lambda **_kwargs: {
            "ingress_gateway": {
                "total_pods": 1,
                "ready_pods": 1,
                "pod_names": ["istio-ingressgateway-1"],
                "ready_pod_names": ["istio-ingressgateway-1"],
            },
            "east_west_gateway": {
                "total_pods": 1,
                "ready_pods": 1,
                "pod_names": ["istio-eastwestgateway-1"],
                "ready_pod_names": ["istio-eastwestgateway-1"],
            },
            "istiod": {
                "total_pods": 1,
                "ready_pods": 1,
                "pod_names": ["istiod-1"],
                "ready_pod_names": ["istiod-1"],
            },
            "remote_secret_count": 2,
            "remote_secret_names": ["istio-remote-secret-a", "istio-remote-secret-b"],
            "envoy_proxy_samples": [
                {
                    "pod": "istio-ingressgateway-1",
                    "status": Status.OK.value,
                    "state": "LIVE",
                    "istio_version": "1.29.1",
                    "cluster_id": "dev-cluster",
                    "network": "dev-network",
                    "discovery_address": "istiod.istio-system.svc:15012",
                }
            ],
        },
    )
    monkeypatch.setattr(
        mesh,
        "_collect_envoy_proxy_samples",
        lambda **_kwargs: [
            {
                "pod": "istio-ingressgateway-1",
                "status": Status.OK.value,
                "state": "LIVE",
                "istio_version": "1.29.1",
                "cluster_id": "dev-cluster",
                "network": "dev-network",
                "discovery_address": "istiod.istio-system.svc:15012",
            }
        ],
    )
    monkeypatch.setattr(
        mesh,
        "_collect_istio_remote_clusters",
        lambda **_kwargs: {"status": Status.OK.value, "rows": [], "error": ""},
    )
    monkeypatch.setattr(
        mesh,
        "_collect_istio_proxy_status",
        lambda **_kwargs: {"status": Status.OK.value, "rows": [], "error": ""},
    )
    monkeypatch.setattr(mesh, "_attach_mesh_api_proxy_log_summaries", lambda **_kwargs: None)

    result = mesh.collect(config=config)

    assert result.status == Status.OK
    cluster = result.details["clusters"][0]
    assert cluster["cluster"] == "dev-cluster"
    assert cluster["remote_secret_count"] == 2
    assert cluster["istiod"]["ready_pods"] == 1
    assert cluster["remote_secret_names"] == ["istio-remote-secret-a", "istio-remote-secret-b"]
    assert cluster["envoy_proxy_samples"][0]["state"] == "LIVE"


def test_mesh_gets_cluster_with_cluster_manager_client(monkeypatch) -> None:
    config = _default_config()
    request_name = _cluster_request_name(config.clusters[0])
    service = _FakeContainerService(
        {
            request_name: _kubernetes_cluster_payload(
                endpoint="10.0.0.5",
                dns_endpoint="gke.fallback.internal",
            )
        }
    )
    client = _FakeClusterManagerGetClient(
        payload=_kubernetes_cluster_payload(
            endpoint="10.0.0.6",
            dns_endpoint="gke.client.internal",
        )
    )

    monkeypatch.setattr(mesh, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(mesh, "build_service", lambda *_args, **_kwargs: service)
    monkeypatch.setattr(mesh, "build_cluster_manager_client", lambda _auth: client)
    monkeypatch.setattr(mesh, "_query_mesh_health", lambda **_kwargs: _healthy_mesh_result())
    monkeypatch.setattr(mesh, "_collect_envoy_proxy_samples", lambda **_kwargs: [])
    monkeypatch.setattr(
        mesh,
        "_collect_istio_remote_clusters",
        lambda **_kwargs: {"status": Status.OK.value, "rows": [], "error": ""},
    )
    monkeypatch.setattr(
        mesh,
        "_collect_istio_proxy_status",
        lambda **_kwargs: {"status": Status.OK.value, "rows": [], "error": ""},
    )
    monkeypatch.setattr(mesh, "_attach_mesh_api_proxy_log_summaries", lambda **_kwargs: None)

    result = mesh.collect(config=config, timeout_seconds=17)

    assert result.status == Status.OK
    cluster = result.details["clusters"][0]
    assert cluster["endpoint"] == "gke.client.internal"
    assert client.get_calls == [(request_name, 17)]
    assert service.clusters().get_calls == []


def test_mesh_falls_back_to_discovery_when_cluster_manager_client_fails(monkeypatch) -> None:
    config = _default_config()
    request_name = _cluster_request_name(config.clusters[0])
    service = _FakeContainerService(
        {
            request_name: _kubernetes_cluster_payload(
                endpoint="10.0.0.5",
                dns_endpoint="gke.fallback.internal",
            )
        }
    )
    client = _FakeClusterManagerGetClient(error=RuntimeError("client unavailable"))

    monkeypatch.setattr(mesh, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(mesh, "build_service", lambda *_args, **_kwargs: service)
    monkeypatch.setattr(mesh, "build_cluster_manager_client", lambda _auth: client)
    monkeypatch.setattr(mesh, "_query_mesh_health", lambda **_kwargs: _healthy_mesh_result())
    monkeypatch.setattr(mesh, "_collect_envoy_proxy_samples", lambda **_kwargs: [])
    monkeypatch.setattr(
        mesh,
        "_collect_istio_remote_clusters",
        lambda **_kwargs: {"status": Status.OK.value, "rows": [], "error": ""},
    )
    monkeypatch.setattr(
        mesh,
        "_collect_istio_proxy_status",
        lambda **_kwargs: {"status": Status.OK.value, "rows": [], "error": ""},
    )
    monkeypatch.setattr(mesh, "_attach_mesh_api_proxy_log_summaries", lambda **_kwargs: None)

    result = mesh.collect(config=config, timeout_seconds=17)

    assert result.status == Status.OK
    cluster = result.details["clusters"][0]
    assert cluster["endpoint"] == "gke.fallback.internal"
    assert client.get_calls == [(request_name, 17)]
    assert service.clusters().get_calls == [request_name]


def _healthy_mesh_result() -> dict[str, Any]:
    return {
        "ingress_gateway": {
            "total_pods": 1,
            "ready_pods": 1,
            "pod_names": ["istio-ingressgateway-1"],
            "ready_pod_names": ["istio-ingressgateway-1"],
        },
        "east_west_gateway": {
            "total_pods": 1,
            "ready_pods": 1,
            "pod_names": ["istio-eastwestgateway-1"],
            "ready_pod_names": ["istio-eastwestgateway-1"],
        },
        "istiod": {
            "total_pods": 1,
            "ready_pods": 1,
            "pod_names": ["istiod-1"],
            "ready_pod_names": ["istiod-1"],
        },
        "remote_secret_count": 0,
        "remote_secret_names": [],
    }


def test_mesh_parses_istioctl_and_squid_proxy_evidence() -> None:
    remote_rows = mesh._parse_istio_remote_clusters(
        "NAME SECRET STATUS ISTIOD\ndev-cluster istio-remote-secret-dev synced istiod-abc\n"
    )
    proxy_rows = mesh._parse_istio_proxy_status(
        "NAME CLUSTER ISTIOD VERSION SUBSCRIBED TYPES\n"
        "istio-ingressgateway-abc Kubernetes istiod-abc 1.29.1 HTTP_ROUTE LISTENER\n"
    )
    squid_summary = mesh._parse_squid_access_log(
        "1710000000.1 10 10.0.0.2 TCP_TUNNEL/200 0 CONNECT "
        "gke-dev.us-central1.gke.goog:443 - HIER_DIRECT/216.239.32.27 -\n"
        "1710000001.1 20 10.0.0.2 TCP_TUNNEL/503 0 CONNECT "
        "gke-dev.us-central1.gke.goog:443 - HIER_NONE/- -\n"
    )

    assert remote_rows == [
        {
            "name": "dev-cluster",
            "secret": "istio-remote-secret-dev",
            "sync_status": "synced",
            "istiod": "istiod-abc",
        }
    ]
    assert proxy_rows[0]["version"] == "1.29.1"
    assert proxy_rows[0]["sync_state"] == "HTTP_ROUTE LISTENER"
    assert squid_summary["tunnel_success_count"] == 1
    assert squid_summary["tunnel_failure_count"] == 1
    assert squid_summary["latest_success"]["target"] == "gke-dev.us-central1.gke.goog:443"


def test_mesh_formats_istioctl_timeout_without_raw_python_command(monkeypatch) -> None:
    def timeout_run(*_args: Any, **_kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(
            cmd=[
                "istioctl",
                "--context",
                "gke_example-dev-project_us-central1_dev-cluster",
                "proxy-status",
            ],
            timeout=_kwargs["timeout"],
        )

    monkeypatch.setattr(mesh.subprocess, "run", timeout_run)

    result = mesh._collect_istio_proxy_status(
        context="gke_example-dev-project_us-central1_dev-cluster",
        timeout_seconds=180,
    )

    assert result == {
        "status": Status.SKIPPED_NETWORK.value,
        "rows": [],
        "error": (
            "istioctl proxy-status timed out after 30 seconds for context "
            "gke_example-dev-project_us-central1_dev-cluster"
        ),
    }


def test_mesh_collects_envoy_server_info_with_kubernetes_stream(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_stream(method: Any, *args: Any, **kwargs: Any) -> str:
        calls.append({"method": method, "args": args, "kwargs": kwargs})
        return str(
            {
                "state": "LIVE",
                "node": {
                    "cluster": "istio-ingressgateway",
                    "metadata": {
                        "ISTIO_VERSION": "1.29.1",
                        "CLUSTER_ID": "dev",
                        "NETWORK": "dev-network",
                        "PROXY_CONFIG": {"discoveryAddress": "istiod:15012"},
                    },
                },
            }
        )

    core_api = SimpleNamespace(connect_get_namespaced_pod_exec=object())
    monkeypatch.setattr(mesh, "k8s_stream", fake_stream)

    sample = mesh._collect_envoy_server_info_sample(
        core_api=core_api,
        pod_name="istio-ingressgateway-abc",
        timeout_seconds=5,
    )

    assert sample == {
        "pod": "istio-ingressgateway-abc",
        "status": Status.OK.value,
        "state": "LIVE",
        "istio_version": "1.29.1",
        "cluster_id": "dev",
        "network": "dev-network",
        "discovery_address": "istiod:15012",
        "service_cluster": "istio-ingressgateway",
    }
    assert calls == [
        {
            "method": core_api.connect_get_namespaced_pod_exec,
            "args": ("istio-ingressgateway-abc", "istio-system"),
            "kwargs": {
                "command": ["pilot-agent", "request", "GET", "server_info"],
                "container": "istio-proxy",
                "stderr": True,
                "stdin": False,
                "stdout": True,
                "tty": False,
                "_request_timeout": 5,
            },
        }
    ]


def test_mesh_envoy_server_info_maps_kubernetes_api_errors(monkeypatch) -> None:
    def fake_stream(*_args: Any, **_kwargs: Any) -> str:
        raise k8s_exceptions.ApiException(status=403, reason="Forbidden")

    monkeypatch.setattr(mesh, "k8s_stream", fake_stream)

    sample = mesh._collect_envoy_server_info_sample(
        core_api=SimpleNamespace(connect_get_namespaced_pod_exec=object()),
        pod_name="istio-ingressgateway-abc",
        timeout_seconds=5,
    )

    assert sample["status"] == Status.SKIPPED_PERMISSION.value
    assert sample["error"] == "403 Forbidden"


def test_mesh_collects_proxy_logs_with_kubernetes_client() -> None:
    calls: list[dict[str, Any]] = []

    def read_namespaced_pod_log(**kwargs: Any) -> str:
        calls.append(kwargs)
        return (
            "1710000000.1 10 10.0.0.2 TCP_TUNNEL/200 0 CONNECT "
            "gke-dev.us-central1.gke.goog:443 - HIER_DIRECT/216.239.32.27 -\n"
        )

    summary = mesh._collect_mesh_api_proxy_logs(
        core_api=SimpleNamespace(read_namespaced_pod_log=read_namespaced_pod_log),
        apps_api=SimpleNamespace(),
        proxy={
            "namespace": "istio-proxy",
            "ready_pod_names": ["squid-proxy-abc"],
        },
        timeout_seconds=45,
    )

    assert summary["status"] == Status.OK.value
    assert summary["tunnel_success_count"] == 1
    assert summary["target_hosts"] == ["gke-dev.us-central1.gke.goog:443"]
    assert calls == [
        {
            "name": "squid-proxy-abc",
            "namespace": "istio-proxy",
            "tail_lines": 80,
            "_request_timeout": 20,
        }
    ]


def test_mesh_collects_proxy_logs_by_deployment_selector() -> None:
    pod = SimpleNamespace(
        metadata=SimpleNamespace(name="squid-proxy-abc"),
        status=SimpleNamespace(phase="Running", container_statuses=[SimpleNamespace(ready=True)]),
    )
    core_api = SimpleNamespace(
        list_namespaced_pod=lambda **_kwargs: SimpleNamespace(items=[pod]),
        read_namespaced_pod_log=lambda **_kwargs: (
            "1710000000.1 10 10.0.0.2 TCP_TUNNEL/503 0 CONNECT "
            "gke-dev.us-central1.gke.goog:443 - HIER_NONE/- -\n"
        ),
    )
    apps_api = SimpleNamespace(
        read_namespaced_deployment=lambda **_kwargs: SimpleNamespace(
            spec=SimpleNamespace(selector=SimpleNamespace(match_labels={"app": "squid-proxy"}))
        )
    )

    summary = mesh._collect_mesh_api_proxy_logs(
        core_api=core_api,
        apps_api=apps_api,
        proxy={
            "namespace": "istio-proxy",
            "deployment": "squid-proxy",
        },
        timeout_seconds=45,
    )

    assert summary["status"] == Status.OK.value
    assert summary["tunnel_failure_count"] == 1


def test_mesh_attaches_proxy_logs_for_mesh_api_proxy_type() -> None:
    proxies = [
        {
            "name": "istio-proxy",
            "type": "mesh-api-proxy",
            "namespace": "istio-proxy",
            "ready_pod_names": ["istio-proxy-abc"],
        }
    ]
    core_api = SimpleNamespace(
        read_namespaced_pod_log=lambda **_kwargs: (
            "1710000000.1 10 10.0.0.2 TCP_TUNNEL/200 0 CONNECT "
            "gke-dev.us-central1.gke.goog:443 - HIER_DIRECT/216.239.32.27 -\n"
        )
    )

    mesh._attach_mesh_api_proxy_log_summaries(
        core_api=core_api,
        apps_api=SimpleNamespace(),
        proxies=proxies,
        timeout_seconds=45,
    )

    assert proxies[0]["control_plane_tunnel_logs"]["tunnel_success_count"] == 1


def test_mesh_proxy_log_collection_extends_tail_when_recent_logs_have_no_tunnels() -> None:
    calls: list[dict[str, Any]] = []

    def read_namespaced_pod_log(**kwargs: Any) -> str:
        calls.append(kwargs)
        if kwargs["tail_lines"] == mesh._PROXY_LOG_TAIL_LINES:
            return (
                "1710000000.1 0 10.0.0.2 NONE_NONE/000 0 - "
                "error:transaction-end-before-headers - HIER_NONE/- -\n"
            )
        return (
            "1710000000.1 10 10.0.0.2 TCP_TUNNEL/200 0 CONNECT "
            "gke-prod.us-central1.gke.goog:443 - HIER_DIRECT/216.239.32.27 -\n"
        )

    summary = mesh._collect_mesh_api_proxy_logs(
        core_api=SimpleNamespace(read_namespaced_pod_log=read_namespaced_pod_log),
        apps_api=SimpleNamespace(),
        proxy={
            "namespace": "istio-proxy",
            "ready_pod_names": ["istio-proxy-abc"],
        },
        timeout_seconds=45,
    )

    assert [call["tail_lines"] for call in calls] == [
        mesh._PROXY_LOG_TAIL_LINES,
        mesh._PROXY_LOG_EXTENDED_TAIL_LINES,
    ]
    assert summary["tunnel_success_count"] == 1
    assert summary["target_hosts"] == ["gke-prod.us-central1.gke.goog:443"]


def test_mesh_component_state_extracts_container_image_version() -> None:
    pod = SimpleNamespace(
        metadata=SimpleNamespace(name="istiod-abc"),
        spec=SimpleNamespace(containers=[SimpleNamespace(image="docker.io/istio/pilot:1.29.1")]),
        status=SimpleNamespace(
            phase="Running",
            container_statuses=[SimpleNamespace(ready=True)],
        ),
    )

    state = mesh._component_state([pod])

    assert state["ready_pods"] == 1
    assert state["version"] == "1.29.1"
    assert state["versions"] == ["1.29.1"]


def test_mesh_permission_error_not_reclassified_as_network(monkeypatch, tmp_path: Path) -> None:
    config = _default_config()
    request_name = _cluster_request_name(config.clusters[0])
    payloads = {
        request_name: {
            "endpoint": "10.0.0.5",
            "masterAuth": {
                "clusterCaCertificate": base64.b64encode(b"dummy-ca-cert").decode("utf-8")
            },
        }
    }

    monkeypatch.setattr(mesh, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(
        mesh,
        "build_service",
        lambda *_args, **_kwargs: _FakeContainerService(payloads),
    )
    monkeypatch.setattr(mesh, "build_cluster_manager_client", lambda _auth: None)
    monkeypatch.setattr(
        mesh,
        "resolve_clusters",
        lambda _config, _service, **_kwargs: config.clusters,
    )
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("dummy", encoding="utf-8")

    @contextmanager
    def fake_cluster_ca_file(_cert: str) -> Any:
        yield ca_file

    monkeypatch.setattr(mesh, "cluster_ca_file", fake_cluster_ca_file)
    monkeypatch.setattr(
        mesh,
        "_query_mesh_health",
        lambda **_kwargs: (_ for _ in ()).throw(k8s_exceptions.ApiException(status=403)),
    )

    result = mesh.collect(config=config)

    assert result.status == Status.SKIPPED_PERMISSION
    assert len(result.details["clusters"]) == 1
    assert result.details["clusters"][0]["status"] == Status.SKIPPED_PERMISSION.value


def test_mesh_query_squid_proxy_reports_ready_workload() -> None:
    pod = SimpleNamespace(
        metadata=SimpleNamespace(name="squid-proxy-abc", labels={"app": "squid-proxy"}),
        status=SimpleNamespace(
            phase="Running",
            container_statuses=[SimpleNamespace(ready=True)],
        ),
    )
    apps_api = SimpleNamespace(
        read_namespaced_deployment=lambda **_kwargs: SimpleNamespace(
            spec=SimpleNamespace(replicas=1),
            status=SimpleNamespace(ready_replicas=1, available_replicas=1),
        )
    )
    core_api = SimpleNamespace(
        list_namespaced_pod=lambda **_kwargs: SimpleNamespace(items=[pod]),
        read_namespaced_service=lambda **_kwargs: SimpleNamespace(
            spec=SimpleNamespace(
                type="LoadBalancer",
                ports=[SimpleNamespace(port=3128)],
            ),
            status=SimpleNamespace(
                load_balancer=SimpleNamespace(ingress=[SimpleNamespace(ip="10.115.2.43")])
            ),
        ),
        read_namespaced_endpoints=lambda **_kwargs: SimpleNamespace(
            subsets=[
                SimpleNamespace(
                    addresses=[SimpleNamespace(ip="10.115.144.31")],
                    ports=[SimpleNamespace(port=3128)],
                )
            ]
        ),
    )

    row = mesh._query_squid_proxy(
        core_api=core_api,
        apps_api=apps_api,
        check={
            "name": "squid-proxy",
            "namespace": "istio-proxy",
            "deployment": "squid-proxy",
            "service": "squid-proxy",
            "label_selector": "app=squid-proxy",
            "port": 3128,
        },
        timeout_seconds=5,
    )

    assert row["status"] == Status.OK.value
    assert row["type"] == "squid"
    assert row["ready_replicas"] == 1
    assert row["pod_ready"] == 1
    assert row["service_ports"] == ["3128"]
    assert row["endpoint_ports"] == ["3128"]
    assert row["endpoint_address_count"] == 1


def test_mesh_query_nginx_api_proxy_reports_configured_resources() -> None:
    pod = SimpleNamespace(
        metadata=SimpleNamespace(name="istio-proxy-abc", labels={"app": "istio-proxy"}),
        status=SimpleNamespace(
            phase="Running",
            container_statuses=[SimpleNamespace(ready=True)],
        ),
    )
    apps_api = SimpleNamespace(
        read_namespaced_deployment=lambda **_kwargs: SimpleNamespace(
            spec=SimpleNamespace(replicas=2),
            status=SimpleNamespace(ready_replicas=2, available_replicas=2),
        )
    )
    core_api = SimpleNamespace(
        list_namespaced_pod=lambda **_kwargs: SimpleNamespace(items=[pod]),
        read_namespaced_service=lambda **_kwargs: SimpleNamespace(
            spec=SimpleNamespace(
                type="LoadBalancer",
                ports=[SimpleNamespace(port=443)],
            ),
            status=SimpleNamespace(
                load_balancer=SimpleNamespace(ingress=[SimpleNamespace(ip="10.115.2.44")])
            ),
        ),
        read_namespaced_endpoints=lambda **_kwargs: SimpleNamespace(
            subsets=[
                SimpleNamespace(
                    addresses=[SimpleNamespace(ip="10.115.144.32")],
                    ports=[SimpleNamespace(port=8443)],
                )
            ]
        ),
        read_namespaced_config_map=lambda **_kwargs: SimpleNamespace(),
        read_namespaced_secret=lambda **_kwargs: SimpleNamespace(),
    )

    row = mesh._query_mesh_api_proxy(
        core_api=core_api,
        apps_api=apps_api,
        check={
            "name": "istio-proxy",
            "type": "nginx",
            "namespace": "istio-proxy",
            "deployment": "istio-proxy",
            "service": "istio-proxy",
            "label_selector": "app=istio-proxy",
            "service_port": 443,
            "endpoint_port": 8443,
            "configmap": "istio-nginx-template",
            "certificate_secret": "istio-proxy-cert",
            "token_secret": "istio-proxy-token",
        },
        timeout_seconds=5,
    )

    assert row["status"] == Status.OK.value
    assert row["type"] == "nginx"
    assert row["ready_replicas"] == 2
    assert row["service_ports"] == ["443"]
    assert row["endpoint_ports"] == ["8443"]
    assert row["configmap_found"] is True
    assert row["certificate_secret_found"] is True
    assert row["token_secret_found"] is True


def test_trend_metrics_collect_warns_on_sql_peak(monkeypatch) -> None:
    config = _default_config()
    monkeypatch.setattr(trend_metrics, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(trend_metrics, "build_service", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        trend_metrics,
        "build_cloud_sql_admin_service",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(trend_metrics, "build_metric_service_client", lambda _auth: None)
    monkeypatch.setattr(trend_metrics, "build_query_service_client", lambda _auth: None)
    monkeypatch.setattr(
        trend_metrics,
        "_list_sql_instances",
        lambda _service, _project: [{"name": "sql-1"}],
    )
    monkeypatch.setattr(
        trend_metrics,
        "_list_non_gke_instances",
        lambda _service, _project, **_kwargs: [{"id": "1", "name": "vm-1"}],
    )
    monkeypatch.setattr(
        trend_metrics,
        "_collect_sql_trends",
        lambda **_kwargs: [
            {
                "instance": "sql-1",
                "cpu_peak_percent": 92.0,
                "memory_peak_percent": 50.0,
                "disk_peak_percent": 40.0,
            }
        ],
    )
    monkeypatch.setattr(
        trend_metrics,
        "_collect_vm_trends",
        lambda **_kwargs: [{"instance": "vm-1", "cpu_peak_percent": 40.0}],
    )
    monkeypatch.setattr(
        trend_metrics,
        "_collect_vm_memory_trends",
        lambda **_kwargs: (
            [{"instance": "vm-1", "memory_peak_percent": 65.0}],
            "",
        ),
    )
    monkeypatch.setattr(
        trend_metrics,
        "_collect_gke_trends",
        lambda **_kwargs: (
            [{"cluster": "dev-cluster", "cpu_peak_cores": 1.0}],
            [{"cluster": "dev-cluster", "node": "node-1"}],
            [{"cluster": "dev-cluster", "namespace": "apps", "pod": "api"}],
            [{"cluster": "dev-cluster", "namespace": "apps", "cpu_peak_cores": 1.0}],
            "",
        ),
    )
    monkeypatch.setattr(
        trend_metrics,
        "_collect_redis_throughput",
        lambda **_kwargs: ([], ""),
    )
    monkeypatch.setattr(
        trend_metrics,
        "_collect_kafka_throughput",
        lambda **_kwargs: ([], ""),
    )

    result = trend_metrics.collect(config=config)

    assert result.status == Status.WARNING
    project = result.details["projects"][0]
    assert project["sql_high_utilization_count"] == 1
    assert project["vm_high_cpu_count"] == 0
    assert project["vm_high_memory_count"] == 0
    assert project["gke_cluster_utilization"][0]["cluster"] == "dev-cluster"
    assert project["gke_namespace_utilization"][0]["namespace"] == "apps"
    assert project["non_gke_vm_memory"] == [{"instance": "vm-1", "memory_peak_percent": 65.0}]


def test_trend_metrics_collect_avoids_monitoring_discovery_when_clients_cover_reads(
    monkeypatch,
) -> None:
    config = _default_config()
    built_services: list[str] = []

    def build_service(
        _auth: Any,
        service_name: str,
        _version: str,
        _timeout_seconds: int,
    ) -> object:
        built_services.append(service_name)
        if service_name == "monitoring":
            raise AssertionError("monitoring discovery should not be initialized")
        return object()

    monkeypatch.setattr(trend_metrics, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(trend_metrics, "build_service", build_service)
    monkeypatch.setattr(
        trend_metrics,
        "build_cloud_sql_admin_service",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        trend_metrics,
        "build_metric_service_client",
        lambda _auth: _FakeMetricServiceClient(),
    )
    monkeypatch.setattr(
        trend_metrics,
        "build_query_service_client",
        lambda _auth: _FakeQueryServiceClient(),
    )
    monkeypatch.setattr(trend_metrics, "build_compute_instances_client", lambda _auth: None)
    monkeypatch.setattr(trend_metrics, "build_cluster_manager_client", lambda _auth: object())
    monkeypatch.setattr(trend_metrics, "_list_sql_instances", lambda _service, _project: [])
    monkeypatch.setattr(
        trend_metrics,
        "_list_non_gke_instances",
        lambda _service, _project, **_kwargs: [],
    )
    monkeypatch.setattr(trend_metrics, "_collect_sql_trends", lambda **_kwargs: [])
    monkeypatch.setattr(trend_metrics, "_collect_vm_trends", lambda **_kwargs: [])
    monkeypatch.setattr(trend_metrics, "_collect_vm_memory_trends", lambda **_kwargs: ([], ""))
    monkeypatch.setattr(
        trend_metrics,
        "_collect_gke_trends",
        lambda **_kwargs: ([], [], [], [], ""),
    )

    def disabled_service_metric(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("disabled Redis/Kafka service metrics should not be collected")

    monkeypatch.setattr(trend_metrics, "_collect_redis_throughput", disabled_service_metric)
    monkeypatch.setattr(trend_metrics, "_collect_kafka_throughput", disabled_service_metric)
    monkeypatch.setattr(
        trend_metrics,
        "_collect_redis_resource_utilization",
        disabled_service_metric,
    )
    monkeypatch.setattr(
        trend_metrics,
        "_collect_kafka_resource_utilization",
        disabled_service_metric,
    )

    result = trend_metrics.collect(config=config, timeout_seconds=17)

    assert result.status == Status.OK
    assert "monitoring" not in built_services
    project = result.details["projects"][0]
    assert project["redis_throughput"] == []
    assert project["kafka_throughput"] == []
    assert project["redis_utilization"] == []
    assert project["kafka_utilization"] == []


def test_trend_metrics_discovers_clusters_with_cluster_manager_client(monkeypatch) -> None:
    config = _default_config()
    config.clusters = []
    config.discovery.auto_discover_clusters = True
    service = _FakeClusterListDiscoveryService(
        [{"name": "fallback-gke", "location": "us-central1"}]
    )
    client = _FakeClusterManagerListClient(
        clusters=[SimpleNamespace(name="client-gke", location="us-central1")]
    )
    captured_clusters: list[list[ClusterConfig]] = []

    _patch_trend_metrics_cluster_discovery(
        monkeypatch,
        container_service=service,
        cluster_client=client,
        captured_clusters=captured_clusters,
    )

    result = trend_metrics.collect(config=config, timeout_seconds=17)

    assert result.status == Status.OK
    project = result.details["projects"][0]
    assert project["gke_cluster_utilization"] == [{"cluster": "client-gke"}]
    assert [[cluster.name for cluster in group] for group in captured_clusters] == [["client-gke"]]
    assert client.calls == [("projects/example-dev-project/locations/-", 17)]
    assert service.list_calls == []


def test_trend_metrics_falls_back_when_cluster_manager_client_fails(monkeypatch) -> None:
    config = _default_config()
    config.clusters = []
    config.discovery.auto_discover_clusters = True
    service = _FakeClusterListDiscoveryService(
        [{"name": "fallback-gke", "location": "us-central1"}]
    )
    client = _FakeClusterManagerListClient(error=RuntimeError("client unavailable"))
    captured_clusters: list[list[ClusterConfig]] = []

    _patch_trend_metrics_cluster_discovery(
        monkeypatch,
        container_service=service,
        cluster_client=client,
        captured_clusters=captured_clusters,
    )

    result = trend_metrics.collect(config=config, timeout_seconds=17)

    assert result.status == Status.OK
    project = result.details["projects"][0]
    assert project["gke_cluster_utilization"] == [{"cluster": "fallback-gke"}]
    assert [[cluster.name for cluster in group] for group in captured_clusters] == [
        ["fallback-gke"]
    ]
    assert client.calls == [("projects/example-dev-project/locations/-", 17)]
    assert service.list_calls == [{"parent": "projects/example-dev-project/locations/-"}]


def _patch_trend_metrics_cluster_discovery(
    monkeypatch,
    *,
    container_service: Any,
    cluster_client: Any,
    captured_clusters: list[list[ClusterConfig]],
) -> None:
    def build_service(
        _auth: Any,
        service_name: str,
        _version: str,
        _timeout_seconds: int,
    ) -> Any:
        return container_service if service_name == "container" else object()

    def collect_gke_trends(
        **kwargs: Any,
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        str,
    ]:
        clusters = list(kwargs["clusters"])
        captured_clusters.append(clusters)
        return (
            [{"cluster": clusters[0].name}] if clusters else [],
            [],
            [],
            [],
            "",
        )

    monkeypatch.setattr(trend_metrics, "get_auth_bundle", lambda **_kwargs: _auth_bundle())
    monkeypatch.setattr(trend_metrics, "build_service", build_service)
    monkeypatch.setattr(
        trend_metrics,
        "build_cloud_sql_admin_service",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(trend_metrics, "build_metric_service_client", lambda _auth: None)
    monkeypatch.setattr(trend_metrics, "build_query_service_client", lambda _auth: None)
    monkeypatch.setattr(trend_metrics, "build_cluster_manager_client", lambda _auth: cluster_client)
    monkeypatch.setattr(trend_metrics, "_list_sql_instances", lambda _service, _project: [])
    monkeypatch.setattr(
        trend_metrics,
        "_list_non_gke_instances",
        lambda _service, _project, **_kwargs: [],
    )
    monkeypatch.setattr(trend_metrics, "_collect_sql_trends", lambda **_kwargs: [])
    monkeypatch.setattr(trend_metrics, "_collect_vm_trends", lambda **_kwargs: [])
    monkeypatch.setattr(trend_metrics, "_collect_vm_memory_trends", lambda **_kwargs: ([], ""))
    monkeypatch.setattr(trend_metrics, "_collect_gke_trends", collect_gke_trends)
    monkeypatch.setattr(trend_metrics, "_collect_redis_throughput", lambda **_kwargs: ([], ""))
    monkeypatch.setattr(trend_metrics, "_collect_kafka_throughput", lambda **_kwargs: ([], ""))
    monkeypatch.setattr(
        trend_metrics,
        "_collect_redis_resource_utilization",
        lambda **_kwargs: ([], ""),
    )
    monkeypatch.setattr(
        trend_metrics,
        "_collect_kafka_resource_utilization",
        lambda **_kwargs: ([], ""),
    )


def test_trend_metrics_lists_non_gke_instances_with_compute_client() -> None:
    service = _FakeComputeInstancesDiscoveryService(
        aggregated_items={
            "zones/us-central1-a": {
                "instances": [
                    {"id": "fallback-1", "name": "fallback-vm"},
                ]
            }
        }
    )
    client = _FakeComputeInstancesClient(
        aggregated_items=[
            (
                "zones/us-central1-a",
                {
                    "instances": [
                        {"id": "1", "name": "app-vm"},
                        {"id": "2", "name": "gke-node-vm"},
                    ]
                },
            )
        ]
    )

    rows = trend_metrics._list_non_gke_instances(
        service,
        "example-dev-project",
        instances_client=client,
        timeout_seconds=17,
    )

    assert rows == [{"id": "1", "name": "app-vm"}]
    assert client.aggregated_calls == [({"project": "example-dev-project", "max_results": 500}, 17)]
    assert service.aggregated_calls == []


def test_trend_metrics_falls_back_to_discovery_when_compute_client_fails() -> None:
    service = _FakeComputeInstancesDiscoveryService(
        aggregated_items={
            "zones/us-central1-a": {
                "instances": [
                    {"id": "fallback-1", "name": "fallback-vm"},
                    {"id": "fallback-2", "name": "gke-fallback-node"},
                ]
            }
        }
    )
    client = _FakeComputeInstancesClient(error=RuntimeError("client unavailable"))

    rows = trend_metrics._list_non_gke_instances(
        service,
        "example-dev-project",
        instances_client=client,
        timeout_seconds=17,
    )

    assert rows == [{"id": "fallback-1", "name": "fallback-vm"}]
    assert client.aggregated_calls == [({"project": "example-dev-project", "max_results": 500}, 17)]
    assert service.aggregated_calls == [("example-dev-project", 500)]


def test_trend_metrics_collect_redis_throughput(monkeypatch) -> None:
    monkeypatch.setattr(
        trend_metrics,
        "_resolve_metric_type",
        lambda **_kwargs: "redis.googleapis.com/stats/network_traffic",
    )
    monkeypatch.setattr(
        trend_metrics,
        "_fetch_time_series",
        lambda **_kwargs: [
            {
                "resource": {"labels": {"instance_id": "redis-1"}},
                "metric": {"labels": {"direction": "in"}},
                "points": [{"value": {"doubleValue": 500.0}}, {"value": {"doubleValue": 1000.0}}],
            }
        ],
    )

    rows, error = trend_metrics._collect_redis_throughput(
        monitoring=object(),
        project="example-dev-project",
        start_text="2026-05-01T00:00:00Z",
        end_text="2026-05-08T00:00:00Z",
    )

    assert error == ""
    assert rows[0]["instance"] == "redis-1"
    assert rows[0]["direction"] == "in"
    assert rows[0]["bytes_per_second_peak"] == 1000.0


def test_trend_metrics_collect_kafka_throughput(monkeypatch) -> None:
    def _resolve(**kwargs: Any) -> str:
        suffix = str(kwargs["suffix"])
        if suffix == "cluster_byte_in_count":
            return "managedkafka.googleapis.com/Cluster/cluster_byte_in_count"
        if suffix == "cluster_byte_out_count":
            return "managedkafka.googleapis.com/Cluster/cluster_byte_out_count"
        if suffix == "cluster_message_in_count":
            return "managedkafka.googleapis.com/Cluster/cluster_message_in_count"
        return ""

    def _series(**kwargs: Any) -> list[dict[str, Any]]:
        metric_type = str(kwargs["metric_type"])
        if metric_type.endswith("cluster_byte_in_count"):
            value = 1024.0
        elif metric_type.endswith("cluster_byte_out_count"):
            value = 2048.0
        else:
            value = 20.0
        return [
            {
                "resource": {"labels": {"cluster_id": "example-dev-kafka"}},
                "metric": {"labels": {}},
                "points": [{"value": {"doubleValue": value}}],
            }
        ]

    monkeypatch.setattr(trend_metrics, "_resolve_metric_type", _resolve)
    monkeypatch.setattr(trend_metrics, "_fetch_time_series", _series)

    rows, error = trend_metrics._collect_kafka_throughput(
        monitoring=object(),
        project="example-dev-project",
        start_text="2026-05-01T00:00:00Z",
        end_text="2026-05-08T00:00:00Z",
    )

    assert error == ""
    assert rows[0]["cluster"] == "example-dev-kafka"
    assert rows[0]["bytes_in_peak"] == 1024.0
    assert rows[0]["bytes_out_peak"] == 2048.0
    assert rows[0]["messages_in_peak"] == 20.0


def test_trend_metrics_collect_kafka_utilization_maps_unlabeled_cluster_metrics(
    monkeypatch,
) -> None:
    def _resolve(**kwargs: Any) -> str:
        suffixes = tuple(kwargs["suffixes"])
        if suffixes == ("cpu/core_usage_time",):
            return "managedkafka.googleapis.com/cpu/core_usage_time"
        if suffixes == ("cpu/limit",):
            return "managedkafka.googleapis.com/cpu/limit"
        return ""

    def _series(**kwargs: Any) -> list[dict[str, Any]]:
        metric_type = str(kwargs["metric_type"])
        if metric_type.endswith("core_usage_time"):
            return [
                {
                    "resource": {
                        "labels": {
                            "cluster_id": "",
                            "location": "us-central1",
                            "project_id": "example-dev-project",
                        }
                    },
                    "metric": {"labels": {"broker_index": "0"}},
                    "points": [{"value": {"doubleValue": 3.0}}],
                }
            ]
        if metric_type.endswith("cpu/limit"):
            return [
                {
                    "resource": {
                        "labels": {
                            "cluster_id": "example-dev-kafka",
                            "location": "us-central1",
                            "project_id": "example-dev-project",
                        }
                    },
                    "metric": {"labels": {"broker_index": "0"}},
                    "points": [{"value": {"doubleValue": 6.0}}],
                }
            ]
        return []

    monkeypatch.setattr(trend_metrics, "_resolve_metric_type_candidates", _resolve)
    monkeypatch.setattr(trend_metrics, "_fetch_time_series", _series)

    rows, error = trend_metrics._collect_kafka_resource_utilization(
        config=_default_config(),
        monitoring=object(),
        project="example-dev-project",
        start_text="2026-05-01T00:00:00Z",
        end_text="2026-05-08T00:00:00Z",
        known_clusters_by_location={"us-central1": ["example-dev-kafka"]},
    )

    assert error == ""
    assert rows == [
        {
            "cluster": "example-dev-kafka",
            "status": Status.OK.value,
            "broker_cpu_peak_percent": 50.0,
            "broker_memory_peak_percent": 0.0,
            "broker_disk_peak_percent": 0.0,
            "under_replicated_partitions_peak": 0.0,
            "offline_partitions_peak": 0.0,
            "consumer_lag_peak": 0.0,
        }
    ]


def test_trend_metrics_keeps_unlabeled_kafka_metrics_unknown_when_ambiguous() -> None:
    cluster = trend_metrics._kafka_metric_cluster_name(
        resource_labels={"cluster_id": "", "location": "us-central1"},
        metric_labels={"broker_index": "0"},
        known_clusters_by_location={"us-central1": ["kafka-a", "kafka-b"]},
    )

    assert cluster == "unknown-cluster"


def test_trend_metrics_collect_vm_cpu_queries_each_instance(monkeypatch) -> None:
    filters: list[str] = []

    def _series(**kwargs: Any) -> list[dict[str, Any]]:
        filter_suffix = str(kwargs.get("filter_suffix", ""))
        filters.append(filter_suffix)
        if 'resource.labels.instance_id="2"' in filter_suffix:
            return [
                {
                    "resource": {"labels": {"instance_id": "2"}},
                    "points": [
                        {"value": {"doubleValue": 0.021}},
                        {"value": {"doubleValue": 0.005}},
                    ],
                }
            ]
        return []

    monkeypatch.setattr(trend_metrics, "_fetch_time_series", _series)

    rows = trend_metrics._collect_vm_trends(
        monitoring=object(),
        project="example-dev-project",
        instances=[
            {"id": "1", "name": "vm-missing"},
            {"id": "2", "name": "vm-active"},
        ],
        start_text="2026-05-01T00:00:00Z",
        end_text="2026-05-08T00:00:00Z",
    )

    assert filters == [
        'resource.labels.instance_id="2"',
        'resource.labels.instance_id="1"',
    ]
    assert [row["instance"] for row in rows] == ["vm-active", "vm-missing"]
    assert rows[0]["telemetry_status"] == "ok"
    assert rows[0]["cpu_peak_percent"] == 2.1
    assert rows[0]["cpu_min_percent"] == 0.5
    assert rows[0]["idle_capacity_at_peak_percent"] == 97.9
    assert rows[0]["activity_state"] == "active"
    assert rows[1]["telemetry_status"] == "missing"
    assert rows[1]["activity_state"] == "unknown"


def test_trend_metrics_collect_vm_memory_uses_ops_agent_metric(monkeypatch) -> None:
    monkeypatch.setattr(
        trend_metrics,
        "_fetch_time_series",
        lambda **_kwargs: [
            {
                "resource": {"labels": {"instance_id": "1"}},
                "metric": {"labels": {"state": "used"}},
                "points": [{"value": {"doubleValue": 64.5}}],
            }
        ],
    )

    rows, error = trend_metrics._collect_vm_memory_trends(
        monitoring=object(),
        project="example-dev-project",
        instances=[{"id": "1", "name": "vm-1"}],
        start_text="2026-05-01T00:00:00Z",
        end_text="2026-05-08T00:00:00Z",
    )

    assert error == ""
    assert rows[0]["instance"] == "vm-1"
    assert rows[0]["telemetry_status"] == "ok"
    assert rows[0]["memory_peak_percent"] == 64.5


def test_trend_metrics_collect_vm_memory_reports_missing_ops_agent_rows(monkeypatch) -> None:
    monkeypatch.setattr(trend_metrics, "_fetch_time_series", lambda **_kwargs: [])

    rows, error = trend_metrics._collect_vm_memory_trends(
        monitoring=object(),
        project="example-dev-project",
        instances=[
            {"id": "1", "name": "vm-1"},
            {"id": "2", "name": "vm-2"},
        ],
        start_text="2026-05-01T00:00:00Z",
        end_text="2026-05-08T00:00:00Z",
    )

    assert "missing for 2 of 2" in error
    assert [row["instance"] for row in rows] == ["vm-1", "vm-2"]
    assert {row["telemetry_status"] for row in rows} == {"missing"}


def test_trend_metrics_collect_gke_cluster_trends(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    pod_calls: list[dict[str, Any]] = []

    def _series(**kwargs: Any) -> list[dict[str, Any]]:
        calls.append(kwargs)
        metric_type = str(kwargs["metric_type"])
        if metric_type.endswith("container/cpu/core_usage_time"):
            return [
                {
                    "resource": {
                        "labels": {
                            "cluster_name": "dev-cluster",
                        }
                    },
                    "points": [
                        {
                            "interval": {"endTime": "2026-05-01T00:00:00Z"},
                            "value": {"doubleValue": 0.5},
                        }
                    ],
                }
            ]
        if metric_type.endswith("container/memory/used_bytes"):
            return [
                {
                    "resource": {
                        "labels": {
                            "cluster_name": "dev-cluster",
                        }
                    },
                    "points": [
                        {
                            "interval": {"endTime": "2026-05-01T00:00:00Z"},
                            "value": {"doubleValue": 268435456.0},
                        }
                    ],
                }
            ]
        if metric_type.endswith("node/cpu/allocatable_utilization"):
            return [
                {
                    "resource": {
                        "labels": {
                            "cluster_name": "dev-cluster",
                            "node_name": "node-1",
                        }
                    },
                    "points": [
                        {
                            "interval": {"endTime": "2026-05-01T00:00:00Z"},
                            "value": {"doubleValue": 0.75},
                        }
                    ],
                }
            ]
        if metric_type.endswith("node/memory/allocatable_utilization"):
            return [
                {
                    "resource": {
                        "labels": {
                            "cluster_name": "dev-cluster",
                            "node_name": "node-1",
                        }
                    },
                    "points": [
                        {
                            "interval": {"endTime": "2026-05-01T00:00:00Z"},
                            "value": {"doubleValue": 0.80},
                        }
                    ],
                }
            ]
        return []

    def _pod_top(**kwargs: Any) -> list[dict[str, Any]]:
        pod_calls.append(kwargs)
        cluster_names = list(kwargs["cluster_names"])
        return [
            {
                "cluster": str(cluster_names[0]),
                "namespace": "apps",
                "pod": "api",
                "cpu_peak_cores": 0.5,
                "memory_peak_bytes": 268435456.0,
                "window_duration": str(kwargs["duration"]),
                "alignment_period": str(kwargs["alignment_period"]),
                "cpu_aggregation": "max_daily_rate",
                "memory_aggregation": "full_window_max",
            }
        ]

    monkeypatch.setattr(trend_metrics, "_fetch_time_series", _series)
    monkeypatch.setattr(trend_metrics, "_collect_gke_pod_top_trends", _pod_top)

    cluster, nodes, pods, namespaces = trend_metrics._collect_gke_cluster_trends(
        monitoring=object(),
        project="example-dev-project",
        cluster_name="dev-cluster",
        start_text="2026-05-01T00:00:00Z",
        end_text="2026-05-08T00:00:00Z",
    )

    assert cluster["cpu_peak_cores"] == 0.5
    assert cluster["memory_peak_bytes"] == 268435456.0
    assert cluster["max_node_cpu_idle_capacity_at_peak_percent"] == 25.0
    assert nodes[0]["cpu_allocatable_peak_percent"] == 75.0
    assert nodes[0]["cpu_idle_capacity_at_peak_percent"] == 25.0
    assert nodes[0]["memory_allocatable_peak_percent"] == 80.0
    assert pods[0]["namespace"] == "apps"
    assert pods[0]["cpu_aggregation"] == "max_daily_rate"
    assert namespaces[0]["namespace"] == "default"
    assert namespaces[0]["cpu_peak_cores"] == 0.5
    assert namespaces[0]["memory_peak_bytes"] == 268435456.0
    assert [
        {
            "project": call["project"],
            "cluster_names": call["cluster_names"],
            "alignment_period": call["alignment_period"],
            "duration": call["duration"],
        }
        for call in pod_calls
    ] == [
        {
            "project": "example-dev-project",
            "cluster_names": ["dev-cluster"],
            "alignment_period": "604800s",
            "duration": "7d",
        }
    ]
    assert any(
        call["metric_type"].endswith("node/cpu/allocatable_utilization")
        and call["reducer"] == "REDUCE_MAX"
        and call["alignment_period"] == "604800s"
        and call["group_by_fields"] == ["resource.labels.cluster_name", "resource.labels.node_name"]
        for call in calls
    )


def test_trend_metrics_fetch_time_series_caps_pages() -> None:
    class _FakeTimeSeries:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def list(self, **kwargs: Any) -> _FakeTimeSeries:
            self.calls.append(kwargs)
            return self

        def execute(self) -> dict[str, Any]:
            return {
                "timeSeries": [
                    {
                        "resource": {"labels": {"pod_name": f"pod-{len(self.calls)}"}},
                        "points": [{"value": {"doubleValue": float(len(self.calls))}}],
                    }
                ],
                "nextPageToken": f"page-{len(self.calls)}",
            }

    class _FakeProjects:
        def __init__(self, time_series: _FakeTimeSeries) -> None:
            self._time_series = time_series

        def timeSeries(self) -> _FakeTimeSeries:  # noqa: N802
            return self._time_series

    class _FakeMonitoring:
        def __init__(self, time_series: _FakeTimeSeries) -> None:
            self._time_series = time_series

        def projects(self) -> _FakeProjects:
            return _FakeProjects(self._time_series)

    time_series = _FakeTimeSeries()
    rows = trend_metrics._fetch_time_series(
        monitoring=_FakeMonitoring(time_series),
        project="example-dev-project",
        metric_type="kubernetes.io/container/cpu/core_usage_time",
        start_text="2026-05-01T00:00:00Z",
        end_text="2026-05-08T00:00:00Z",
        aligner="ALIGN_RATE",
        alignment_period="86400s",
        reducer="REDUCE_SUM",
        group_by_fields=["resource.labels.namespace_name", "resource.labels.pod_name"],
        max_series=100,
        max_pages=3,
    )

    assert len(rows) == 3
    assert len(time_series.calls) == 3
    assert time_series.calls[0]["aggregation_crossSeriesReducer"] == "REDUCE_SUM"
    assert time_series.calls[0]["aggregation_groupByFields"] == [
        "resource.labels.namespace_name",
        "resource.labels.pod_name",
    ]


def test_trend_metrics_fetch_time_series_uses_metric_service_client() -> None:
    client = _FakeMetricServiceClient(
        time_series_pages=[
            [
                {
                    "resource": {"labels": {"pod_name": "pod-a"}},
                    "points": [{"value": {"doubleValue": 1.0}}],
                }
            ],
            [
                {
                    "resource": {"labels": {"pod_name": "pod-b"}},
                    "points": [{"value": {"doubleValue": 2.0}}],
                }
            ],
        ]
    )
    monitoring_clients = trend_metrics._MonitoringClients(
        service=object(),
        metric_client=client,
        query_client=None,
        timeout_seconds=17,
    )

    rows = trend_metrics._fetch_time_series(
        monitoring=monitoring_clients,
        project="example-dev-project",
        metric_type="kubernetes.io/container/cpu/core_usage_time",
        start_text="2026-05-01T00:00:00Z",
        end_text="2026-05-08T00:00:00Z",
        aligner="ALIGN_RATE",
        alignment_period="86400s",
        reducer="REDUCE_SUM",
        group_by_fields=["resource.labels.namespace_name", "resource.labels.pod_name"],
        max_series=10,
        max_pages=1,
        page_size=50,
    )

    request, timeout = client.time_series_calls[0]
    assert timeout == 17
    assert len(rows) == 1
    assert request.name == "projects/example-dev-project"
    assert request.filter == 'metric.type="kubernetes.io/container/cpu/core_usage_time"'
    assert request.page_size == 50
    assert request.aggregation.alignment_period.total_seconds() == 86400
    assert list(request.aggregation.group_by_fields) == [
        "resource.labels.namespace_name",
        "resource.labels.pod_name",
    ]


def test_trend_metrics_list_metric_descriptors_uses_metric_service_client() -> None:
    client = _FakeMetricServiceClient(
        metric_descriptors=[
            {"type": "managedkafka.googleapis.com/cpu/core_usage_time"},
            {"type": "managedkafka.googleapis.com/memory/usage"},
        ]
    )
    monitoring_clients = trend_metrics._MonitoringClients(
        service=object(),
        metric_client=client,
        query_client=None,
        timeout_seconds=23,
    )

    descriptors = trend_metrics._list_metric_descriptors(
        monitoring=monitoring_clients,
        project="example-dev-project",
        prefix="managedkafka.googleapis.com/",
    )

    request, timeout = client.metric_descriptor_calls[0]
    assert timeout == 23
    assert descriptors == [
        "managedkafka.googleapis.com/cpu/core_usage_time",
        "managedkafka.googleapis.com/memory/usage",
    ]
    assert request.name == "projects/example-dev-project"
    assert request.filter == 'metric.type = starts_with("managedkafka.googleapis.com/")'


def test_trend_metrics_mql_pod_values_parses_descriptor_response() -> None:
    response = {
        "timeSeriesDescriptor": {
            "labelDescriptors": [
                {"key": "resource.cluster_name"},
                {"key": "resource.namespace_name"},
                {"key": "resource.pod_name"},
            ]
        },
        "timeSeriesData": [
            {
                "labelValues": [
                    {"stringValue": "dev-cluster"},
                    {"stringValue": "apps"},
                    {"stringValue": "api-123"},
                ],
                "pointData": [
                    {"values": [{"doubleValue": 0.42}]},
                    {"values": [{"doubleValue": 0.75}]},
                ],
            }
        ],
    }

    values = trend_metrics._mql_pod_values(response)

    assert values == {("dev-cluster", "apps", "api-123"): 0.75}
    assert "| every 1h" in trend_metrics._gke_pod_memory_peak_query("7d")


def test_trend_metrics_query_gke_pod_top_mql_uses_query_service_client() -> None:
    client = _FakeQueryServiceClient(
        pages=[
            {
                "timeSeriesDescriptor": {
                    "labelDescriptors": [
                        {"key": "resource.cluster_name"},
                        {"key": "resource.namespace_name"},
                        {"key": "resource.pod_name"},
                    ]
                },
                "timeSeriesData": [
                    {
                        "labelValues": [
                            {"stringValue": "dev-cluster"},
                            {"stringValue": "apps"},
                            {"stringValue": "api-123"},
                        ],
                        "pointData": [
                            {"values": [{"doubleValue": 0.42}]},
                            {"values": [{"doubleValue": 0.75}]},
                        ],
                    }
                ],
            }
        ]
    )

    def unexpected_service() -> object:
        raise AssertionError("monitoring discovery should not be used")

    monitoring_clients = trend_metrics._MonitoringClients(
        service=unexpected_service,
        metric_client=None,
        query_client=client,
        timeout_seconds=17,
    )

    values = trend_metrics._query_gke_pod_top_mql(
        monitoring=monitoring_clients,
        project="example-dev-project",
        query="fetch k8s_container",
    )

    request, timeout = client.query_calls[0]
    assert timeout == 17
    assert request == {
        "name": "projects/example-dev-project",
        "query": "fetch k8s_container",
    }
    assert values == {("dev-cluster", "apps", "api-123"): 0.75}


def test_trend_metrics_pod_top_marks_missing_metric_values(monkeypatch) -> None:
    def _query(**kwargs: Any) -> dict[tuple[str, str, str], float]:
        query = str(kwargs["query"])
        if "core_usage_time" in query:
            return {("dev-cluster", "apps", "cpu-heavy"): 0.75}
        if "memory/used_bytes" in query:
            return {("dev-cluster", "apps", "memory-heavy"): 536870912.0}
        return {}

    monkeypatch.setattr(trend_metrics, "_query_gke_pod_top_mql", _query)

    rows = trend_metrics._collect_gke_pod_top_trends(
        monitoring=object(),
        project="example-dev-project",
        cluster_names=["dev-cluster"],
        alignment_period="604800s",
        duration="7d",
    )

    rows_by_pod = {str(row["pod"]): row for row in rows}
    assert rows_by_pod["cpu-heavy"]["cpu_peak_cores"] == 0.75
    assert rows_by_pod["cpu-heavy"]["memory_peak_bytes"] is None
    assert rows_by_pod["cpu-heavy"]["cpu_peak_observed"] is True
    assert rows_by_pod["cpu-heavy"]["memory_peak_observed"] is False
    assert rows_by_pod["memory-heavy"]["cpu_peak_cores"] is None
    assert rows_by_pod["memory-heavy"]["memory_peak_bytes"] == 536870912.0
    assert rows_by_pod["memory-heavy"]["cpu_peak_observed"] is False
    assert rows_by_pod["memory-heavy"]["memory_peak_observed"] is True


def test_trend_metrics_gke_pod_budget_is_per_cluster(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    pod_cluster_batches: list[list[str]] = []

    def _cluster_from_filter(filter_suffix: str) -> str:
        if 'cluster_name="cluster-a"' in filter_suffix:
            return "cluster-a"
        if 'cluster_name="cluster-b"' in filter_suffix:
            return "cluster-b"
        return ""

    def _series(**kwargs: Any) -> list[dict[str, Any]]:
        calls.append(kwargs)
        metric_type = str(kwargs["metric_type"])
        cluster = _cluster_from_filter(str(kwargs.get("filter_suffix", "")))
        if not cluster and metric_type.endswith("container/cpu/core_usage_time"):
            return [
                {
                    "resource": {"labels": {"cluster_name": "cluster-a"}},
                    "points": [{"value": {"doubleValue": 1.0}}],
                },
                {
                    "resource": {"labels": {"cluster_name": "cluster-b"}},
                    "points": [{"value": {"doubleValue": 2.0}}],
                },
            ]
        if not cluster and metric_type.endswith("container/memory/used_bytes"):
            return [
                {
                    "resource": {"labels": {"cluster_name": "cluster-a"}},
                    "points": [{"value": {"doubleValue": 100.0}}],
                },
                {
                    "resource": {"labels": {"cluster_name": "cluster-b"}},
                    "points": [{"value": {"doubleValue": 200.0}}],
                },
            ]
        if not cluster:
            return []
        if metric_type.endswith("container/cpu/core_usage_time"):
            return [
                {
                    "resource": {
                        "labels": {
                            "cluster_name": cluster,
                            "namespace_name": "apps",
                            "pod_name": f"{cluster}-pod",
                        }
                    },
                    "points": [{"value": {"doubleValue": 0.5}}],
                }
            ]
        if metric_type.endswith("container/memory/used_bytes"):
            return [
                {
                    "resource": {
                        "labels": {
                            "cluster_name": cluster,
                            "namespace_name": "apps",
                            "pod_name": f"{cluster}-pod",
                        }
                    },
                    "points": [{"value": {"doubleValue": 128.0}}],
                }
            ]
        if metric_type.endswith("node/cpu/allocatable_utilization"):
            return [
                {
                    "resource": {"labels": {"cluster_name": cluster, "node_name": "node-1"}},
                    "points": [{"value": {"doubleValue": 0.5}}],
                }
            ]
        if metric_type.endswith("node/memory/allocatable_utilization"):
            return [
                {
                    "resource": {"labels": {"cluster_name": cluster, "node_name": "node-1"}},
                    "points": [{"value": {"doubleValue": 0.6}}],
                }
            ]
        return []

    def _pod_top(**kwargs: Any) -> list[dict[str, Any]]:
        clusters = list(kwargs["cluster_names"])
        pod_cluster_batches.append(clusters)
        return [
            {
                "cluster": cluster,
                "namespace": "apps",
                "pod": f"{cluster}-pod",
                "cpu_peak_cores": 0.5,
                "memory_peak_bytes": 128.0,
                "window_duration": str(kwargs["duration"]),
                "alignment_period": str(kwargs["alignment_period"]),
                "cpu_aggregation": "max_daily_rate",
                "memory_aggregation": "full_window_max",
            }
            for cluster in clusters
        ]

    monkeypatch.setattr(trend_metrics, "_fetch_time_series", _series)
    monkeypatch.setattr(trend_metrics, "_collect_gke_pod_top_trends", _pod_top)

    clusters, _nodes, pods, namespaces, error = trend_metrics._collect_gke_trends(
        monitoring=object(),
        project="example-dev-project",
        clusters=[SimpleNamespace(name="cluster-a"), SimpleNamespace(name="cluster-b")],
        start_text="2026-05-01T00:00:00Z",
        end_text="2026-05-08T00:00:00Z",
    )

    assert error == ""
    assert {item["cluster"] for item in clusters} == {"cluster-a", "cluster-b"}
    assert {item["cluster"] for item in pods} == {"cluster-a", "cluster-b"}
    assert {item["cluster"] for item in namespaces} == {"cluster-a", "cluster-b"}
    assert pod_cluster_batches == [["cluster-a", "cluster-b"]]


def test_trend_metrics_window_days_from_config() -> None:
    config = _default_config()
    config.time_windows.trend_days = 14

    assert trend_metrics._trend_window_days(config) == 14


def test_load_balancer_backend_health_warning() -> None:
    service = _FakeComputeService(
        {
            "healthStatus": [
                {"healthState": "HEALTHY"},
                {"healthState": "UNHEALTHY"},
            ]
        }
    )

    health = services._load_balancer_backend_health(
        service=service,
        project="example-dev-project",
        backend_service="backend-a",
        scope="global",
        region="",
        backends=[{"group": "projects/p/zones/z/instanceGroups/ig-a"}],
    )

    assert health["status"] == Status.WARNING.value
    assert health["counts"]["HEALTHY"] == 1
    assert health["counts"]["UNHEALTHY"] == 1
