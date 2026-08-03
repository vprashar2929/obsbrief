from __future__ import annotations

from pathlib import Path

import pytest

from opsbrief.branding import load_brand_profile


def test_load_bundled_sample_brand_profile() -> None:
    profile_path = Path(__file__).parents[1] / "config" / "brand.example.yaml"

    profile = load_brand_profile(profile_path)

    assert profile.organization_name == "OpsBrief"
    assert profile.logo_path.endswith("sample-assets/opsbrief-mark.svg")
    assert profile.report_theme.theme_lines.enabled is True
    assert profile.report_theme.corner_motif.enabled is True


def test_load_brand_profile_from_yaml(tmp_path: Path) -> None:
    logo_path = tmp_path / "logo.png"
    logo_path.write_bytes(_tiny_png())
    font_path = tmp_path / "brand-font.ttf"
    font_path.write_bytes(b"font")
    profile_path = tmp_path / "brand.yaml"
    profile_path.write_text(
        "\n".join(
            [
                "organization_name: Example Org",
                "logo_path: logo.png",
                "logo_alt: Example logo",
                "logo_width_mm: 40",
                "font_family: Arial, sans-serif",
                "font_faces:",
                "  - family: Arial",
                "    path: brand-font.ttf",
                "    weight: 700",
                "    style: normal",
                "colors:",
                '  primary: "#005f73"',
                '  accent: "#ca6702"',
                "report_theme:",
                "  theme_lines:",
                "    enabled: true",
                "    count: 6",
                '    color: "#ca6702"',
                "  corner_motif:",
                "    enabled: true",
                '    fill: "#005f73"',
                '    border: "#ca6702"',
                "  status_colors:",
                '    ok: "#005f73"',
                '    warning: "#8A6B00"',
                '    critical: "#9F2A2A"',
                '    scoped: "#475569"',
                "  chart_palette:",
                '    - "#005f73"',
                '    - "#ca6702"',
            ]
        ),
        encoding="utf-8",
    )

    profile = load_brand_profile(profile_path)

    assert profile.organization_name == "Example Org"
    assert profile.report_title == "Example Org Weekly Operational Report"
    assert profile.logo_path == str(logo_path)
    assert profile.logo_alt == "Example logo"
    assert profile.logo_width_mm == 40.0
    assert profile.font_family == "Arial, sans-serif"
    assert len(profile.font_faces) == 1
    assert profile.font_faces[0].family == "Arial"
    assert profile.font_faces[0].path == str(font_path)
    assert profile.font_faces[0].weight == "700"
    assert profile.font_faces[0].style == "normal"
    assert profile.colors.primary == "#005f73"
    assert profile.colors.accent == "#ca6702"
    assert profile.colors.background == "#f3f5f9"
    assert profile.report_theme.theme_lines.enabled is True
    assert profile.report_theme.theme_lines.count == 6
    assert profile.report_theme.theme_lines.color == "#ca6702"
    assert profile.report_theme.corner_motif.enabled is True
    assert profile.report_theme.corner_motif.fill == "#005f73"
    assert profile.report_theme.corner_motif.border == "#ca6702"
    assert profile.report_theme.status_colors.warning == "#8A6B00"
    assert profile.report_theme.chart_palette == ("#005f73", "#ca6702")


def test_load_brand_profile_rejects_invalid_color(tmp_path: Path) -> None:
    profile_path = tmp_path / "brand.yaml"
    profile_path.write_text(
        "\n".join(
            [
                "organization_name: Example Org",
                "colors:",
                "  primary: blue",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="colors.primary"):
        load_brand_profile(profile_path)


def _tiny_png() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00"
        b"\x00\x00\x0cIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02"
        b"\xfeA\xe2!\xbc\x00\x00\x00\x00IEND\xaeB`\x82"
    )
