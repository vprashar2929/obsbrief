from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import matplotlib
import matplotlib.dates as mdates
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter

if TYPE_CHECKING:
    from opsbrief.branding import BrandProfile

matplotlib.use("Agg")  # non-interactive backend for CLI/report generation

_DEFAULT_PALETTE: tuple[str, ...] = (
    "#2563eb",
    "#16a34a",
    "#dc2626",
    "#d97706",
    "#7c3aed",
    "#0891b2",
    "#475569",
    "#db2777",
)


@dataclass(frozen=True, slots=True)
class ChartSeries:
    """A single named time series.

    samples must be a tuple of (unix_timestamp_seconds: float, value: float).
    The public contract is intentionally unchanged for zero impact on callers.
    """

    label: str
    samples: tuple[tuple[float, float], ...]


def write_time_series_png(
    path: Path,
    series: Sequence[ChartSeries],
    *,
    title: str = "",
    y_label: str = "",
    width: int = 1180,
    height: int = 460,
    brand: BrandProfile | None = None,
) -> None:
    """Write a time-series line chart PNG.

    Exact public contract is preserved for backward compatibility.
    The optional `brand` parameter allows the chart chrome (title, axes,
    grid, background) to respect the report's BrandProfile colors.

    width/height are target pixel dimensions. y_label controls formatting.
    """
    graphable = [
        ChartSeries(label=item.label, samples=_sorted_samples(item.samples))
        for item in series
        if item.samples
    ]
    if not graphable:
        return

    # =====================================================================
    # Design Constants - First-Class Charting Standards
    # These values control visual density, spacing, and professionalism.
    # They are centralized here for maintainability and documentation.
    # =====================================================================

    DPI = 150  # Target resolution for high-quality PDF embedding

    # Target figure size bounds (in inches at DPI above)
    MIN_FIG_WIDTH_IN = 5.5
    MIN_FIG_HEIGHT_IN = 2.9

    # Margins in inches (generous for operational reports with legends)
    MARGIN_LEFT_IN = 0.68  # y-tick labels + ylabel
    MARGIN_RIGHT_IN = 0.16
    MARGIN_TOP_IN = 0.46
    MARGIN_BOTTOM_IN = 0.78  # sufficient for up to 3 legend rows

    # Density handling
    DENSE_SERIES_THRESHOLD = 80  # points above this → no markers

    # Typography (points)
    TITLE_FONTSIZE = 11.5
    YLABEL_FONTSIZE = 9.0
    LEGEND_FONTSIZE = 7.0
    TICK_LABELSIZE = 8

    # Line and marker styling
    LINEWIDTH = 1.6
    MARKERSIZE = 2.1
    SPINE_LINEWIDTH = 0.7

    # Spacing
    TITLE_PAD = 4
    SAVEFIG_PAD_INCHES = 0.04
    LEGEND_Y_OFFSET = -0.12

    # Grid appearance (balanced readability vs data-ink ratio).
    # Values chosen for good contrast on both light and brand-specific backgrounds.
    # Higher values improve accessibility for readers with lower contrast sensitivity.
    GRID_ALPHA_DEFAULT = 0.32
    GRID_ALPHA_BRANDED = 0.38

    # Legend column logic
    LEGEND_NCOL_THRESHOLD = 4

    # =====================================================================
    # End of Design Constants
    # =====================================================================

    dpi = DPI

    fig_width_in = max(MIN_FIG_WIDTH_IN, width / dpi)
    fig_height_in = max(MIN_FIG_HEIGHT_IN, height / dpi)

    bg = brand.colors.surface if brand is not None else "#ffffff"
    fig = Figure(figsize=(fig_width_in, fig_height_in), dpi=dpi, facecolor=bg)
    ax = fig.add_subplot(111)

    # Explicit margins give far more stable final pixel sizes than pure tight_layout.
    fig.subplots_adjust(
        left=MARGIN_LEFT_IN / fig_width_in,
        right=1.0 - MARGIN_RIGHT_IN / fig_width_in,
        top=1.0 - MARGIN_TOP_IN / fig_height_in,
        bottom=MARGIN_BOTTOM_IN / fig_height_in,
    )

    all_datetimes: list[datetime] = []
    palette = _series_palette(brand)
    for idx, item in enumerate(graphable):
        color = palette[idx % len(palette)]
        xs = [datetime.fromtimestamp(s[0], tz=UTC) for s in item.samples]
        ys = [s[1] for s in item.samples]
        all_datetimes.extend(xs)

        # Density-aware styling: markers on every point look noisy on real
        # telemetry (often 200-350 points). We only draw markers for sparse series.
        n_points = len(item.samples)
        use_markers = n_points <= DENSE_SERIES_THRESHOLD
        ax.plot(
            xs,  # type: ignore[arg-type]
            ys,
            label=item.label,
            linewidth=LINEWIDTH,
            color=color,
            marker="o" if use_markers else None,
            markersize=MARKERSIZE if use_markers else 0,
            zorder=3,
        )

    if not all_datetimes:
        return

    # Brand-aware chrome colors (fall back to calm professional defaults)
    title_color = brand.colors.primary if brand is not None else "#0f172a"
    muted_color = brand.colors.muted if brand is not None else "#475569"
    border_color = brand.colors.border if brand is not None else "#64748b"
    grid_color = brand.colors.border if brand is not None else "#e2e8f0"

    ax.set_title(title or "Time Series", fontsize=TITLE_FONTSIZE, pad=TITLE_PAD, color=title_color)
    if y_label:
        ax.set_ylabel(y_label, fontsize=YLABEL_FONTSIZE, color=muted_color)

    grid_alpha = GRID_ALPHA_DEFAULT if brand is None else GRID_ALPHA_BRANDED
    ax.grid(True, linestyle="-", alpha=grid_alpha, color=grid_color, zorder=0)
    ax.tick_params(colors=muted_color, labelsize=TICK_LABELSIZE)

    for spine in ax.spines.values():
        spine.set_color(border_color)
        spine.set_linewidth(SPINE_LINEWIDTH)

    # X locator/formatter tuned for the 1-30 day spans typical in weekly reports
    span_seconds = (max(all_datetimes) - min(all_datetimes)).total_seconds()
    locator, formatter = _pick_time_locator_and_formatter(span_seconds)
    ax.xaxis.set_major_locator(locator)  # type: ignore[arg-type]
    ax.xaxis.set_major_formatter(formatter)  # type: ignore[arg-type]

    # Y tick formatting replicates the previous compact/percent/count behavior
    ax.yaxis.set_major_formatter(_make_y_formatter(y_label))

    # Legend placed below the axes. We reserved space via MARGIN_BOTTOM_IN above.
    ncol = 2 if len(graphable) <= LEGEND_NCOL_THRESHOLD else 3
    ax.legend(
        loc="upper center",
        bbox_to_anchor=(0.5, LEGEND_Y_OFFSET),
        ncol=ncol,
        fontsize=LEGEND_FONTSIZE,
        frameon=False,
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(
        str(path),
        format="png",
        facecolor=bg,
        edgecolor="none",
        pad_inches=SAVEFIG_PAD_INCHES,
    )
    # Release memory for long-running CLI usage
    fig.clf()


def _sorted_samples(samples: tuple[tuple[float, float], ...]) -> tuple[tuple[float, float], ...]:
    return tuple(sorted(samples, key=lambda item: item[0]))


def _series_palette(brand: BrandProfile | None) -> tuple[str, ...]:
    if brand is None:
        return _DEFAULT_PALETTE

    configured_palette = _unique_hex_colors(brand.report_theme.chart_palette)
    if configured_palette:
        return configured_palette

    base = _unique_hex_colors(
        (
            brand.colors.primary,
            brand.colors.accent,
            brand.colors.secondary,
        )
    )
    if len(base) < 2:
        return _DEFAULT_PALETTE

    variants: list[str] = list(base)
    for color in base:
        variants.append(_mix_hex_color(color, "#000000", 0.18))
    for color in base:
        variants.append(_mix_hex_color(color, "#ffffff", 0.24))
    return tuple(_unique_hex_colors(tuple(variants))) or _DEFAULT_PALETTE


def _unique_hex_colors(colors: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for color in colors:
        normalized = color.strip()
        if not _is_hex_color(normalized):
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return tuple(result)


def _is_hex_color(value: str) -> bool:
    if len(value) != 7 or not value.startswith("#"):
        return False
    return all(char in "0123456789abcdefABCDEF" for char in value[1:])


def _mix_hex_color(color: str, target: str, amount: float) -> str:
    source_rgb = _hex_to_rgb(color)
    target_rgb = _hex_to_rgb(target)
    mixed = tuple(
        round(source + (target_item - source) * amount)
        for source, target_item in zip(source_rgb, target_rgb, strict=True)
    )
    return "#" + "".join(f"{channel:02x}" for channel in mixed)


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    normalized = color.strip().lstrip("#")
    return (
        int(normalized[0:2], 16),
        int(normalized[2:4], 16),
        int(normalized[4:6], 16),
    )


def _pick_time_locator_and_formatter(
    span_seconds: float,
) -> tuple[object, object]:
    """Return a (Locator, DateFormatter) pair.

    The concrete types come from matplotlib.dates and have incomplete stubs;
    we use object here to keep the public API fully typed while satisfying
    strict mypy. The implementation is covered by execution tests.
    """
    days = span_seconds / 86400.0
    if days >= 6:  # typical weekly report window
        interval = max(1, int(days / 6))
        return mdates.DayLocator(interval=interval), mdates.DateFormatter("%m-%d")  # type: ignore[no-untyped-call]
    if days >= 2:
        return mdates.DayLocator(interval=1), mdates.DateFormatter("%m-%d")  # type: ignore[no-untyped-call]
    if days >= 0.5:
        return mdates.HourLocator(interval=6), mdates.DateFormatter("%m-%d %H:%M")  # type: ignore[no-untyped-call]
    # sub-day windows
    return mdates.HourLocator(interval=2), mdates.DateFormatter("%H:%M")  # type: ignore[no-untyped-call]


def _make_y_formatter(unit: str) -> FuncFormatter:
    normalized = unit.strip().lower()

    def _fmt(value: float, pos: int) -> str:  # noqa: ARG001 - matplotlib signature
        if normalized == "percent":
            return f"{value:.1f}%"
        if normalized == "count":
            return _compact_number(value)
        return _compact_number(value)

    return FuncFormatter(_fmt)


def _compact_number(value: float) -> str:
    """Replicates the compact number formatting from the previous renderer."""
    abs_value = abs(value)
    if abs_value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if abs_value >= 1_000:
        return f"{value / 1_000:.1f}K"
    if abs(value - round(value)) < 0.05:
        return str(int(round(value)))
    return f"{value:.1f}"
