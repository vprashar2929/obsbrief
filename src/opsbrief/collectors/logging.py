from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, cast

from googleapiclient.errors import HttpError

from opsbrief.config import EnvConfig
from opsbrief.gcp_api import (
    build_logging_config_service_client,
    build_metric_service_client,
    build_pubsub_publisher_client,
    build_service,
    collect_paged,
    list_monitoring_time_series,
    protobuf_to_dict,
    resolve_fallback_service,
)
from opsbrief.gcp_auth import AuthMode, get_auth_bundle
from opsbrief.models import CheckResult, Status, now_utc_iso
from opsbrief.status import classify_http_error, max_status

PUBSUB_DEST_PATTERN = re.compile(
    r"^pubsub\.googleapis\.com/(?P<topic>projects/[^/]+/topics/[^/]+)$"
)
_TIME_SERIES_PAGE_SIZE = 200
_TIME_SERIES_MAX_PAGES = 3
_TIME_SERIES_MAX_SERIES = 500
_PUBSUB_METRIC_SUBSCRIPTION_SAMPLE_LIMIT = 10


@dataclass(frozen=True)
class _MonitoringClients:
    service: Any | None
    metric_client: Any | None
    timeout_seconds: int


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
    project_summaries: list[dict[str, Any]] = []

    projects = _candidate_projects(config)
    if not projects:
        return CheckResult(
            collector="logging",
            status=Status.SKIPPED_CONFIG,
            summary="No projects configured for logging collector",
            details={"projects": []},
            errors=[],
            started_at=started_at,
            finished_at=now_utc_iso(),
        )

    try:
        auth = get_auth_bundle(
            auth_mode=auth_mode, impersonate_service_account=impersonate_service_account
        )
        logging_config_client = None
        try:
            logging_config_client = build_logging_config_service_client(auth)
        except Exception:  # noqa: BLE001
            logging_config_client = None
        logging_service = None

        def logging_service_fallback() -> Any:
            nonlocal logging_service
            if logging_service is None:
                logging_service = build_service(auth, "logging", "v2", timeout_seconds)
            return logging_service

        pubsub_client = None
        try:
            pubsub_client = build_pubsub_publisher_client(auth)
        except Exception:  # noqa: BLE001
            pubsub_client = None
        pubsub_service = None

        def pubsub_service_fallback() -> Any:
            nonlocal pubsub_service
            if pubsub_service is None:
                pubsub_service = build_service(auth, "pubsub", "v1", timeout_seconds)
            return pubsub_service

        monitoring_service = None

        def monitoring_service_fallback() -> Any:
            nonlocal monitoring_service
            if monitoring_service is None:
                monitoring_service = build_service(auth, "monitoring", "v3", timeout_seconds)
            return monitoring_service

        metric_service_client = None
        try:
            metric_service_client = build_metric_service_client(auth)
        except Exception:  # noqa: BLE001
            metric_service_client = None
        monitoring = _MonitoringClients(
            service=monitoring_service_fallback,
            metric_client=metric_service_client,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            collector="logging",
            status=Status.FAILED,
            summary="Unable to initialize Logging API client",
            details={},
            errors=[str(exc)],
            started_at=started_at,
            finished_at=now_utc_iso(),
        )

    for project in projects:
        try:
            sinks = _list_sinks(
                logging_service_fallback,
                project,
                config_client=logging_config_client,
                timeout_seconds=timeout_seconds,
            )
            buckets = _list_buckets(
                logging_service_fallback,
                project,
                config_client=logging_config_client,
                timeout_seconds=timeout_seconds,
            )
            topic_refs = _extract_pubsub_topics(sinks)
            logging_metrics, logging_metrics_error = _collect_logging_metrics(
                monitoring=monitoring,
                project=project,
                buckets=buckets,
            )
        except HttpError as exc:
            failure_status = classify_http_error(exc)
            project_summaries.append(
                {"project": project, "status": failure_status.value, "error": str(exc)}
            )
            errors.append(f"{project}: {exc}")
            status = max_status(status, failure_status)
            continue
        except Exception as exc:  # noqa: BLE001
            project_summaries.append(
                {"project": project, "status": Status.FAILED.value, "error": str(exc)}
            )
            errors.append(f"{project}: {exc}")
            status = max_status(status, Status.FAILED)
            continue

        pubsub_status = Status.OK
        topic_checks: list[dict[str, Any]] = []
        topic_subscriptions_total = 0
        pubsub_metric_subscriptions: list[str] = []
        pubsub_metric_subscriptions_sampled = False
        for topic_ref in topic_refs:
            try:
                topic_payload = _get_topic(
                    pubsub_service_fallback,
                    topic_ref,
                    pubsub_client=pubsub_client,
                    timeout_seconds=timeout_seconds,
                )
                subscriptions = _list_topic_subscriptions(
                    pubsub_service_fallback,
                    topic_ref,
                    pubsub_client=pubsub_client,
                    timeout_seconds=timeout_seconds,
                )
            except HttpError as exc:
                topic_status = classify_http_error(exc)
                pubsub_status = max_status(pubsub_status, topic_status)
                topic_checks.append(
                    {
                        "topic": topic_ref,
                        "status": topic_status.value,
                        "error": str(exc),
                    }
                )
                errors.append(f"{project}/{topic_ref}: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001
                pubsub_status = max_status(pubsub_status, Status.FAILED)
                topic_checks.append(
                    {
                        "topic": topic_ref,
                        "status": Status.FAILED.value,
                        "error": str(exc),
                    }
                )
                errors.append(f"{project}/{topic_ref}: {exc}")
                continue

            topic_subscriptions_total += len(subscriptions)
            if len(subscriptions) > _PUBSUB_METRIC_SUBSCRIPTION_SAMPLE_LIMIT:
                pubsub_metric_subscriptions_sampled = True
            for subscription in subscriptions[:_PUBSUB_METRIC_SUBSCRIPTION_SAMPLE_LIMIT]:
                if subscription not in pubsub_metric_subscriptions:
                    pubsub_metric_subscriptions.append(subscription)
            topic_checks.append(
                {
                    "topic": topic_ref,
                    "status": Status.OK.value,
                    "labels": topic_payload.get("labels", {}),
                    "kms_key_name": topic_payload.get("kmsKeyName", ""),
                    "subscription_count": len(subscriptions),
                    "sample_subscriptions": subscriptions[
                        :_PUBSUB_METRIC_SUBSCRIPTION_SAMPLE_LIMIT
                    ],
                }
            )

        pubsub_metrics, pubsub_metrics_error = _collect_pubsub_metrics(
            monitoring=monitoring,
            project=project,
            subscriptions=pubsub_metric_subscriptions,
        )
        metrics_errors = [error for error in (logging_metrics_error, pubsub_metrics_error) if error]

        if not buckets:
            status = max_status(status, Status.WARNING)
        status = max_status(status, pubsub_status)
        if topic_refs and topic_subscriptions_total == 0:
            status = max_status(status, Status.WARNING)

        project_status = max_status(Status.OK, pubsub_status)
        if not buckets or (topic_refs and topic_subscriptions_total == 0):
            project_status = max_status(project_status, Status.WARNING)

        project_summaries.append(
            {
                "project": project,
                "status": project_status.value,
                "sink_count": len(sinks),
                "bucket_count": len(buckets),
                "pubsub_topic_count": len(topic_refs),
                "pubsub_subscription_total": topic_subscriptions_total,
                "pubsub_metric_subscriptions_checked": len(pubsub_metric_subscriptions),
                "pubsub_metric_subscription_sample_limit": (
                    _PUBSUB_METRIC_SUBSCRIPTION_SAMPLE_LIMIT
                ),
                "pubsub_metric_subscriptions_sampled": pubsub_metric_subscriptions_sampled,
                "splunk_hints": _splunk_hints(sinks=sinks, topic_checks=topic_checks),
                "sinks": [
                    {
                        "name": sink.get("name", ""),
                        "destination": sink.get("destination", ""),
                        "filter": sink.get("filter", ""),
                        "disabled": sink.get("disabled", False),
                    }
                    for sink in sinks[:15]
                ],
                "buckets": [
                    {
                        "name": bucket.get("name", ""),
                        "retention_days": bucket.get("retentionDays", None),
                        "locked": bucket.get("locked", None),
                        "location": _bucket_location(bucket),
                    }
                    for bucket in buckets[:20]
                ],
                "pubsub_topics": topic_checks,
                "logging_metrics": logging_metrics,
                "pubsub_metrics": pubsub_metrics,
                "metrics_error": "; ".join(metrics_errors),
            }
        )

    summary = f"Collected logging summary for {len(project_summaries)} project(s)"
    return CheckResult(
        collector="logging",
        status=status,
        summary=summary,
        details={"projects": project_summaries},
        errors=errors,
        started_at=started_at,
        finished_at=now_utc_iso(),
    )


def _candidate_projects(config: EnvConfig) -> list[str]:
    unique: list[str] = []
    for project in config.projects.values():
        if project and project not in unique:
            unique.append(project)
    for cluster in config.clusters:
        if cluster.project and cluster.project not in unique:
            unique.append(cluster.project)
    return unique


def _list_sinks(
    service: object,
    project: str,
    *,
    config_client: Any | None = None,
    timeout_seconds: int = 60,
) -> list[dict[str, Any]]:
    client_error: Exception | None = None
    if config_client is not None:
        try:
            return _list_sinks_with_client(config_client, project, timeout_seconds)
        except Exception as exc:  # noqa: BLE001
            client_error = exc
    service = resolve_fallback_service(
        service,
        client_error,
        "Logging discovery service unavailable",
    )
    return _list_sinks_with_service(service, project)


def _list_sinks_with_client(
    client: Any,
    project: str,
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    response = client.list_sinks(
        parent=f"projects/{project}",
        timeout=max(1, timeout_seconds),
    )
    return [protobuf_to_dict(item) for item in response]


def _list_sinks_with_service(service: Any, project: str) -> list[dict[str, Any]]:
    request = service.projects().sinks().list(parent=f"projects/{project}", pageSize=100)
    return collect_paged(
        request,
        item_key="sinks",
        next_request=lambda previous_request, previous_response: (
            service.projects().sinks().list_next(previous_request, previous_response)
        ),
    )


def _list_buckets(
    service: object,
    project: str,
    *,
    config_client: Any | None = None,
    timeout_seconds: int = 60,
) -> list[dict[str, Any]]:
    client_error: Exception | None = None
    if config_client is not None:
        try:
            return _list_buckets_with_client(config_client, project, timeout_seconds)
        except Exception as exc:  # noqa: BLE001
            client_error = exc
    service = resolve_fallback_service(
        service,
        client_error,
        "Logging discovery service unavailable",
    )
    return _list_buckets_with_service(service, project)


def _list_buckets_with_client(
    client: Any,
    project: str,
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    parents = [f"projects/{project}/locations/-", f"projects/{project}/locations/global"]
    collected: list[dict[str, Any]] = []
    last_error: Exception | None = None

    for parent in parents:
        try:
            response = client.list_buckets(
                parent=parent,
                timeout=max(1, timeout_seconds),
            )
            collected.extend(protobuf_to_dict(item) for item in response)
            if collected:
                return collected
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue

    if last_error is not None:
        raise last_error
    return collected


def _list_buckets_with_service(service: Any, project: str) -> list[dict[str, Any]]:
    parents = [f"projects/{project}/locations/-", f"projects/{project}/locations/global"]
    collected: list[dict[str, Any]] = []
    last_error: HttpError | None = None

    for parent in parents:
        try:
            request = service.projects().locations().buckets().list(parent=parent, pageSize=100)
            while request is not None:
                response = request.execute()
                collected.extend(response.get("buckets", []))
                request = service.projects().locations().buckets().list_next(request, response)
            if collected:
                return collected
        except HttpError as exc:
            last_error = exc
            continue

    if last_error is not None:
        raise last_error
    return collected


def _bucket_location(bucket: dict[str, Any]) -> str:
    location = str(bucket.get("location", "")).strip()
    if location:
        return location
    name = str(bucket.get("name", ""))
    match = re.search(r"/locations/([^/]+)/buckets/", name)
    return match.group(1) if match else ""


def _extract_pubsub_topics(sinks: list[dict[str, Any]]) -> list[str]:
    topics: list[str] = []
    for sink in sinks:
        destination = str(sink.get("destination", ""))
        match = PUBSUB_DEST_PATTERN.match(destination)
        if not match:
            continue
        topic_name = match.group("topic")
        if topic_name not in topics:
            topics.append(topic_name)
    return topics


def _get_topic(
    service: object,
    topic_ref: str,
    *,
    pubsub_client: Any | None = None,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    client_error: Exception | None = None
    if pubsub_client is not None:
        try:
            return _get_topic_with_client(pubsub_client, topic_ref, timeout_seconds)
        except Exception as exc:  # noqa: BLE001
            client_error = exc
    service = resolve_fallback_service(
        service,
        client_error,
        "Pub/Sub discovery service unavailable",
    )
    return _get_topic_with_service(service, topic_ref)


def _get_topic_with_client(
    client: Any,
    topic_ref: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    topic = client.get_topic(topic=topic_ref, timeout=max(1, timeout_seconds))
    return protobuf_to_dict(topic)


def _get_topic_with_service(service: Any, topic_ref: str) -> dict[str, Any]:
    payload = service.projects().topics().get(topic=topic_ref).execute()
    return cast(dict[str, Any], payload)


def _list_topic_subscriptions(
    service: object,
    topic_ref: str,
    *,
    pubsub_client: Any | None = None,
    timeout_seconds: int = 60,
) -> list[str]:
    client_error: Exception | None = None
    if pubsub_client is not None:
        try:
            return _list_topic_subscriptions_with_client(
                pubsub_client,
                topic_ref,
                timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            client_error = exc
    service = resolve_fallback_service(
        service,
        client_error,
        "Pub/Sub discovery service unavailable",
    )
    return _list_topic_subscriptions_with_service(service, topic_ref)


def _list_topic_subscriptions_with_client(
    client: Any,
    topic_ref: str,
    timeout_seconds: int,
) -> list[str]:
    subscriptions = client.list_topic_subscriptions(
        topic=topic_ref,
        timeout=max(1, timeout_seconds),
    )
    return [str(subscription) for subscription in subscriptions]


def _list_topic_subscriptions_with_service(service: Any, topic_ref: str) -> list[str]:
    request = service.projects().topics().subscriptions().list(topic=topic_ref, pageSize=100)
    collected: list[str] = []
    while request is not None:
        response = request.execute()
        collected.extend(response.get("subscriptions", []))
        request = service.projects().topics().subscriptions().list_next(request, response)
    return collected


def _collect_logging_metrics(
    monitoring: Any | None,
    project: str,
    buckets: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], str]:
    if monitoring is None:
        return {}, "monitoring client unavailable for logging metrics"

    try:
        ingested = _fetch_time_series(
            monitoring=monitoring,
            project=project,
            metric_type="logging.googleapis.com/billing/bytes_ingested",
            aligner="ALIGN_SUM",
            alignment_period="3600s",
            reducer="REDUCE_SUM",
        )
        bucket_ingested = _fetch_time_series(
            monitoring=monitoring,
            project=project,
            metric_type="logging.googleapis.com/billing/log_bucket_bytes_ingested",
            aligner="ALIGN_SUM",
            alignment_period="3600s",
            reducer="REDUCE_SUM",
            group_by_fields=[
                "metric.labels.log_bucket_id",
                "metric.labels.log_bucket_location",
            ],
        )
        stored = _fetch_time_series(
            monitoring=monitoring,
            project=project,
            metric_type="logging.googleapis.com/billing/bytes_stored",
            aligner="ALIGN_MAX",
            alignment_period="3600s",
            reducer="REDUCE_SUM",
            group_by_fields=[
                "metric.labels.log_bucket_id",
                "metric.labels.log_bucket_location",
                "metric.labels.data_type",
            ],
        )
    except HttpError as exc:
        return {}, str(exc)
    except Exception as exc:  # noqa: BLE001
        return {}, str(exc)

    ingested_values = [value for series in ingested for value in _extract_point_values(series)]
    stored_values = [value for series in stored for value in _extract_point_values(series)]
    bucket_ingestion = _bucket_ingestion_rows(bucket_ingested)
    stored_metric_status = _bytes_stored_metric_status(stored_values, buckets)
    metrics_error = ""
    if not ingested_values and not stored_values and not bucket_ingestion:
        metrics_error = "logging billing metrics not found"
    return (
        {
            "window_days": 7,
            "bytes_ingested_total": sum(ingested_values),
            "bytes_ingested_peak_hour": _peak_sum_by_timestamp(ingested),
            "bytes_stored_peak": _peak_sum_by_timestamp(stored) if stored_values else None,
            "bytes_stored_metric_status": stored_metric_status,
            "bytes_stored_metric_scope": "retention_beyond_default_30_days",
            "bucket_ingestion": bucket_ingestion,
            "bucket_storage": _bucket_storage_rows(stored),
        },
        metrics_error,
    )


def _bytes_stored_metric_status(
    stored_values: list[float],
    buckets: list[dict[str, Any]] | None,
) -> str:
    if stored_values:
        return "available"
    if buckets is not None and not _has_chargeable_extended_retention_bucket(buckets):
        return "not_applicable"
    return "metric_not_returned"


def _has_chargeable_extended_retention_bucket(buckets: list[dict[str, Any]]) -> bool:
    for bucket in buckets:
        if _log_bucket_id(bucket) == "_Required":
            continue
        retention_days = _optional_int(bucket.get("retentionDays"))
        if retention_days is not None and retention_days > 30:
            return True
    return False


def _log_bucket_id(bucket: dict[str, Any]) -> str:
    name = str(bucket.get("name", "")).strip()
    if name:
        return name.rsplit("/", 1)[-1]
    return str(bucket.get("bucketId", "")).strip()


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _collect_pubsub_metrics(
    monitoring: Any | None,
    project: str,
    subscriptions: list[str],
) -> tuple[list[dict[str, Any]], str]:
    if monitoring is None:
        return [], "monitoring client unavailable for pub/sub metrics"
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for subscription in subscriptions:
        subscription_id = subscription.rsplit("/", 1)[-1]
        if not subscription_id:
            continue
        filter_suffix = f'resource.labels.subscription_id="{subscription_id}"'
        try:
            unacked = _fetch_time_series(
                monitoring=monitoring,
                project=project,
                metric_type="pubsub.googleapis.com/subscription/num_unacked_messages_by_region",
                aligner="ALIGN_MAX",
                alignment_period="300s",
                filter_suffix=filter_suffix,
            )
            oldest = _fetch_time_series(
                monitoring=monitoring,
                project=project,
                metric_type=(
                    "pubsub.googleapis.com/subscription/oldest_unacked_message_age_by_region"
                ),
                aligner="ALIGN_MAX",
                alignment_period="300s",
                filter_suffix=filter_suffix,
            )
            health = _fetch_time_series(
                monitoring=monitoring,
                project=project,
                metric_type="pubsub.googleapis.com/subscription/delivery_latency_health_score",
                aligner="ALIGN_FRACTION_TRUE",
                alignment_period="300s",
                filter_suffix=filter_suffix,
            )
            dead_letter = _fetch_time_series(
                monitoring=monitoring,
                project=project,
                metric_type="pubsub.googleapis.com/subscription/dead_letter_message_count",
                aligner="ALIGN_SUM",
                alignment_period="3600s",
                filter_suffix=filter_suffix,
            )
        except HttpError as exc:
            errors.append(f"{subscription_id}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{subscription_id}: {exc}")
            continue

        unacked_values = [value for series in unacked for value in _extract_point_values(series)]
        oldest_values = [value for series in oldest for value in _extract_point_values(series)]
        health_values = [value for series in health for value in _extract_point_values(series)]
        dead_letter_values = [
            value for series in dead_letter for value in _extract_point_values(series)
        ]
        rows.append(
            {
                "subscription": subscription,
                "subscription_id": subscription_id,
                "num_unacked_messages_peak": max(unacked_values, default=0.0),
                "oldest_unacked_message_age_peak_seconds": max(oldest_values, default=0.0),
                "delivery_latency_health_score_min": (
                    min(health_values) if health_values else None
                ),
                "dead_letter_message_count_total": sum(dead_letter_values),
            }
        )
    return rows, "; ".join(errors)


def _fetch_time_series(
    monitoring: Any,
    project: str,
    metric_type: str,
    aligner: str,
    alignment_period: str,
    filter_suffix: str = "",
    reducer: str = "",
    group_by_fields: list[str] | None = None,
    page_size: int = _TIME_SERIES_PAGE_SIZE,
    max_pages: int = _TIME_SERIES_MAX_PAGES,
    max_series: int = _TIME_SERIES_MAX_SERIES,
) -> list[dict[str, Any]]:
    filter_text = f'metric.type="{metric_type}"'
    suffix = filter_suffix.strip()
    if suffix:
        filter_text = f"{filter_text} AND {suffix}"
    interval_start = _rfc3339_days_ago(7)
    interval_end = _rfc3339_days_ago(0)

    service: Any | None = None
    metric_client = _monitoring_metric_client(monitoring)
    if metric_client is not None:
        try:
            return _fetch_time_series_with_client(
                metric_client,
                project=project,
                filter_text=filter_text,
                interval_start=interval_start,
                interval_end=interval_end,
                aligner=aligner,
                alignment_period=alignment_period,
                reducer=reducer,
                group_by_fields=group_by_fields,
                page_size=page_size,
                max_pages=max_pages,
                max_series=max_series,
                timeout_seconds=_monitoring_timeout_seconds(monitoring),
            )
        except Exception:  # noqa: BLE001
            service = _monitoring_service(monitoring)
            if service is None:
                raise

    if service is None:
        service = _monitoring_service(monitoring)
    if service is None:
        msg = "monitoring discovery service unavailable"
        raise RuntimeError(msg)
    return _fetch_time_series_with_service(
        service,
        project=project,
        filter_text=filter_text,
        interval_start=interval_start,
        interval_end=interval_end,
        aligner=aligner,
        alignment_period=alignment_period,
        reducer=reducer,
        group_by_fields=group_by_fields,
        page_size=page_size,
        max_pages=max_pages,
        max_series=max_series,
    )


def _fetch_time_series_with_client(
    metric_client: Any,
    *,
    project: str,
    filter_text: str,
    interval_start: str,
    interval_end: str,
    aligner: str,
    alignment_period: str,
    reducer: str,
    group_by_fields: list[str] | None,
    page_size: int,
    max_pages: int,
    max_series: int,
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    return list_monitoring_time_series(
        metric_client,
        project=project,
        filter_text=filter_text,
        start_text=interval_start,
        end_text=interval_end,
        aligner=aligner,
        alignment_period=alignment_period,
        reducer=reducer,
        group_by_fields=group_by_fields,
        page_size=page_size,
        max_pages=max_pages,
        max_series=max_series,
        timeout_seconds=timeout_seconds,
    )


def _fetch_time_series_with_service(
    monitoring: Any,
    *,
    project: str,
    filter_text: str,
    interval_start: str,
    interval_end: str,
    aligner: str,
    alignment_period: str,
    reducer: str,
    group_by_fields: list[str] | None,
    page_size: int,
    max_pages: int,
    max_series: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page_token = ""
    page_count = 0
    while True:
        kwargs: dict[str, Any] = {
            "name": f"projects/{project}",
            "filter": filter_text,
            "interval_startTime": interval_start,
            "interval_endTime": interval_end,
            "view": "FULL",
            "pageSize": max(1, page_size),
            "aggregation_alignmentPeriod": alignment_period,
            "aggregation_perSeriesAligner": aligner,
        }
        if reducer:
            kwargs["aggregation_crossSeriesReducer"] = reducer
        if group_by_fields:
            kwargs["aggregation_groupByFields"] = group_by_fields
        if page_token:
            kwargs["pageToken"] = page_token
        response = monitoring.projects().timeSeries().list(**kwargs).execute()
        page_count += 1
        rows.extend(_as_dict_list(response.get("timeSeries")))
        page_token = str(response.get("nextPageToken", "")).strip()
        if not page_token or page_count >= max_pages or len(rows) >= max_series:
            return rows[:max_series]


def _monitoring_service(monitoring: Any) -> Any | None:
    if isinstance(monitoring, _MonitoringClients):
        service = monitoring.service
    else:
        service = monitoring
    if callable(service):
        return service()
    return service


def _monitoring_metric_client(monitoring: Any) -> Any | None:
    if isinstance(monitoring, _MonitoringClients):
        return monitoring.metric_client
    return None


def _monitoring_timeout_seconds(monitoring: Any) -> int:
    if isinstance(monitoring, _MonitoringClients):
        return max(1, monitoring.timeout_seconds)
    return 60


def _extract_point_values(series: dict[str, Any]) -> list[float]:
    values: list[float] = []
    for point in _as_dict_list(series.get("points")):
        value_block = _as_dict(point.get("value"))
        candidate = value_block.get("doubleValue")
        if candidate is None:
            candidate = value_block.get("int64Value")
        if isinstance(candidate, (int, float)):
            values.append(float(candidate))
        elif isinstance(candidate, str):
            try:
                values.append(float(candidate))
            except ValueError:
                continue
    return values


def _peak_sum_by_timestamp(series_list: list[dict[str, Any]]) -> float:
    values_by_time: dict[str, float] = {}
    untimed_values: list[float] = []
    for series in series_list:
        for timestamp, value in _extract_timed_point_values(series):
            if timestamp:
                values_by_time[timestamp] = values_by_time.get(timestamp, 0.0) + value
            else:
                untimed_values.append(value)
    return max(values_by_time.values(), default=max(untimed_values, default=0.0))


def _bucket_storage_rows(series_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str, str], dict[str, float]] = {}
    for series in series_list:
        metric_labels = _as_dict(_as_dict(series.get("metric")).get("labels"))
        bucket_id = str(metric_labels.get("log_bucket_id", "")).strip()
        if not bucket_id:
            continue
        key = (
            bucket_id,
            str(metric_labels.get("log_bucket_location", "")).strip() or "n/a",
            str(metric_labels.get("data_type", "")).strip() or "n/a",
        )
        values_by_time = buckets.setdefault(key, {})
        for timestamp, value in _extract_timed_point_values(series):
            if timestamp:
                values_by_time[timestamp] = values_by_time.get(timestamp, 0.0) + value
            else:
                values_by_time[""] = max(values_by_time.get("", 0.0), value)

    rows = [
        {
            "bucket": bucket_id,
            "location": location,
            "data_type": data_type,
            "bytes_stored_peak": max(values_by_time.values(), default=0.0),
        }
        for (bucket_id, location, data_type), values_by_time in buckets.items()
    ]
    return sorted(rows, key=lambda row: float(row.get("bytes_stored_peak", 0.0)), reverse=True)


def _bucket_ingestion_rows(series_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for series in series_list:
        metric_labels = _as_dict(_as_dict(series.get("metric")).get("labels"))
        bucket_id = str(metric_labels.get("log_bucket_id", "")).strip()
        if not bucket_id:
            continue
        key = (
            bucket_id,
            str(metric_labels.get("log_bucket_location", "")).strip() or "n/a",
        )
        bucket = buckets.setdefault(
            key,
            {
                "values_by_time": {},
                "series_count": 0,
            },
        )
        bucket["series_count"] = int(bucket["series_count"]) + 1
        values_by_time = cast(dict[str, float], bucket["values_by_time"])
        for timestamp, value in _extract_timed_point_values(series):
            key_time = timestamp or ""
            values_by_time[key_time] = values_by_time.get(key_time, 0.0) + value

    rows: list[dict[str, Any]] = []
    for (bucket_id, location), bucket in buckets.items():
        values_by_time = cast(dict[str, float], bucket["values_by_time"])
        rows.append(
            {
                "bucket": bucket_id,
                "location": location,
                "bytes_ingested_total": sum(values_by_time.values()),
                "bytes_ingested_peak_hour": max(values_by_time.values(), default=0.0),
                "series_count": int(bucket["series_count"]),
            }
        )
    return sorted(
        rows,
        key=lambda row: float(row.get("bytes_ingested_total", 0.0)),
        reverse=True,
    )


def _extract_timed_point_values(series: dict[str, Any]) -> list[tuple[str, float]]:
    values: list[tuple[str, float]] = []
    for point in _as_dict_list(series.get("points")):
        value_block = _as_dict(point.get("value"))
        candidate = value_block.get("doubleValue")
        if candidate is None:
            candidate = value_block.get("int64Value")
        if isinstance(candidate, (int, float)):
            numeric_value = float(candidate)
        elif isinstance(candidate, str):
            try:
                numeric_value = float(candidate)
            except ValueError:
                continue
        else:
            continue
        interval = _as_dict(point.get("interval"))
        timestamp = str(interval.get("endTime", "") or interval.get("startTime", ""))
        values.append((timestamp, numeric_value))
    return values


def _rfc3339_days_ago(days: int) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(tz=UTC) - timedelta(days=days)).isoformat().replace("+00:00", "Z")


def _splunk_hints(
    sinks: list[dict[str, Any]], topic_checks: list[dict[str, Any]]
) -> dict[str, Any]:
    tokens: list[str] = []
    for sink in sinks:
        tokens.extend(
            [
                str(sink.get("name", "")),
                str(sink.get("destination", "")),
                str(sink.get("filter", "")),
            ]
        )
    for topic in topic_checks:
        tokens.append(str(topic.get("topic", "")))
        for sub_name in topic.get("sample_subscriptions", []):
            tokens.append(str(sub_name))

    matched = [token for token in tokens if "splunk" in token.lower()]
    return {
        "matched": bool(matched),
        "samples": matched[:10],
    }


def _str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if isinstance(item, str)]
    return []


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []
