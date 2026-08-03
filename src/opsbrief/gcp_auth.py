from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Literal

import google.auth
from google.auth import impersonated_credentials
from google.auth.credentials import Credentials
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials as UserTokenCredentials

AuthMode = Literal["auto", "adc", "metadata", "impersonation"]
SCOPE = "https://www.googleapis.com/auth/cloud-platform"


@dataclass(slots=True)
class AuthBundle:
    credentials: Credentials
    default_project: str | None
    principal_hint: str


def get_auth_bundle(
    auth_mode: AuthMode = "auto",
    impersonate_service_account: str = "",
) -> AuthBundle:
    if auth_mode == "impersonation" and not impersonate_service_account:
        raise ValueError("--auth-mode impersonation requires --impersonate-service-account")

    default_project: str | None = None
    source_creds: Credentials
    try:
        source_creds, default_project = google.auth.default(scopes=[SCOPE])
        if not source_creds.valid:
            source_creds.refresh(Request())
    except Exception:
        if auth_mode != "auto":
            raise
        source_creds, default_project = _fallback_to_gcloud_token()

    if auth_mode == "impersonation":
        target_creds = impersonated_credentials.Credentials(
            source_credentials=source_creds,
            target_principal=impersonate_service_account,
            target_scopes=[SCOPE],
            lifetime=3600,
        )
        target_creds.refresh(Request())
        return AuthBundle(
            credentials=target_creds,
            default_project=default_project,
            principal_hint=impersonate_service_account,
        )

    principal_hint = getattr(source_creds, "service_account_email", "")
    if not principal_hint and auth_mode == "auto":
        principal_hint = _active_gcloud_account()
    return AuthBundle(
        credentials=source_creds,
        default_project=default_project,
        principal_hint=principal_hint or "active-default-credentials",
    )


def _fallback_to_gcloud_token() -> tuple[Credentials, str | None]:
    token = _run_stdout(["gcloud", "auth", "print-access-token", "--quiet"])
    if not token:
        raise RuntimeError(
            "ADC unavailable and gcloud access token fallback failed. "
            "Run `gcloud auth application-default login` or pass --auth-mode impersonation."
        )

    project = _run_stdout(["gcloud", "config", "get-value", "project"])
    creds = UserTokenCredentials(token=token, scopes=[SCOPE])
    return creds, project or None


def _active_gcloud_account() -> str:
    account = _run_stdout(
        ["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"]
    )
    return account or "active-default-credentials"


def _run_stdout(cmd: list[str]) -> str:
    result = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()
