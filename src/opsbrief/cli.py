from __future__ import annotations

import argparse
import json
import multiprocessing
import tempfile
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, cast

from opsbrief.branding import BrandProfile, load_brand_profile
from opsbrief.collectors import COLLECTORS
from opsbrief.config import ClusterConfig, EnvConfig, load_config, load_manual_input
from opsbrief.models import SEVERITY_SCORE, CheckResult, Report, Status, now_utc_iso
from opsbrief.reporting import (
    ensure_report_directory,
    read_report_json,
    write_collector_evidence,
    write_preflight_evidence,
    write_report_html,
    write_report_json,
    write_report_markdown,
    write_report_pdf,
)


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "list-collectors":
        for name in COLLECTORS:
            print(name)
        return 0

    if args.command == "preflight":
        config = load_config(_require_config(args, parser), env_override=args.env)
        _apply_runtime_overrides(config=config, args=args, parser=parser)
        timeout_seconds = _effective_timeout_seconds(config=config, args=args, parser=parser)
        result = COLLECTORS["preflight"](
            config,
            timeout_seconds=timeout_seconds,
            output_dir=args.output_dir,
            auth_mode=args.auth_mode,
            impersonate_service_account=args.impersonate_service_account,
        )
        _print_result(result)
        evidence_path = write_preflight_evidence(
            result=result,
            output_dir=args.output_dir,
            environment=config.environment,
        )
        print(f"evidence={evidence_path}")
        return _exit_code_for_status(result.status)

    if args.command == "weekly":
        brand = load_brand_profile(args.brand_profile)
        if args.from_report_json:
            include_evidence_index = args.include_evidence_index
            report_mode = args.report_mode or "full"
            include_technical_appendix = bool(args.include_technical_appendix)
            include_collector_warnings_and_gaps = False
            replay_config: EnvConfig | None = None
            if args.config:
                replay_config = load_config(args.config, env_override=args.env)
                include_evidence_index = _include_evidence_index(replay_config, args)
                report_mode = _report_mode(replay_config, args)
                include_technical_appendix = _include_technical_appendix(replay_config, args)
                include_collector_warnings_and_gaps = _include_collector_warnings_and_gaps(
                    replay_config
                )
            report = read_report_json(args.from_report_json)
            if replay_config is not None:
                _apply_configured_collector_scope(report, replay_config)
            if args.manual_input:
                report.manual_input = load_manual_input(args.manual_input)
            paths = _write_weekly_artifacts(
                report=report,
                output_dir=args.output_dir,
                brand=brand,
                include_evidence_index=include_evidence_index,
                report_mode=report_mode,
                include_technical_appendix=include_technical_appendix,
                include_collector_warnings_and_gaps=include_collector_warnings_and_gaps,
            )
            print(f"replayed_from={Path(args.from_report_json)}")
            _print_weekly_artifacts(report=report, paths=paths)
            return _exit_code_for_status(report.overall_status)

        config = load_config(_require_config(args, parser), env_override=args.env)
        _apply_runtime_overrides(config=config, args=args, parser=parser)
        timeout_seconds = _effective_timeout_seconds(config=config, args=args, parser=parser)
        selected = _resolve_collectors(args.collectors, args.skip_collectors, config)
        manual_input = load_manual_input(args.manual_input)
        report = _run_weekly(
            config=config,
            selected_collectors=selected,
            manual_input=manual_input,
            output_dir=args.output_dir,
            timeout_seconds=timeout_seconds,
            auth_mode=args.auth_mode,
            impersonate_service_account=args.impersonate_service_account,
        )
        paths = _write_weekly_artifacts(
            report=report,
            output_dir=args.output_dir,
            brand=brand,
            include_evidence_index=_include_evidence_index(config, args),
            report_mode=_report_mode(config, args),
            include_technical_appendix=_include_technical_appendix(config, args),
            include_collector_warnings_and_gaps=_include_collector_warnings_and_gaps(config),
        )
        _print_weekly_artifacts(report=report, paths=paths)
        return _exit_code_for_status(report.overall_status)

    parser.print_help()
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opsbrief")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list-collectors", help="List available collectors")

    preflight = sub.add_parser("preflight", help="Run preflight checks")
    _add_common_args(preflight)
    preflight.add_argument("--output-dir", default="./reports", help="Output directory")

    weekly = sub.add_parser("weekly", help="Run weekly report flow")
    _add_common_args(weekly)
    weekly.add_argument("--output-dir", default="./reports", help="Output directory")
    weekly.add_argument("--collectors", default="", help="Comma-separated collector names")
    weekly.add_argument("--skip-collectors", default="", help="Comma-separated collector names")
    weekly.add_argument("--manual-input", default="", help="YAML file for manual sections")
    weekly.add_argument(
        "--brand-profile",
        default="",
        help="YAML file containing report brand title, colors, and font settings",
    )
    weekly.add_argument(
        "--from-report-json",
        default="",
        help=(
            "Regenerate weekly artifacts from a previous opsbrief weekly report JSON "
            "without running collectors"
        ),
    )
    weekly.add_argument(
        "--include-evidence-index",
        action="store_true",
        help="Include the Evidence Index section in rendered weekly reports",
    )
    weekly.add_argument(
        "--report-mode",
        choices=("full", "concise"),
        default="",
        help=(
            "Report rendering mode. "
            "full includes complete technical sections; concise focuses on high-signal sections."
        ),
    )
    weekly.add_argument(
        "--include-technical-appendix",
        action="store_true",
        help=("When used with --report-mode concise, append full technical detail as an appendix."),
    )

    return parser


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default="", help="Path to YAML config")
    parser.add_argument("--env", default="", help="Environment override")
    parser.add_argument(
        "--project",
        action="append",
        default=[],
        help="Project ID override (repeatable). Example: --project my-gcp-project-id",
    )
    parser.add_argument(
        "--region",
        default="",
        help="Default region override used for discovery and shorthand cluster arguments",
    )
    parser.add_argument(
        "--cluster",
        action="append",
        default=[],
        help=("Cluster selector (repeatable). Supported forms: NAME or PROJECT/REGION/NAME"),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=None,
        help="Per-command timeout. If omitted, config timeout_seconds is used.",
    )
    parser.add_argument(
        "--auth-mode",
        default="auto",
        choices=("auto", "adc", "metadata", "impersonation"),
        help="Credential source mode",
    )
    parser.add_argument(
        "--impersonate-service-account",
        default="",
        help="Service account email used when --auth-mode impersonation",
    )
    parser.add_argument(
        "--trend-days",
        type=int,
        default=0,
        help=(
            "Trend window in days for trend metrics collector. "
            "If omitted, config value is used (default 7)."
        ),
    )


