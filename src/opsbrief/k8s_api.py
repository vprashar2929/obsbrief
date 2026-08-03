from __future__ import annotations

import base64
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from kubernetes import client as k8s_client
from kubernetes.client import exceptions as k8s_exceptions

from opsbrief.models import Status


@contextmanager
def cluster_ca_file(ca_cert_b64: str) -> Iterator[Path]:
    decoded = base64.b64decode(ca_cert_b64)
    handle = tempfile.NamedTemporaryFile(mode="wb", delete=False)
    try:
        handle.write(decoded)
        handle.flush()
        handle.close()
        path = Path(handle.name)
        try:
            yield path
        finally:
            path.unlink(missing_ok=True)
    finally:
        if not handle.closed:
            handle.close()


def preferred_endpoints(dns_endpoint: str, ip_endpoint: str) -> list[tuple[str, bool]]:
    candidates: list[tuple[str, bool]] = []
    for endpoint, use_cluster_ca in ((dns_endpoint, False), (ip_endpoint, True)):
        normalized = endpoint.strip()
        if not normalized:
            continue
        if normalized.startswith("https://"):
            normalized = normalized.removeprefix("https://")
        if not any(normalized == existing[0] for existing in candidates):
            candidates.append((normalized, use_cluster_ca))
    return candidates


def api_client(endpoint: str, token: str, ca_path: Path | None) -> k8s_client.ApiClient:
    cfg = k8s_client.Configuration()
    cfg.host = endpoint if endpoint.startswith("https://") else f"https://{endpoint}"
    cfg.verify_ssl = True
    if ca_path is not None:
        cfg.ssl_ca_cert = str(ca_path)
    cfg.api_key = {"authorization": token}
    cfg.api_key_prefix = {"authorization": "Bearer"}
    return k8s_client.ApiClient(configuration=cfg)


def access_token(credentials: Any, *, allow_gcloud_fallback: bool) -> str:
    token = str(getattr(credentials, "token", "") or "").strip()
    if token:
        return token
    try:
        credentials.refresh(Request())
        refreshed = str(getattr(credentials, "token", "") or "").strip()
        if refreshed:
            return refreshed
    except Exception:  # noqa: BLE001
        if not allow_gcloud_fallback:
            return ""
    return _fallback_gcloud_access_token() if allow_gcloud_fallback else ""


def status_for_api_exception(exc: k8s_exceptions.ApiException) -> Status:
    if exc.status in (401, 403):
        return Status.SKIPPED_PERMISSION
    if exc.status == 404:
        return Status.SKIPPED_CONFIG
    if exc.status in (429, 500, 502, 503, 504):
        return Status.SKIPPED_NETWORK
    return Status.FAILED


def _fallback_gcloud_access_token() -> str:
    result = subprocess.run(
        ["gcloud", "auth", "print-access-token", "--quiet"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()
