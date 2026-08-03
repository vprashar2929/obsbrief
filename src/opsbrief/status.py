from __future__ import annotations

from googleapiclient.errors import HttpError

from opsbrief.models import Status


def max_status(current: Status, candidate: Status) -> Status:
    order = {
        Status.OK: 0,
        Status.SKIPPED_CONFIG: 1,
        Status.SKIPPED_PERMISSION: 1,
        Status.SKIPPED_NETWORK: 1,
        Status.WARNING: 2,
        Status.CRITICAL: 3,
        Status.FAILED: 4,
    }
    return candidate if order[candidate] > order[current] else current


def classify_http_error(exc: HttpError) -> Status:
    status_code = getattr(exc.resp, "status", 0)
    if status_code in (401, 403):
        return Status.SKIPPED_PERMISSION
    if status_code in (429, 500, 502, 503, 504):
        return Status.SKIPPED_NETWORK
    return Status.FAILED