def _require_config(args: argparse.Namespace, parser: argparse.ArgumentParser) -> str:
    config_path = str(getattr(args, "config", "") or "").strip()
    if not config_path:
        if getattr(args, "command", "") == "weekly":
            parser.error("--config is required unless --from-report-json is used")
        parser.error("--config is required")
    return config_path


def _apply_runtime_overrides(
    config: EnvConfig,
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> None:
    region_override = str(getattr(args, "region", "") or "").strip()
    if region_override:
        config.default_region = region_override

    project_args = _dedupe_non_empty(getattr(args, "project", []))
    if project_args:
        config.projects = {
            f"runtime_{idx + 1}": project for idx, project in enumerate(project_args)
        }

    cluster_args = _dedupe_non_empty(getattr(args, "cluster", []))
    if cluster_args:
        config.clusters = _parse_runtime_clusters(
            cluster_args=cluster_args,
            default_project=_first_project(config),
            default_region=config.default_region,
            parser=parser,
        )
        config.discovery.auto_discover_clusters = False

    trend_days = int(getattr(args, "trend_days", 0) or 0)
    if trend_days:
        if trend_days < 1 or trend_days > 365:
            parser.error("--trend-days must be between 1 and 365")
        config.time_windows.trend_days = trend_days


def _effective_timeout_seconds(
    config: EnvConfig,
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    timeout_arg = getattr(args, "timeout_seconds", None)
    if timeout_arg is not None:
        timeout_arg = int(timeout_arg)
        if timeout_arg < 1:
            parser.error("--timeout-seconds must be at least 1")
        return timeout_arg
    return config.timeout_seconds


def _dedupe_non_empty(items: list[str]) -> list[str]:
    out: list[str] = []
    for raw in items:
        value = str(raw).strip()
        if value and value not in out:
            out.append(value)
    return out


def _parse_runtime_clusters(
    cluster_args: list[str],
    default_project: str,
    default_region: str,
    parser: argparse.ArgumentParser,
) -> list[ClusterConfig]:
    clusters: list[ClusterConfig] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in cluster_args:
        token = raw.strip()
        if "/" in token:
            parts = [part.strip() for part in token.split("/")]
            if len(parts) != 3 or any(not part for part in parts):
                parser.error(f"invalid --cluster '{raw}'. Use NAME or PROJECT/REGION/NAME")
            project, region, name = parts
        else:
            if not default_project:
                parser.error(
                    f"--cluster '{raw}' needs PROJECT/REGION/NAME because no project is set"
                )
            if not default_region:
                parser.error(
                    f"--cluster '{raw}' needs PROJECT/REGION/NAME because no default region is set"
                )
            project, region, name = default_project, default_region, token

        key = (project, region, name)
        if key in seen:
            continue
        seen.add(key)
        clusters.append(ClusterConfig(name=name, project=project, region=region))
    return clusters


def _first_project(config: EnvConfig) -> str:
    if config.projects:
        return next(iter(config.projects.values()))
    if config.clusters:
        return config.clusters[0].project
    return ""


def _resolve_collectors(collectors_arg: str, skip_arg: str, config: EnvConfig) -> list[str]:
    if collectors_arg.strip():
        selected = [item.strip() for item in collectors_arg.split(",") if item.strip()]
    else:
        selected = [name for name, enabled in config.collectors.items() if enabled]
        if not selected:
            selected = list(COLLECTORS.keys())

    skipped = {item.strip() for item in skip_arg.split(",") if item.strip()}
    filtered = [name for name in selected if name not in skipped]

    unknown = [name for name in filtered if name not in COLLECTORS]
    if unknown:
        raise ValueError(f"Unknown collectors: {', '.join(unknown)}")
    return filtered


def _apply_configured_collector_scope(report: Report, config: EnvConfig) -> None:
    if not config.collectors:
        return
    disabled = {name for name, enabled in config.collectors.items() if not enabled}
    if not disabled:
        return
    report.collectors = [item for item in report.collectors if item.collector not in disabled]


def _run_weekly(
    config: EnvConfig,
    selected_collectors: list[str],
    manual_input: dict[str, object],
    output_dir: str,
    timeout_seconds: int,
    auth_mode: str,
    impersonate_service_account: str,
) -> Report:
    results: list[CheckResult] = []
    collector_kwargs = {
        "timeout_seconds": timeout_seconds,
        "output_dir": output_dir,
        "auth_mode": auth_mode,
        "impersonate_service_account": impersonate_service_account,
    }
    for collector_name in selected_collectors:
        collector_fn: Callable[..., CheckResult] = COLLECTORS[collector_name]
        result = _run_collector_with_timeout(
            collector_name=collector_name,
            collector_fn=collector_fn,
            config=config,
            collector_kwargs=collector_kwargs,
            timeout_seconds=timeout_seconds,
        )
        results.append(result)
        _print_result(result)

    today = date.today()
    iso = today.isocalendar()
    return Report(
        environment=config.environment,
        generated_at=now_utc_iso(),
        iso_year=iso.year,
        iso_week=iso.week,
        collectors=results,
        manual_input=manual_input,
    )


def _run_collector_with_timeout(
    collector_name: str,
    collector_fn: Callable[..., CheckResult],
    config: EnvConfig,
    collector_kwargs: dict[str, object],
    timeout_seconds: int,
) -> CheckResult:
    print(f"collector_start={collector_name}", flush=True)
    started_at = now_utc_iso()
    started_monotonic = time.monotonic()
    timeout = max(1, timeout_seconds)
    try:
        with tempfile.TemporaryDirectory(prefix="opsbrief-collector-") as temp_dir:
            result_path = Path(temp_dir) / "result.json"
            context = multiprocessing.get_context("spawn")
            process = context.Process(
                target=_collector_process_main,
                args=(collector_name, collector_fn, config, collector_kwargs, str(result_path)),
            )
            process.start()
            process.join(timeout)

            if process.is_alive():
                process.terminate()
                process.join(5)
                if process.is_alive():
                    process.kill()
                    process.join(5)
                return _collector_failure_result(
                    collector_name=collector_name,
                    started_at=started_at,
                    summary=f"Collector timed out after {timeout} second(s)",
                    error=f"{collector_name} did not finish within {timeout} second(s)",
                    details={"timeout_seconds": timeout},
                )

            return _read_collector_process_result(
                collector_name=collector_name,
                result_path=result_path,
                exit_code=process.exitcode,
                started_at=started_at,
            )
    finally:
        elapsed = time.monotonic() - started_monotonic
        print(f"collector_elapsed_seconds={collector_name}:{elapsed:.1f}", flush=True)


def _collector_process_main(
    collector_name: str,
    collector_fn: Callable[..., CheckResult],
    config: EnvConfig,
    collector_kwargs: dict[str, object],
    result_path: str,
) -> None:
    try:
        result = collector_fn(config, **collector_kwargs)
        payload: dict[str, Any] = {"ok": True, "result": result.to_dict()}
    except BaseException as exc:  # noqa: BLE001
        payload = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "collector": collector_name,
        }
    Path(result_path).write_text(json.dumps(payload), encoding="utf-8")


def _read_collector_process_result(
    collector_name: str,
    result_path: Path,
    exit_code: int | None,
    started_at: str,
) -> CheckResult:
    if not result_path.exists():
        exit_text = "unknown" if exit_code is None else str(exit_code)
        return _collector_failure_result(
            collector_name=collector_name,
            started_at=started_at,
            summary="Collector subprocess exited without a result",
            error=f"{collector_name} subprocess exit code: {exit_text}",
        )

    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return _collector_failure_result(
            collector_name=collector_name,
            started_at=started_at,
            summary="Collector subprocess wrote an unreadable result",
            error=f"{type(exc).__name__}: {exc}",
        )

    if not isinstance(payload, dict):
        return _collector_failure_result(
            collector_name=collector_name,
            started_at=started_at,
            summary="Collector subprocess wrote an invalid result",
            error=f"{collector_name} result payload was not an object",
        )

    if payload.get("ok") is True:
        result_payload = payload.get("result")
        if isinstance(result_payload, dict):
            return CheckResult.from_dict(cast(dict[str, Any], result_payload))
        return _collector_failure_result(
            collector_name=collector_name,
            started_at=started_at,
            summary="Collector subprocess wrote an invalid result",
            error=f"{collector_name} result payload did not include a collector result",
        )

    error = str(payload.get("error") or "collector subprocess failed")
    return _collector_failure_result(
        collector_name=collector_name,
        started_at=started_at,
        summary="Collector subprocess failed",
        error=error,
    )


def _collector_failure_result(
    collector_name: str,
    started_at: str,
    summary: str,
    error: str,
    details: dict[str, Any] | None = None,
) -> CheckResult:
    return CheckResult(
        collector=collector_name,
        status=Status.FAILED,
        summary=summary,
        details=details or {},
        errors=[error],
        started_at=started_at,
        finished_at=now_utc_iso(),
    )


def _write_weekly_artifacts(
    report: Report,
    output_dir: str,
    brand: BrandProfile,
    include_evidence_index: bool = False,
    report_mode: str = "full",
    include_technical_appendix: bool = False,
    include_collector_warnings_and_gaps: bool = False,
) -> dict[str, Path]:
    report_dir = ensure_report_directory(
        output_dir=output_dir,
        environment=report.environment,
        iso_year=report.iso_year,
        iso_week=report.iso_week,
    )
    return {
        "markdown": write_report_markdown(
            report,
            report_dir,
            brand,
            include_evidence_index=include_evidence_index,
            report_mode=report_mode,
            include_technical_appendix=include_technical_appendix,
            include_collector_warnings_and_gaps=include_collector_warnings_and_gaps,
        ),
        "html": write_report_html(
            report,
            report_dir,
            brand,
            include_evidence_index=include_evidence_index,
            report_mode=report_mode,
            include_technical_appendix=include_technical_appendix,
            include_collector_warnings_and_gaps=include_collector_warnings_and_gaps,
        ),
        "pdf": write_report_pdf(
            report,
            report_dir,
            brand,
            include_evidence_index=include_evidence_index,
            report_mode=report_mode,
            include_technical_appendix=include_technical_appendix,
            include_collector_warnings_and_gaps=include_collector_warnings_and_gaps,
        ),
        "json": write_report_json(report, report_dir),
        "evidence": write_collector_evidence(report, report_dir),
    }


def _include_evidence_index(config: EnvConfig, args: argparse.Namespace) -> bool:
    if args.include_evidence_index:
        return True
    return config.reporting.include_evidence_index


def _report_mode(config: EnvConfig, args: argparse.Namespace) -> str:
    mode = str(getattr(args, "report_mode", "") or "").strip().lower()
    if mode:
        return mode
    return config.reporting.report_mode


def _include_technical_appendix(config: EnvConfig, args: argparse.Namespace) -> bool:
    if bool(getattr(args, "include_technical_appendix", False)):
        return True
    return config.reporting.include_technical_appendix


def _include_collector_warnings_and_gaps(config: EnvConfig) -> bool:
    return config.reporting.include_collector_warnings_and_gaps


def _print_weekly_artifacts(report: Report, paths: dict[str, Path]) -> None:
    print(f"overall_status={report.overall_status.value}")
    print(f"markdown={paths['markdown']}")
    print(f"html={paths['html']}")
    print(f"pdf={paths['pdf']}")
    print(f"json={paths['json']}")
    print(f"evidence={paths['evidence']}")


def _print_result(result: CheckResult) -> None:
    print(f"[{result.collector}] status={result.status.value} summary={result.summary}")
    if result.errors:
        for error in result.errors:
            print(f"  error: {error}")


def _exit_code_for_status(status: Status) -> int:
    if SEVERITY_SCORE[status] >= SEVERITY_SCORE[Status.CRITICAL]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
