from __future__ import annotations

import sys
from pathlib import Path
from typing import NoReturn

from opsbrief.cli import main
from opsbrief.collectors import COLLECTORS
from opsbrief.models import CheckResult, Report, Status
from opsbrief.reporting import ensure_report_directory, read_report_json, write_report_json


def test_bundled_sample_report_fixture_is_replayable() -> None:
    source_report = Path(__file__).parents[1] / "examples" / "sample-report.json"

    report = read_report_json(source_report)

    assert report.environment == "sample"
    assert len(report.collectors) == 12
    assert {item.collector for item in report.collectors} == set(COLLECTORS)


def test_weekly_replays_previous_report_json_without_collectors(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    source_report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="preflight",
                status=Status.OK,
                summary="cached ok",
                details={"checks": []},
            )
        ],
        manual_input={"open_risks": ["old risk"]},
    )
    source_dir = ensure_report_directory(tmp_path / "source", "dev", 2026, 20)
    source_json = write_report_json(source_report, source_dir)
    manual_input = tmp_path / "manual.yaml"
    manual_input.write_text("open_risks:\n  - refreshed risk\n", encoding="utf-8")

    def fail_collector(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("replay mode must not run collectors")

    for collector_name in list(COLLECTORS):
        monkeypatch.setitem(COLLECTORS, collector_name, fail_collector)

    output_dir = tmp_path / "out"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "opsbrief",
            "weekly",
            "--from-report-json",
            str(source_json),
            "--manual-input",
            str(manual_input),
            "--output-dir",
            str(output_dir),
        ],
    )

    assert main() == 0

    report_dir = output_dir / "dev" / "weekly" / "2026-W20"
    markdown_path = report_dir / "opsbrief-dev-weekly-report.md"
    html_path = report_dir / "opsbrief-dev-weekly-report.html"
    pdf_path = report_dir / "opsbrief-dev-weekly-report.pdf"
    json_path = report_dir / "opsbrief-dev-weekly-report.json"
    evidence_path = report_dir / "evidence" / "collector-status.json"

    assert markdown_path.exists()
    assert html_path.exists()
    assert pdf_path.read_bytes().startswith(b"%PDF")
    assert evidence_path.exists()
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "## Evidence Index" not in markdown
    assert "## Assessment Gaps And Action Items" not in markdown

    replayed_report = read_report_json(json_path)
    assert replayed_report.generated_at == "2026-05-14T00:00:00+00:00"
    assert replayed_report.collectors[0].summary == "cached ok"
    assert replayed_report.manual_input == {"open_risks": ["refreshed risk"]}

    output = capsys.readouterr().out
    assert f"replayed_from={source_json}" in output
    assert "overall_status=ok" in output
    assert "[preflight]" not in output


def test_weekly_replay_omits_collectors_disabled_in_config(monkeypatch, tmp_path: Path) -> None:
    source_report = Report(
        environment="shared-mon",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(collector="preflight", status=Status.OK, summary="cached ok"),
            CheckResult(
                collector="mesh",
                status=Status.SKIPPED_CONFIG,
                summary="mesh disabled in config",
                details={"clusters": []},
            ),
        ],
    )
    source_dir = ensure_report_directory(tmp_path / "source", "shared-mon", 2026, 20)
    source_json = write_report_json(source_report, source_dir)
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text(
        "\n".join(
            [
                "environment: shared-mon",
                "default_region: us-central1",
                "projects: {}",
                "collectors:",
                "  preflight: true",
                "  mesh: false",
            ]
        ),
        encoding="utf-8",
    )

    output_dir = tmp_path / "out"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "opsbrief",
            "weekly",
            "--from-report-json",
            str(source_json),
            "--config",
            str(config_path),
            "--output-dir",
            str(output_dir),
        ],
    )

    assert main() == 0

    report_dir = output_dir / "shared-mon" / "weekly" / "2026-W20"
    markdown = (report_dir / "opsbrief-shared-mon-weekly-report.md").read_text(encoding="utf-8")
    replayed_report = read_report_json(report_dir / "opsbrief-shared-mon-weekly-report.json")

    assert [item.collector for item in replayed_report.collectors] == ["preflight"]
    assert "## Service Mesh (Istio)" not in markdown
    assert "mesh disabled in config" not in markdown


def test_weekly_replay_uses_configured_evidence_index(monkeypatch, tmp_path: Path) -> None:
    source_report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[CheckResult(collector="preflight", status=Status.OK, summary="cached ok")],
    )
    source_dir = ensure_report_directory(tmp_path / "source", "dev", 2026, 20)
    source_json = write_report_json(source_report, source_dir)
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text(
        "\n".join(
            [
                "environment: runtime",
                "default_region: us-central1",
                "projects: {}",
                "collectors: {}",
                "reporting:",
                "  include_evidence_index: true",
            ]
        ),
        encoding="utf-8",
    )

    output_dir = tmp_path / "out"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "opsbrief",
            "weekly",
            "--from-report-json",
            str(source_json),
            "--config",
            str(config_path),
            "--output-dir",
            str(output_dir),
        ],
    )

    assert main() == 0

    markdown_path = output_dir / "dev" / "weekly" / "2026-W20" / "opsbrief-dev-weekly-report.md"
    assert "## Evidence Index" in markdown_path.read_text(encoding="utf-8")


def test_weekly_replay_uses_configured_collector_warnings_and_gaps(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source_report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[
            CheckResult(
                collector="preflight",
                status=Status.WARNING,
                summary="cached warning",
                details={"checks": []},
            )
        ],
    )
    source_dir = ensure_report_directory(tmp_path / "source", "dev", 2026, 20)
    source_json = write_report_json(source_report, source_dir)
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text(
        "\n".join(
            [
                "environment: runtime",
                "default_region: us-central1",
                "projects: {}",
                "collectors: {}",
                "reporting:",
                "  include_collector_warnings_and_gaps: true",
            ]
        ),
        encoding="utf-8",
    )

    output_dir = tmp_path / "out"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "opsbrief",
            "weekly",
            "--from-report-json",
            str(source_json),
            "--config",
            str(config_path),
            "--output-dir",
            str(output_dir),
        ],
    )

    assert main() == 0

    markdown_path = output_dir / "dev" / "weekly" / "2026-W20" / "opsbrief-dev-weekly-report.md"
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "## Assessment Gaps And Action Items" not in markdown
    assert "Warning-level findings were observed." not in markdown


def test_weekly_replay_uses_configured_concise_report_mode(monkeypatch, tmp_path: Path) -> None:
    source_report = Report(
        environment="dev",
        generated_at="2026-05-14T00:00:00+00:00",
        iso_year=2026,
        iso_week=20,
        collectors=[CheckResult(collector="preflight", status=Status.OK, summary="cached ok")],
    )
    source_dir = ensure_report_directory(tmp_path / "source", "dev", 2026, 20)
    source_json = write_report_json(source_report, source_dir)
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text(
        "\n".join(
            [
                "environment: runtime",
                "default_region: us-central1",
                "projects: {}",
                "collectors: {}",
                "reporting:",
                "  report_mode: concise",
            ]
        ),
        encoding="utf-8",
    )

    output_dir = tmp_path / "out"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "opsbrief",
            "weekly",
            "--from-report-json",
            str(source_json),
            "--config",
            str(config_path),
            "--output-dir",
            str(output_dir),
        ],
    )

    assert main() == 0

    markdown_path = output_dir / "dev" / "weekly" / "2026-W20" / "opsbrief-dev-weekly-report.md"
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "## Operational Summary (Concise)" in markdown
    assert "## Technical Detail (Ops/Internal)" not in markdown
