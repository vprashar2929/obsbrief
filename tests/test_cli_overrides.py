from __future__ import annotations

import time

import pytest

from opsbrief.cli import (
    _apply_runtime_overrides,
    _build_parser,
    _effective_timeout_seconds,
    _run_collector_with_timeout,
)
from opsbrief.config import DiscoveryConfig, EnvConfig, ServicesConfig, TimeWindowsConfig
from opsbrief.models import CheckResult, Status


def _successful_collector(_config: EnvConfig, **kwargs: object) -> CheckResult:
    return CheckResult(
        collector="example",
        status=Status.OK,
        summary="ok",
        details={"timeout_seconds": kwargs.get("timeout_seconds")},
    )


def _slow_collector(_config: EnvConfig, **_kwargs: object) -> CheckResult:
    time.sleep(5)
    return CheckResult(
        collector="slow",
        status=Status.OK,
        summary="should not complete",
    )


def _base_config() -> EnvConfig:
    return EnvConfig(
        environment="runtime",
        default_region="",
        projects={},
        collectors={},
        clusters=[],
        services=ServicesConfig(),
        discovery=DiscoveryConfig(),
        time_windows=TimeWindowsConfig(),
    )


def test_runtime_overrides_project_region_and_cluster_names() -> None:
    parser = _build_parser()
    args = parser.parse_args(
        [
            "preflight",
            "--config",
            "config/runtime.yaml",
            "--project",
            "prj-a",
            "--region",
            "us-central1",
            "--cluster",
            "apps",
            "--cluster",
            "bff",
        ]
    )
    config = _base_config()

    _apply_runtime_overrides(config=config, args=args, parser=parser)

    assert list(config.projects.values()) == ["prj-a"]
    assert [item.name for item in config.clusters] == ["apps", "bff"]
    assert all(item.project == "prj-a" for item in config.clusters)
    assert all(item.region == "us-central1" for item in config.clusters)
    assert config.discovery.auto_discover_clusters is False


def test_runtime_overrides_cluster_triplet() -> None:
    parser = _build_parser()
    args = parser.parse_args(
        [
            "preflight",
            "--config",
            "config/runtime.yaml",
            "--cluster",
            "example-test-project/us-central1/example-test-apps-gke",
        ]
    )
    config = _base_config()

    _apply_runtime_overrides(config=config, args=args, parser=parser)

    assert len(config.clusters) == 1
    assert config.clusters[0].name == "example-test-apps-gke"
    assert config.clusters[0].project == "example-test-project"
    assert config.clusters[0].region == "us-central1"


def test_runtime_overrides_cluster_name_requires_project_and_region() -> None:
    parser = _build_parser()
    args = parser.parse_args(
        [
            "preflight",
            "--config",
            "config/runtime.yaml",
            "--cluster",
            "apps",
        ]
    )
    config = _base_config()

    with pytest.raises(SystemExit):
        _apply_runtime_overrides(config=config, args=args, parser=parser)


def test_runtime_overrides_trend_days() -> None:
    parser = _build_parser()
    args = parser.parse_args(
        [
            "preflight",
            "--config",
            "config/runtime.yaml",
            "--trend-days",
            "14",
        ]
    )
    config = _base_config()

    _apply_runtime_overrides(config=config, args=args, parser=parser)

    assert config.time_windows.trend_days == 14


def test_effective_timeout_seconds_uses_config_when_cli_omitted() -> None:
    parser = _build_parser()
    args = parser.parse_args(["preflight", "--config", "config/runtime.yaml"])
    config = _base_config()
    config.timeout_seconds = 180

    assert _effective_timeout_seconds(config=config, args=args, parser=parser) == 180


def test_effective_timeout_seconds_prefers_cli_override() -> None:
    parser = _build_parser()
    args = parser.parse_args(
        [
            "preflight",
            "--config",
            "config/runtime.yaml",
            "--timeout-seconds",
            "90",
        ]
    )
    config = _base_config()
    config.timeout_seconds = 180

    assert _effective_timeout_seconds(config=config, args=args, parser=parser) == 90


def test_effective_timeout_seconds_rejects_negative_cli_override() -> None:
    parser = _build_parser()
    args = parser.parse_args(
        [
            "preflight",
            "--config",
            "config/runtime.yaml",
            "--timeout-seconds",
            "-1",
        ]
    )
    config = _base_config()

    with pytest.raises(SystemExit):
        _effective_timeout_seconds(config=config, args=args, parser=parser)


def test_effective_timeout_seconds_rejects_zero_cli_override() -> None:
    parser = _build_parser()
    args = parser.parse_args(
        [
            "preflight",
            "--config",
            "config/runtime.yaml",
            "--timeout-seconds",
            "0",
        ]
    )
    config = _base_config()

    with pytest.raises(SystemExit):
        _effective_timeout_seconds(config=config, args=args, parser=parser)


def test_run_collector_with_timeout_returns_successful_result() -> None:
    config = _base_config()

    result = _run_collector_with_timeout(
        collector_name="example",
        collector_fn=_successful_collector,
        config=config,
        collector_kwargs={"timeout_seconds": 7},
        timeout_seconds=7,
    )

    assert result.status == Status.OK
    assert result.summary == "ok"
    assert result.details == {"timeout_seconds": 7}


def test_run_collector_with_timeout_terminates_slow_subprocess() -> None:
    config = _base_config()

    result = _run_collector_with_timeout(
        collector_name="slow",
        collector_fn=_slow_collector,
        config=config,
        collector_kwargs={},
        timeout_seconds=1,
    )

    assert result.collector == "slow"
    assert result.status == Status.FAILED
    assert result.details == {"timeout_seconds": 1}
    assert result.errors == ["slow did not finish within 1 second(s)"]
