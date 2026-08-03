from __future__ import annotations

import math
import os
import re
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from opsbrief import __version__
from opsbrief.config import EnvConfig
from opsbrief.gcp_auth import AuthMode
from opsbrief.models import CheckResult, Status, now_utc_iso
from opsbrief.status import max_status

_HPA_FAILURE_CONDITIONS = ("AbleToScale", "ScalingActive")
_HPA_CONDITIONS = ("AbleToScale", "ScalingActive", "ScalingLimited")
_RANGE_RATE_WINDOW = "5m"
_RANGE_STEP_SECONDS = 3600


class PrometheusConfigError(ValueError):
    pass


class PrometheusApiError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PrometheusClient:
    base_url: str
    timeout_seconds: int
    token: str = ""

    def status_buildinfo(self) -> dict[str, Any]:
        payload = self._get_json("/api/v1/status/buildinfo", {})
        return _dict_value(payload, "data")

    def query(self, query: str) -> list[dict[str, Any]]:
        payload = self._get_json("/api/v1/query", {"query": query})
        return _prometheus_result(payload)

    def query_range(
        self,
        query: str,
        *,
        start: float,
        end: float,
        step_seconds: int,
    ) -> list[dict[str, Any]]:
        payload = self._get_json(
            "/api/v1/query_range",
            {
                "query": query,
                "start": f"{start:.3f}",
                "end": f"{end:.3f}",
                "step": str(max(1, step_seconds)),
            },
        )
        return _prometheus_result(payload)

    def _get_json(self, api_path: str, params: dict[str, str]) -> dict[str, Any]:
        url = _build_url(self.base_url, api_path, params)
        headers = {
            "Accept": "application/json",
            "User-Agent": f"opsbrief/{__version__}",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        response = httpx.get(
            url,
            headers=headers,
            timeout=max(1, self.timeout_seconds),
            follow_redirects=True,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise PrometheusApiError("Prometheus response is not a JSON object")
        return payload


def _prometheus_result(payload: dict[str, Any]) -> list[dict[str, Any]]:
    status = str(payload.get("status", ""))
    if status != "success":
        error_type = str(payload.get("errorType", "unknown"))
        error = str(payload.get("error", "Prometheus query failed"))
        raise PrometheusApiError(f"{error_type}: {error}")
    data = _dict_value(payload, "data")
    result = data.get("result", [])
    if not isinstance(result, list):
        raise PrometheusApiError("Prometheus response field data.result is not a list")
    return [item for item in result if isinstance(item, dict)]


def collect(
    config: EnvConfig,
    timeout_seconds: int = 45,
    output_dir: str | None = None,
    auth_mode: AuthMode = "auto",
    impersonate_service_account: str = "",
) -> CheckResult:
    started_at = now_utc_iso()
    _ = output_dir, auth_mode, impersonate_service_account

    try:
        client = _client_from_config(config, timeout_seconds)
        scope_labels = _scope_labels(config)
    except PrometheusConfigError as exc:
        return CheckResult(
            collector="prometheus_monitoring",
            status=Status.SKIPPED_CONFIG,
            summary="Prometheus monitoring collector is not configured",
            details={},
            errors=[str(exc)],
            started_at=started_at,
            finished_at=now_utc_iso(),
        )

    status = Status.OK
    errors: list[str] = []
    query_results: list[dict[str, Any]] = []
    build_info: dict[str, Any] = {}

    try:
        build_info = client.status_buildinfo()
    except Exception as exc:  # noqa: BLE001
        failure_status = _classify_exception(exc)
        return CheckResult(
            collector="prometheus_monitoring",
            status=failure_status,
            summary="Unable to reach Prometheus API",
            details={
                "url": config.prometheus.url,
                "scope": {"labels": scope_labels, "window_days": _window_days(config)},
            },
            errors=[_exception_message(exc)],
            started_at=started_at,
            finished_at=now_utc_iso(),
        )

    selector = _label_selector(scope_labels)
    window = f"{_window_days(config)}d"

    hpa_total = _safe_query(
        client=client,
        query_results=query_results,
        errors=errors,
        name="hpa_total",
        query=f"count by (cluster, environment) (kube_horizontalpodautoscaler_info{selector})",
    )
    hpa_current_failures = _safe_query(
        client=client,
        query_results=query_results,
        errors=errors,
        name="hpa_current_failure_conditions",
        query=(
            "sum by (cluster, environment, condition, status) "
            "(kube_horizontalpodautoscaler_status_condition"
            f"{_hpa_failure_label_selector(scope_labels)})"
        ),
    )
    hpa_failure_history = _safe_query(
        client=client,
        query_results=query_results,
        errors=errors,
        name="hpa_failure_conditions_7d",
        query=(
            "sum by (cluster, environment, condition, status) "
            "(max_over_time(kube_horizontalpodautoscaler_status_condition"
            f"{_hpa_failure_label_selector(scope_labels)}"
            f"[{window}]))"
        ),
    )
    hpa_current_conditions = _safe_query(
        client=client,
        query_results=query_results,
        errors=errors,
        name="hpa_current_conditions",
        query=(
            "sum by (cluster, environment, condition, status) "
            "(kube_horizontalpodautoscaler_status_condition"
            f"{_label_selector(scope_labels, condition=_HPA_CONDITIONS)})"
        ),
    )
    hpa_scaling_limited_history = _safe_query(
        client=client,
        query_results=query_results,
        errors=errors,
        name="hpa_scaling_limited_7d",
        query=(
            "sum by (cluster, environment) "
            "(max_over_time(kube_horizontalpodautoscaler_status_condition"
            f"{_label_selector(scope_labels, condition=('ScalingLimited',), status=('true',))}"
            f"[{window}]))"
        ),
    )
    target_total = _safe_query(
        client=client,
        query_results=query_results,
        errors=errors,
        name="target_total",
        query=f"count by (cluster, environment) (up{selector})",
    )
    target_down = _safe_query(
        client=client,
        query_results=query_results,
        errors=errors,
        name="target_down",
        query=f"count by (cluster, environment, job) (up{selector} == 0)",
    )
    time_series = _collect_time_series(
        client=client,
        query_results=query_results,
        errors=errors,
        scope_labels=scope_labels,
        window_days=_window_days(config),
    )

    for query_result in query_results:
        status = max_status(status, _status_from_value(query_result.get("status", "ok")))

    clusters = _cluster_summaries(
        hpa_total=hpa_total,
        hpa_current_failures=hpa_current_failures,
        hpa_failure_history=hpa_failure_history,
        hpa_current_conditions=hpa_current_conditions,
        hpa_scaling_limited_history=hpa_scaling_limited_history,
        target_total=target_total,
        target_down=target_down,
    )

    hpa_failure_series = sum(
        _int_value(cluster.get("hpa_failure_condition_series_7d", 0)) for cluster in clusters
    )
    down_targets = sum(_int_value(cluster.get("target_down", 0)) for cluster in clusters)
    autoscaling_policy = _autoscaling_policy_details(config)
    hpa_signals_scored = bool(autoscaling_policy["hpa_signals_scored"])
    if (hpa_signals_scored and hpa_failure_series > 0) or down_targets > 0:
        status = max_status(status, Status.WARNING)

    if not clusters:
        summary = "Prometheus API reachable; no scoped monitoring series returned"
    else:
        hpa_scope_text = "scored" if hpa_signals_scored else "informational"
        summary = (
            f"Collected Prometheus monitoring for {len(clusters)} cluster/environment scope(s); "
            f"hpa_failure_condition_series_7d={hpa_failure_series} ({hpa_scope_text}); "
            f"down_targets={down_targets}"
        )

    return CheckResult(
        collector="prometheus_monitoring",
        status=status,
        summary=summary,
        details={
            "url": config.prometheus.url,
            "build": build_info,
            "scope": {"labels": scope_labels, "window_days": _window_days(config)},
            "autoscaling_policy": autoscaling_policy,
            "queries": query_results,
            "clusters": clusters,
            "hpa_failure_conditions_7d": _condition_rows(
                hpa_failure_history, "condition_series_7d"
            ),
            "hpa_current_failure_conditions": _condition_rows(
                hpa_current_failures, "condition_series"
            ),
            "time_series": time_series,
        },
        errors=errors,
        started_at=started_at,
        finished_at=now_utc_iso(),
    )


def _autoscaling_policy_details(config: EnvConfig) -> dict[str, Any]:
    autoscaling = config.report_expectations.autoscaling
    workload = autoscaling.workload_hpa
    platform = autoscaling.platform_hpa
    return {
        "workload_hpa": {
            "status": workload.status,
            "reason": workload.reason,
        },
        "platform_hpa": {
            "status": platform.status,
            "reason": platform.reason,
        },
        "hpa_signals_scored": (workload.status == "assessed" or platform.status == "assessed"),
    }


def probe(config: EnvConfig, timeout_seconds: int = 30) -> tuple[Status, str]:
    try:
        client = _client_from_config(config, timeout_seconds)
        _scope_labels(config)
        build_info = client.status_buildinfo()
    except PrometheusConfigError as exc:
        return Status.SKIPPED_CONFIG, str(exc)
    except Exception as exc:  # noqa: BLE001
        return _classify_exception(exc), _exception_message(exc)
    version = str(build_info.get("version", "unknown"))
    return Status.OK, f"reachable version={version}"


def _client_from_config(config: EnvConfig, timeout_seconds: int) -> PrometheusClient:
    url = _normalize_base_url(config.prometheus.url)
    token = ""
    token_env = config.prometheus.token_env.strip()
    if token_env:
        token = os.environ.get(token_env, "").strip()
        if not token:
            raise PrometheusConfigError(f"Prometheus token env var is not set: {token_env}")
    return PrometheusClient(base_url=url, timeout_seconds=timeout_seconds, token=token)


def _normalize_base_url(url: str) -> str:
    value = url.strip().rstrip("/")
    if not value:
        raise PrometheusConfigError("prometheus.url is required")
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise PrometheusConfigError("prometheus.url must be an http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise PrometheusConfigError(
            "prometheus.url must not include credentials; use prometheus.token_env"
        )
    if parsed.query or parsed.fragment:
        raise PrometheusConfigError("prometheus.url must not include query strings or fragments")
    return value


def _scope_labels(config: EnvConfig) -> dict[str, list[str]]:
    if config.prometheus.labels:
        return {label: list(values) for label, values in config.prometheus.labels.items()}
    clusters = _dedupe([cluster.name for cluster in config.clusters if cluster.name])
    if clusters:
        return {"cluster": clusters}
    raise PrometheusConfigError(
        "prometheus.labels or configured clusters are required to scope Prometheus queries"
    )


def _safe_query(
    *,
    client: PrometheusClient,
    query_results: list[dict[str, Any]],
    errors: list[str],
    name: str,
    query: str,
) -> list[dict[str, Any]]:
    try:
        series = client.query(query)
    except Exception as exc:  # noqa: BLE001
        message = _exception_message(exc)
        query_results.append(
            {"name": name, "status": _classify_exception(exc).value, "error": message}
        )
        errors.append(f"{name}: {message}")
        return []
    query_results.append({"name": name, "status": Status.OK.value, "series_count": len(series)})
    return series


def _collect_time_series(
    *,
    client: PrometheusClient,
    query_results: list[dict[str, Any]],
    errors: list[str],
    scope_labels: dict[str, list[str]],
    window_days: int,
) -> list[dict[str, Any]]:
    end = datetime.now(UTC)
    start = end - timedelta(days=window_days)
    start_ts = start.timestamp()
    end_ts = end.timestamp()
    step_seconds = _range_step_seconds(window_days)
    scoped_selector = _label_selector(scope_labels)
    cluster_selector = _label_selector(_cluster_scope_labels(scope_labels))
    cpu_selector = _selector_with_matchers(cluster_selector, 'mode!~"idle|iowait|steal"')

    specs = [
        {
            "name": "cluster_cpu_utilization_percent",
            "title": "Cluster CPU Utilization (%)",
            "unit": "percent",
            "query": (
                "100 * avg by (cluster) "
                "(sum by (cluster, instance, cpu) "
                f"(rate(node_cpu_seconds_total{cpu_selector}[{_RANGE_RATE_WINDOW}])))"
            ),
        },
        {
            "name": "cluster_memory_utilization_percent",
            "title": "Cluster Memory Utilization (%)",
            "unit": "percent",
            "query": (
                "100 * sum by (cluster) "
                f"(node_memory_MemTotal_bytes{cluster_selector} "
                f"- node_memory_MemAvailable_bytes{cluster_selector}) "
                f"/ sum by (cluster) (node_memory_MemTotal_bytes{cluster_selector})"
            ),
        },
        {
            "name": "hpa_total",
            "title": "HPA Count",
            "unit": "count",
            "query": (
                "count by (cluster, environment) "
                f"(kube_horizontalpodautoscaler_info{scoped_selector})"
            ),
        },
        {
            "name": "hpa_failure_conditions",
            "title": "HPA Failure Condition Series",
            "unit": "count",
            "query": (
                "sum by (cluster, environment) "
                "(kube_horizontalpodautoscaler_status_condition"
                f"{_hpa_failure_label_selector(scope_labels)})"
            ),
        },
        {
            "name": "hpa_scaling_limited",
            "title": "HPA Scaling-Limited Series",
            "unit": "count",
            "query": (
                "sum by (cluster, environment) "
                "(kube_horizontalpodautoscaler_status_condition"
                f"{_label_selector(scope_labels, condition=('ScalingLimited',), status=('true',))})"
            ),
        },
        {
            "name": "scrape_targets_total",
            "title": "Prometheus Scrape Targets",
            "unit": "count",
            "query": f"count by (cluster, environment) (up{scoped_selector})",
        },
        {
            "name": "scrape_targets_down",
            "title": "Down Prometheus Scrape Targets",
            "unit": "count",
            "query": f"count by (cluster, environment) (up{scoped_selector} == 0)",
        },
    ]

    blocks: list[dict[str, Any]] = []
    for spec in specs:
        name = str(spec["name"])
        query = str(spec["query"])
        series = _safe_query_range(
            client=client,
            query_results=query_results,
            errors=errors,
            name=f"{name}_range",
            query=query,
            start=start_ts,
            end=end_ts,
            step_seconds=step_seconds,
        )
        blocks.append(
            _time_series_block(
                name=name,
                title=str(spec["title"]),
                unit=str(spec["unit"]),
                query=query,
                start=start_ts,
                end=end_ts,
                step_seconds=step_seconds,
                raw_series=series,
                scope_labels=scope_labels,
            )
        )
    return blocks


def _safe_query_range(
    *,
    client: PrometheusClient,
    query_results: list[dict[str, Any]],
    errors: list[str],
    name: str,
    query: str,
    start: float,
    end: float,
    step_seconds: int,
) -> list[dict[str, Any]]:
    try:
        series = client.query_range(
            query,
            start=start,
            end=end,
            step_seconds=step_seconds,
        )
    except Exception as exc:  # noqa: BLE001
        message = _exception_message(exc)
        query_results.append(
            {
                "name": name,
                "type": "range",
                "status": _classify_exception(exc).value,
                "error": message,
            }
        )
        errors.append(f"{name}: {message}")
        return []
    query_results.append(
        {
            "name": name,
            "type": "range",
            "status": Status.OK.value,
            "series_count": len(series),
            "step_seconds": step_seconds,
        }
    )
    return series


def _time_series_block(
    *,
    name: str,
    title: str,
    unit: str,
    query: str,
    start: float,
    end: float,
    step_seconds: int,
    raw_series: list[dict[str, Any]],
    scope_labels: dict[str, list[str]],
) -> dict[str, Any]:
    return {
        "name": name,
        "title": title,
        "unit": unit,
        "query": query,
        "start": round(start, 3),
        "end": round(end, 3),
        "step_seconds": step_seconds,
        "series": _range_series_rows(raw_series, scope_labels),
    }


def _range_series_rows(
    raw_series: list[dict[str, Any]],
    scope_labels: dict[str, list[str]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for series in raw_series:
        metric = _metric(series)
        values = series.get("values", [])
        if not isinstance(values, list):
            continue
        samples: list[list[float]] = []
        for sample in values:
            if not isinstance(sample, list) or len(sample) < 2:
                continue
            timestamp = _float_value(sample[0])
            value = _float_value(sample[1])
            if timestamp is None or value is None:
                continue
            samples.append([round(timestamp, 3), value])
        if not samples:
            continue
        rows.append(
            {
                "label": _range_series_label(metric, scope_labels),
                "metric": metric,
                "values": samples,
            }
        )
    return sorted(rows, key=lambda item: str(item.get("label", "")))


def _range_series_label(metric: dict[str, Any], scope_labels: dict[str, list[str]]) -> str:
    environment = str(metric.get("environment") or _single_scope_value(scope_labels, "environment"))
    cluster = str(metric.get("cluster") or _single_scope_value(scope_labels, "cluster"))
    if environment and cluster:
        return f"{environment}/{cluster}"
    if cluster:
        return cluster
    if environment:
        return environment
    return "unlabelled"


def _cluster_summaries(
    *,
    hpa_total: list[dict[str, Any]],
    hpa_current_failures: list[dict[str, Any]],
    hpa_failure_history: list[dict[str, Any]],
    hpa_current_conditions: list[dict[str, Any]],
    hpa_scaling_limited_history: list[dict[str, Any]],
    target_total: list[dict[str, Any]],
    target_down: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    summaries: dict[tuple[str, str], dict[str, Any]] = {}

    for series in hpa_total:
        summary = _summary_for_series(summaries, series)
        summary["hpa_total"] = _int_sample_value(series)
    for series in hpa_current_failures:
        value = _int_sample_value(series)
        if value <= 0:
            continue
        summary = _summary_for_series(summaries, series)
        summary["hpa_current_failure_condition_series"] += value
    for series in hpa_failure_history:
        value = _int_sample_value(series)
        if value <= 0:
            continue
        summary = _summary_for_series(summaries, series)
        summary["hpa_failure_condition_series_7d"] += value
    for series in hpa_current_conditions:
        metric = _metric(series)
        if metric.get("condition") != "ScalingLimited" or metric.get("status") != "true":
            continue
        value = _int_sample_value(series)
        if value <= 0:
            continue
        summary = _summary_for_series(summaries, series)
        summary["hpa_current_scaling_limited"] += value
    for series in hpa_scaling_limited_history:
        value = _int_sample_value(series)
        if value <= 0:
            continue
        summary = _summary_for_series(summaries, series)
        summary["hpa_scaling_limited_7d"] += value
    for series in target_total:
        summary = _summary_for_series(summaries, series)
        summary["target_total"] = _int_sample_value(series)
    for series in target_down:
        value = _int_sample_value(series)
        if value <= 0:
            continue
        summary = _summary_for_series(summaries, series)
        summary["target_down"] += value
        metric = _metric(series)
        summary["down_jobs"].append(
            {
                "job": str(metric.get("job", "unknown")),
                "down_targets": value,
            }
        )

    rows = list(summaries.values())
    for row in rows:
        row["down_jobs"] = sorted(
            row["down_jobs"],
            key=lambda item: (-_int_value(item.get("down_targets", 0)), str(item.get("job", ""))),
        )
        if (
            _int_value(row.get("target_down", 0)) > 0
            or _int_value(row.get("hpa_current_failure_condition_series", 0)) > 0
            or _int_value(row.get("hpa_failure_condition_series_7d", 0)) > 0
        ):
            row["status"] = Status.WARNING.value
        else:
            row["status"] = Status.OK.value
    return sorted(
        rows, key=lambda item: (str(item.get("environment", "")), str(item.get("cluster", "")))
    )


def _summary_for_series(
    summaries: dict[tuple[str, str], dict[str, Any]],
    series: dict[str, Any],
) -> dict[str, Any]:
    metric = _metric(series)
    cluster = str(metric.get("cluster", "unlabelled"))
    environment = str(metric.get("environment", "unlabelled"))
    key = (cluster, environment)
    if key not in summaries:
        summaries[key] = {
            "cluster": cluster,
            "environment": environment,
            "status": Status.OK.value,
            "hpa_total": 0,
            "hpa_current_failure_condition_series": 0,
            "hpa_failure_condition_series_7d": 0,
            "hpa_current_scaling_limited": 0,
            "hpa_scaling_limited_7d": 0,
            "target_total": 0,
            "target_down": 0,
            "down_jobs": [],
        }
    return summaries[key]


def _condition_rows(series_list: list[dict[str, Any]], count_key: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for series in series_list:
        count = _int_sample_value(series)
        if count <= 0:
            continue
        metric = _metric(series)
        rows.append(
            {
                "cluster": str(metric.get("cluster", "unlabelled")),
                "environment": str(metric.get("environment", "unlabelled")),
                "condition": str(metric.get("condition", "")),
                "status": str(metric.get("status", "")),
                count_key: count,
            }
        )
    return sorted(
        rows,
        key=lambda item: (
            str(item.get("environment", "")),
            str(item.get("cluster", "")),
            str(item.get("condition", "")),
            str(item.get("status", "")),
        ),
    )


def _label_selector(
    scope_labels: dict[str, list[str]],
    **extra_labels: tuple[str, ...],
) -> str:
    matchers: list[str] = []
    for label, values in sorted(scope_labels.items()):
        matchers.append(_label_matcher(label, values))
    for label, extra_values in sorted(extra_labels.items()):
        matchers.append(_label_matcher(label, list(extra_values)))
    return "{" + ",".join(matchers) + "}" if matchers else ""


def _selector_with_matchers(selector: str, *raw_matchers: str) -> str:
    matchers = [matcher for matcher in raw_matchers if matcher]
    if not matchers:
        return selector
    if not selector:
        return "{" + ",".join(matchers) + "}"
    return selector[:-1] + "," + ",".join(matchers) + "}"


def _hpa_failure_label_selector(scope_labels: dict[str, list[str]]) -> str:
    return _label_selector(
        scope_labels,
        condition=_HPA_FAILURE_CONDITIONS,
        status=("false", "unknown"),
    )


def _label_matcher(label: str, values: list[str]) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", label):
        raise PrometheusConfigError(f"Invalid Prometheus label name: {label}")
    if not values:
        raise PrometheusConfigError(f"Prometheus label has no values: {label}")
    if any(value == "" for value in values):
        raise PrometheusConfigError(f"Prometheus label has empty value: {label}")
    if len(values) == 1:
        return f'{label}="{_escape_promql_string(values[0])}"'
    regex = "^(?:" + "|".join(re.escape(value) for value in values) + ")$"
    return f'{label}=~"{_escape_promql_string(regex)}"'


def _escape_promql_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _build_url(base_url: str, api_path: str, params: dict[str, str]) -> str:
    query = urllib.parse.urlencode(params)
    return (
        f"{base_url}/{api_path.lstrip('/')}?{query}"
        if query
        else f"{base_url}/{api_path.lstrip('/')}"
    )


def _classify_exception(exc: Exception) -> Status:
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        if status_code in (401, 403):
            return Status.SKIPPED_PERMISSION
        if status_code in (429, 500, 502, 503, 504):
            return Status.SKIPPED_NETWORK
        return Status.FAILED
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return Status.SKIPPED_NETWORK
    if isinstance(exc, PrometheusConfigError):
        return Status.SKIPPED_CONFIG
    return Status.FAILED


def _status_from_value(value: Any) -> Status:
    try:
        return Status(str(value))
    except ValueError:
        return Status.FAILED


def _exception_message(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        reason = exc.response.reason_phrase
        return f"HTTP {status_code}: {reason}"
    if isinstance(exc, httpx.RequestError):
        return f"URL error: {exc}"
    return str(exc)


def _window_days(config: EnvConfig) -> int:
    return max(1, min(365, config.time_windows.trend_days))


def _range_step_seconds(window_days: int) -> int:
    # Weekly reports need trend shape, not dashboard-resolution sampling.
    return max(_RANGE_STEP_SECONDS, int((window_days * 24 * 60 * 60) / 240))


def _cluster_scope_labels(scope_labels: dict[str, list[str]]) -> dict[str, list[str]]:
    clusters = scope_labels.get("cluster", [])
    if clusters:
        return {"cluster": clusters}
    return scope_labels


def _single_scope_value(scope_labels: dict[str, list[str]], label: str) -> str:
    values = scope_labels.get(label, [])
    return values[0] if len(values) == 1 else ""


def _dict_value(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key, {})
    return value if isinstance(value, dict) else {}


def _metric(series: dict[str, Any]) -> dict[str, Any]:
    value = series.get("metric", {})
    return value if isinstance(value, dict) else {}


def _int_sample_value(series: dict[str, Any]) -> int:
    value = series.get("value", [])
    if not isinstance(value, list) or len(value) < 2:
        return 0
    return _int_value(value[1])


def _int_value(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def _float_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        numeric = float(value)
    elif isinstance(value, str):
        try:
            numeric = float(value)
        except ValueError:
            return None
    else:
        return None
    return numeric if math.isfinite(numeric) else None


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        if value not in out:
            out.append(value)
    return out
