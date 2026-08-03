from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_HEX_COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")
_DEFAULT_FONT_FAMILY = "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif"


@dataclass(frozen=True, slots=True)
class BrandColors:
    primary: str = "#0f172a"
    secondary: str = "#0b1324"
    accent: str = "#1e3a5f"
    background: str = "#f3f5f9"
    surface: str = "#ffffff"
    text: str = "#0f172a"
    muted: str = "#475569"
    border: str = "#dbe1ea"
    header_background: str = "#e8edf6"
    row_alternate: str = "#f8fafc"
    code_background: str = "#f1f5f9"


@dataclass(frozen=True, slots=True)
class BrandFontFace:
    family: str
    path: str
    weight: str = "400"
    style: str = "normal"


@dataclass(frozen=True, slots=True)
class BrandThemeLines:
    enabled: bool = False
    count: int = 0
    color: str = ""


@dataclass(frozen=True, slots=True)
class BrandCornerMotif:
    enabled: bool = False
    fill: str = ""
    border: str = ""


@dataclass(frozen=True, slots=True)
class BrandStatusColors:
    ok: str = ""
    warning: str = "#8a6b00"
    critical: str = "#9f2a2a"
    scoped: str = ""


@dataclass(frozen=True, slots=True)
class BrandReportTheme:
    theme_lines: BrandThemeLines = BrandThemeLines()
    corner_motif: BrandCornerMotif = BrandCornerMotif()
    status_colors: BrandStatusColors = BrandStatusColors()
    chart_palette: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BrandProfile:
    organization_name: str = ""
    report_title: str = "OpsBrief Weekly Operational Report"
    logo_path: str = ""
    logo_alt: str = "Organization logo"
    logo_width_mm: float = 35.0
    font_family: str = _DEFAULT_FONT_FAMILY
    font_faces: tuple[BrandFontFace, ...] = ()
    colors: BrandColors = BrandColors()
    report_theme: BrandReportTheme = BrandReportTheme()


def default_brand_profile() -> BrandProfile:
    return BrandProfile()


def load_brand_profile(path: str | Path | None) -> BrandProfile:
    if not path:
        return default_brand_profile()

    profile_path = Path(path)
    with profile_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Expected dict YAML at {profile_path}, got {type(raw).__name__}")
    return brand_profile_from_dict(raw, base_dir=profile_path.parent)


def brand_profile_from_dict(raw: dict[str, Any], base_dir: Path | None = None) -> BrandProfile:
    organization_name = _optional_string(raw, "organization_name", "")
    report_title = _optional_string(raw, "report_title", "")
    if not report_title:
        report_title = (
            f"{organization_name} Weekly Operational Report"
            if organization_name
            else "OpsBrief Weekly Operational Report"
        )

    font_family = _optional_string(raw, "font_family", _DEFAULT_FONT_FAMILY)
    _validate_font_family(font_family)
    font_faces = _optional_font_faces(raw, base_dir)
    logo_path = _optional_logo_path(raw, base_dir)
    logo_width_mm = _optional_float(raw, "logo_width_mm", 35.0)
    if logo_width_mm <= 0:
        raise ValueError("Brand profile logo_width_mm must be greater than 0")

    colors_raw = raw.get("colors", {})
    if not isinstance(colors_raw, dict):
        raise ValueError("Expected dict value for brand profile field: colors")

    default_colors = BrandColors()
    colors = BrandColors(
        primary=_color_value(colors_raw, "primary", default_colors.primary),
        secondary=_color_value(colors_raw, "secondary", default_colors.secondary),
        accent=_color_value(colors_raw, "accent", default_colors.accent),
        background=_color_value(colors_raw, "background", default_colors.background),
        surface=_color_value(colors_raw, "surface", default_colors.surface),
        text=_color_value(colors_raw, "text", default_colors.text),
        muted=_color_value(colors_raw, "muted", default_colors.muted),
        border=_color_value(colors_raw, "border", default_colors.border),
        header_background=_color_value(
            colors_raw, "header_background", default_colors.header_background
        ),
        row_alternate=_color_value(colors_raw, "row_alternate", default_colors.row_alternate),
        code_background=_color_value(colors_raw, "code_background", default_colors.code_background),
    )

    return BrandProfile(
        organization_name=organization_name,
        report_title=report_title,
        logo_path=logo_path,
        logo_alt=_optional_string(raw, "logo_alt", "Organization logo"),
        logo_width_mm=logo_width_mm,
        font_family=font_family,
        font_faces=font_faces,
        colors=colors,
        report_theme=_optional_report_theme(raw),
    )


def _optional_string(raw: dict[str, Any], key: str, default: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"Expected string value for brand profile field: {key}")
    return value.strip() or default


def _optional_logo_path(raw: dict[str, Any], base_dir: Path | None) -> str:
    value = _optional_string(raw, "logo_path", "")
    if not value:
        return ""
    logo_path = Path(value)
    if not logo_path.is_absolute() and base_dir is not None:
        logo_path = base_dir / logo_path
    if not logo_path.exists():
        raise ValueError(f"Brand profile logo_path does not exist: {logo_path}")
    if not logo_path.is_file():
        raise ValueError(f"Brand profile logo_path is not a file: {logo_path}")
    return str(logo_path)


def _optional_float(raw: dict[str, Any], key: str, default: float) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Expected numeric value for brand profile field: {key}")
    return float(value)


