from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self


class Status(StrEnum):
    OK = "ok"
    WARNING = "warning"
    CRITICAL = "critical"
    SKIPPED_CONFIG = "skipped_config"
    SKIPPED_PERMISSION = "skipped_permission"
    SKIPPED_NETWORK = "skipped_network"
    FAILED = "failed"


SEVERITY_SCORE: dict[Status, int] = {
    Status.OK: 0,
    Status.SKIPPED_CONFIG: 1,
    Status.SKIPPED_PERMISSION: 1,
    Status.SKIPPED_NETWORK: 1,
    Status.WARNING: 2,
    Status.CRITICAL: 3,
    Status.FAILED: 4,
}


@dataclass(slots=True)
class CheckResult:
    collector: str
    status: Status
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: now_utc_iso())
    finished_at: str = field(default_factory=lambda: now_utc_iso())

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Self:
        try:
            status = Status(_required_str(payload, "status"))
        except ValueError as exc:
            raise ValueError(f"Invalid collector status: {payload.get('status')!r}") from exc

        return cls(
            collector=_required_str(payload, "collector"),
            status=status,
            summary=_required_str(payload, "summary"),
            details=_dict_value(payload, "details"),
            errors=_str_list_value(payload, "errors"),
            started_at=_optional_str(payload, "started_at", now_utc_iso()),
            finished_at=_optional_str(payload, "finished_at", now_utc_iso()),
        )


@dataclass(slots=True)
class Report:
    environment: str
    generated_at: str
    iso_year: int
    iso_week: int
    collectors: list[CheckResult]
    manual_input: dict[str, Any] = field(default_factory=dict)

    @property
    def overall_status(self) -> Status:
        if not self.collectors:
            return Status.FAILED
        return max(self.collectors, key=lambda item: SEVERITY_SCORE[item.status]).status

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "environment": self.environment,
            "generated_at": self.generated_at,
            "iso_year": self.iso_year,
            "iso_week": self.iso_week,
            "overall_status": self.overall_status.value,
            "collectors": [item.to_dict() for item in self.collectors],
            "manual_input": self.manual_input,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Self:
        collectors = payload.get("collectors", [])
        if not isinstance(collectors, list):
            raise ValueError("Expected list value for report field: collectors")

        manual_input = payload.get("manual_input", {})
        if not isinstance(manual_input, dict):
            raise ValueError("Expected dict value for report field: manual_input")

        return cls(
            environment=_required_str(payload, "environment"),
            generated_at=_required_str(payload, "generated_at"),
            iso_year=_required_int(payload, "iso_year"),
            iso_week=_required_int(payload, "iso_week"),
            collectors=[_check_result_from_item(item) for item in collectors],
            manual_input=manual_input,
        )


def now_utc_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _check_result_from_item(item: object) -> CheckResult:
    if not isinstance(item, dict):
        raise ValueError("Expected dict item in report field: collectors")
    return CheckResult.from_dict(item)


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Expected non-empty string value for field: {key}")
    return value


def _optional_str(payload: dict[str, Any], key: str, default: str) -> str:
    value = payload.get(key, default)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Expected non-empty string value for field: {key}")
    return value


def _required_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int):
        raise ValueError(f"Expected int value for field: {key}")
    return value


def _dict_value(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"Expected dict value for field: {key}")
    return value


def _str_list_value(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Expected list[str] value for field: {key}")
    return value
