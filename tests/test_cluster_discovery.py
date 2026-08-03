from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from opsbrief.cluster_discovery import resolve_clusters
from opsbrief.config import ClusterConfig, DiscoveryConfig, EnvConfig, ServicesConfig


class _FakeRequest:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def execute(self) -> dict[str, Any]:
        return self._payload


class _FakeClustersApi:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def list(
        self,
        parent: str,
        pageSize: int | None = None,  # noqa: N803
        pageToken: str | None = None,  # noqa: N803
    ) -> _FakeRequest:
        _ = parent
        _ = pageSize
        _ = pageToken
        return _FakeRequest(self._payload)

    def list_next(self, previous_request: _FakeRequest, previous_response: dict[str, Any]) -> None:
        _ = previous_request
        _ = previous_response
        return None


class _FakeContainerService:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._clusters = _FakeClustersApi(payload)

    def projects(self) -> _FakeContainerService:
        return self

    def locations(self) -> _FakeContainerService:
        return self

    def clusters(self) -> _FakeClustersApi:
        return self._clusters


class _FakeClusterManagerClient:
    def __init__(
        self,
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


def test_resolve_clusters_uses_configured_when_auto_disabled() -> None:
    config = EnvConfig(
        environment="dev",
        default_region="us-central1",
        projects={"component": "prj-a"},
        collectors={},
        clusters=[ClusterConfig(name="apps", project="prj-a", region="us-central1")],
        services=ServicesConfig(),
        discovery=DiscoveryConfig(auto_discover_clusters=False),
    )
    service = _FakeContainerService({"clusters": []})

    clusters = resolve_clusters(config, service)

    assert len(clusters) == 1
    assert clusters[0].name == "apps"


def test_resolve_clusters_discovers_when_not_configured() -> None:
    config = EnvConfig(
        environment="dev",
        default_region="us-central1",
        projects={"component": "prj-a"},
        collectors={},
        clusters=[],
        services=ServicesConfig(),
        discovery=DiscoveryConfig(auto_discover_clusters=True),
    )
    service = _FakeContainerService(
        {
            "clusters": [
                {"name": "apps", "location": "us-central1"},
                {"name": "bff", "location": "us-central1"},
            ]
        }
    )

    clusters = resolve_clusters(config, service)

    assert len(clusters) == 2
    assert {item.name for item in clusters} == {"apps", "bff"}


def test_resolve_clusters_discovers_with_cluster_manager_client() -> None:
    config = EnvConfig(
        environment="dev",
        default_region="us-central1",
        projects={"component": "prj-a"},
        collectors={},
        clusters=[],
        services=ServicesConfig(),
        discovery=DiscoveryConfig(auto_discover_clusters=True),
    )
    service = _FakeContainerService({"clusters": []})
    client = _FakeClusterManagerClient(
        [
            SimpleNamespace(name="apps", location="us-central1"),
            {"name": "bff", "location": "us-central1"},
            SimpleNamespace(name="zonal", zone="us-central1-a"),
            {"name": "apps", "location": "us-central1"},
            {"name": "", "location": "us-central1"},
        ]
    )

    clusters = resolve_clusters(
        config,
        service,
        cluster_client=client,
        timeout_seconds=17,
    )

    assert {(item.name, item.region) for item in clusters} == {
        ("apps", "us-central1"),
        ("bff", "us-central1"),
        ("zonal", "us-central1-a"),
    }
    assert client.calls == [("projects/prj-a/locations/-", 17)]


def test_resolve_clusters_falls_back_to_container_service_when_client_fails() -> None:
    config = EnvConfig(
        environment="dev",
        default_region="us-central1",
        projects={"component": "prj-a"},
        collectors={},
        clusters=[],
        services=ServicesConfig(),
        discovery=DiscoveryConfig(auto_discover_clusters=True),
    )
    service = _FakeContainerService(
        {
            "clusters": [
                {"name": "apps", "location": "us-central1"},
            ]
        }
    )
    client = _FakeClusterManagerClient(error=RuntimeError("client unavailable"))

    clusters = resolve_clusters(
        config,
        service,
        cluster_client=client,
        timeout_seconds=17,
    )

    assert len(clusters) == 1
    assert clusters[0].name == "apps"
    assert client.calls == [("projects/prj-a/locations/-", 17)]