def _optional_font_faces(raw: dict[str, Any], base_dir: Path | None) -> tuple[BrandFontFace, ...]:
    value = raw.get("font_faces", [])
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("Expected list value for brand profile field: font_faces")

    font_faces: list[BrandFontFace] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"Expected dict value for brand profile field: font_faces[{index}]")

        family = _required_string(item, "family", f"font_faces[{index}].family")
        _validate_font_family(family)

        raw_path = _required_string(item, "path", f"font_faces[{index}].path")
        font_path = Path(raw_path)
        if not font_path.is_absolute() and base_dir is not None:
            font_path = base_dir / font_path
        if not font_path.exists():
            raise ValueError(f"Brand profile font path does not exist: {font_path}")
        if not font_path.is_file():
            raise ValueError(f"Brand profile font path is not a file: {font_path}")

        weight = _optional_font_weight(item, index)
        style = _optional_font_style(item, index)
        font_faces.append(
            BrandFontFace(
                family=family,
                path=str(font_path),
                weight=weight,
                style=style,
            )
        )

    return tuple(font_faces)


def _optional_report_theme(raw: dict[str, Any]) -> BrandReportTheme:
    theme_raw = _optional_dict(raw, "report_theme", "report_theme")
    theme_lines_raw = _optional_dict(theme_raw, "theme_lines", "report_theme.theme_lines")
    corner_raw = _optional_dict(theme_raw, "corner_motif", "report_theme.corner_motif")
    status_raw = _optional_dict(theme_raw, "status_colors", "report_theme.status_colors")
    default_status_colors = BrandStatusColors()

    return BrandReportTheme(
        theme_lines=BrandThemeLines(
            enabled=_optional_bool(
                theme_lines_raw, "enabled", False, "report_theme.theme_lines.enabled"
            ),
            count=_optional_int(
                theme_lines_raw, "count", 0, "report_theme.theme_lines.count", 0, 12
            ),
            color=_optional_color(theme_lines_raw, "color", "", "report_theme.theme_lines.color"),
        ),
        corner_motif=BrandCornerMotif(
            enabled=_optional_bool(
                corner_raw, "enabled", False, "report_theme.corner_motif.enabled"
            ),
            fill=_optional_color(corner_raw, "fill", "", "report_theme.corner_motif.fill"),
            border=_optional_color(corner_raw, "border", "", "report_theme.corner_motif.border"),
        ),
        status_colors=BrandStatusColors(
            ok=_optional_color(
                status_raw, "ok", default_status_colors.ok, "report_theme.status_colors.ok"
            ),
            warning=_optional_color(
                status_raw,
                "warning",
                default_status_colors.warning,
                "report_theme.status_colors.warning",
            ),
            critical=_optional_color(
                status_raw,
                "critical",
                default_status_colors.critical,
                "report_theme.status_colors.critical",
            ),
            scoped=_optional_color(
                status_raw,
                "scoped",
                default_status_colors.scoped,
                "report_theme.status_colors.scoped",
            ),
        ),
        chart_palette=_optional_color_list(
            theme_raw, "chart_palette", "report_theme.chart_palette"
        ),
    )


def _required_string(raw: dict[str, Any], key: str, field_name: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Expected non-empty string value for brand profile field: {field_name}")
    return value.strip()


def _optional_font_weight(raw: dict[str, Any], index: int) -> str:
    value = raw.get("weight", "400")
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        raise ValueError(
            f"Expected string or integer value for brand profile field: font_faces[{index}].weight"
        )
    normalized = value.strip().lower()
    if normalized in {"normal", "bold"}:
        return normalized
    if normalized in {str(weight) for weight in range(100, 1000, 100)}:
        return normalized
    raise ValueError(f"Unsupported brand profile font weight: font_faces[{index}].weight")


def _optional_font_style(raw: dict[str, Any], index: int) -> str:
    value = raw.get("style", "normal")
    if not isinstance(value, str):
        raise ValueError(
            f"Expected string value for brand profile field: font_faces[{index}].style"
        )
    normalized = value.strip().lower()
    if normalized not in {"normal", "italic", "oblique"}:
        raise ValueError(f"Unsupported brand profile font style: font_faces[{index}].style")
    return normalized


def _color_value(raw: dict[str, Any], key: str, default: str) -> str:
    value = raw.get(key, default)
    return _validated_color(value, f"colors.{key}")


def _optional_color(raw: dict[str, Any], key: str, default: str, field_name: str) -> str:
    value = raw.get(key, default)
    if value in (None, ""):
        return default
    return _validated_color(value, field_name)


def _optional_color_list(raw: dict[str, Any], key: str, field_name: str) -> tuple[str, ...]:
    value = raw.get(key, ())
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"Expected list value for brand profile field: {field_name}")

    colors: list[str] = []
    for index, item in enumerate(value):
        colors.append(_validated_color(item, f"{field_name}[{index}]"))
    return tuple(colors)


def _validated_color(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Expected string color value for brand profile field: {field_name}")
    normalized = value.strip()
    if not _HEX_COLOR_PATTERN.fullmatch(normalized):
        raise ValueError(f"Expected #RRGGBB color value for brand profile field: {field_name}")
    return normalized


def _optional_dict(raw: dict[str, Any], key: str, field_name: str) -> dict[str, Any]:
    value = raw.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"Expected dict value for brand profile field: {field_name}")
    return value


def _optional_bool(raw: dict[str, Any], key: str, default: bool, field_name: str) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"Expected boolean value for brand profile field: {field_name}")
    return value


def _optional_int(
    raw: dict[str, Any],
    key: str,
    default: int,
    field_name: str,
    min_value: int,
    max_value: int,
) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Expected integer value for brand profile field: {field_name}")
    if value < min_value or value > max_value:
        raise ValueError(
            f"Expected integer value between {min_value} and {max_value} "
            f"for brand profile field: {field_name}"
        )
    return value


def _validate_font_family(font_family: str) -> None:
    blocked = {";", "{", "}", "<", ">"}
    if any(char in font_family for char in blocked) or "\n" in font_family:
        raise ValueError("Brand profile font_family contains unsupported characters")
