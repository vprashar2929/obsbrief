from __future__ import annotations

import mimetypes
import re
from base64 import b64encode
from html import escape
from pathlib import Path

from jinja2 import Environment, PackageLoader
from markdown import markdown

from opsbrief.branding import BrandProfile
from opsbrief.models import Report

PRINT_SECTION_BREAK_IDS = (
    "technical-evidence-appendix",
    "monitoring-and-alerting",
    "change-audit",
    "backup-and-recovery-posture",
    "network-and-dns-posture",
    "kubernetes-capacity-evidence",
    "infrastructure-capacity-and-managed-services",
    "logging-and-delivery-pipeline",
    "collector-warnings-and-gaps",
    "evidence-index",
)

_TEMPLATE_ENV = Environment(
    loader=PackageLoader("opsbrief", "templates"),
    autoescape=True,
)


def render_report_html(report: Report, markdown_text: str, brand: BrandProfile) -> str:
    headings = _markdown_headings(markdown_text)
    sections = [(heading, slug) for level, heading, slug in headings if level == 2]
    html_body = markdown(markdown_text, extensions=["tables", "fenced_code", "sane_lists"])
    html_body = _wrap_html_tables(html_body)
    html_body = _add_heading_ids(html_body, headings)
    html_body = _decorate_status_labels(html_body)
    title = f"{brand.report_title} ({report.environment})"
    return _render_html_document(
        title=title,
        style_css="\n".join(_html_document_style_lines(brand)),
        section_nav=_html_section_nav(sections),
        brand_banner=_html_brand_banner(brand),
        report_hero=_html_report_hero(report),
        html_body=html_body,
    )


def _render_html_document(
    *,
    title: str,
    style_css: str,
    section_nav: str,
    brand_banner: str,
    report_hero: str,
    html_body: str,
) -> str:
    template = _TEMPLATE_ENV.get_template("report.html.j2")
    return template.render(
        title=title,
        style_css=style_css,
        section_nav=section_nav,
        brand_banner=brand_banner,
        report_hero=report_hero,
        html_body=html_body,
    )


def _html_document_style_lines(brand: BrandProfile) -> list[str]:
    return [
        *_html_brand_style_lines(brand),
        "    * { box-sizing: border-box; }",
        "    body {"
        "      margin: 0;"
        "      background: var(--bg);"
        "      color: var(--text);"
        "      font-family: var(--font-family);"
        "      line-height: 1.6;"
        "    }",
        "    .report-shell { max-width: 1480px; margin: 0 auto; padding: 22px 20px 34px; }",
        "    .report-layout {"
        "      display: grid;"
        "      grid-template-columns: minmax(190px, 250px) minmax(0, 1fr);"
        "      gap: 18px;"
        "      align-items: start;"
        "    }",
        "    .report-toc {"
        "      background: var(--surface);"
        "      border: 1px solid var(--border);"
        "      border-radius: 8px;"
        "      padding: 14px;"
        "      position: sticky;"
        "      top: 18px;"
        "      max-height: calc(100vh - 36px);"
        "      overflow: auto;"
        "    }",
        "    .toc-title {"
        "      margin: 0 0 10px;"
        "      color: var(--muted);"
        "      font-size: 0.72rem;"
        "      font-weight: 800;"
        "      letter-spacing: 0.08em;"
        "      text-transform: uppercase;"
        "    }",
        "    .toc-list { list-style: none; margin: 0; padding: 0; }",
        "    .toc-list li { margin: 0; }",
        "    .toc-list a {"
        "      display: block;"
        "      color: var(--secondary);"
        "      text-decoration: none;"
        "      font-size: 0.86rem;"
        "      line-height: 1.25;"
        "      padding: 7px 8px;"
        "      border-left: 3px solid transparent;"
        "      border-radius: 4px;"
        "    }",
        "    .toc-list a:hover {"
        "      background: var(--code-bg);"
        "      border-left-color: var(--accent);"
        "    }",
        "    .report-content {"
        "      background: var(--surface);"
        "      border: 1px solid var(--border);"
        "      border-radius: 6px;"
        "      padding: 0 30px 30px;"
        "      position: relative;"
        "      overflow: hidden;"
        "    }",
        "    .brand-banner {"
        "      display: flex;"
        "      align-items: flex-start;"
        "      justify-content: space-between;"
        "      gap: 24px;"
        "      margin: 0 -30px 22px;"
        "      min-height: 146px;"
        "      padding: 56px 30px 22px;"
        "      position: relative;"
        "      background: var(--surface);"
        "      border-bottom: 1px solid var(--border);"
        "      overflow: hidden;"
        "    }",
        "    .brand-banner::before {"
        "      content: '';"
        "      position: absolute;"
        "      inset: 0 148px auto 0;"
        "      height: 82px;"
        "      opacity: 0.76;"
        "      background: transparent;"
        "      border-bottom-right-radius: 58% 92%;"
        "      pointer-events: none;"
        "    }",
        "    .brand-banner::after {"
        "      content: '';"
        "      display: none;"
        "      position: absolute;"
        "      right: -44px;"
        "      top: -66px;"
        "      width: 132px;"
        "      height: 132px;"
        "      border-radius: 50%;"
        "      background: var(--corner-motif-fill);"
        "      border: 2px solid var(--corner-motif-border);"
        "      pointer-events: none;"
        "    }",
        "    .brand-banner.brand-corner-motif::after { display: block; }",
        "    .brand-banner > * { position: relative; z-index: 1; }",
        "    .brand-banner-copy { flex: 1 1 auto; min-width: 0; }",
        "    .brand-theme-lines {"
        "      display: block;"
        "      width: min(720px, 72%);"
        "      height: 42px;"
        "      margin: -40px 0 14px;"
        "      overflow: hidden;"
        "      pointer-events: none;"
        "    }",
        "    .brand-theme-lines span {"
        "      display: block;"
        "      width: 100%;"
        "      height: 1px;"
        "      margin-top: 8px;"
        "      background: var(--theme-line-color);"
        "      opacity: 0.68;"
        "    }",
        "    .brand-kicker {"
        "      margin: 0 0 4px;"
        "      color: var(--muted);"
        "      font-size: 0.82rem;"
        "      font-weight: 700;"
        "    }",
        "    .brand-banner-title {"
        "      margin: 0;"
        "      color: var(--primary);"
        "      font-size: 1.34rem;"
        "      font-weight: 800;"
        "    }",
        "    .brand-logo, .brand-wordmark {      margin-left: auto;      flex: 0 0 auto;    }",
        "    .brand-logo {"
        "      width: clamp(120px, 16vw, 190px);"
        "      height: auto;"
        "      object-fit: contain;"
        "      padding: 0;"
        "      background: transparent;"
        "      border-radius: 0;"
        "    }",
        "    .brand-wordmark {"
        "      max-width: 220px;"
        "      color: var(--primary);"
        "      font-weight: 800;"
        "      line-height: 1.15;"
        "      text-align: right;"
        "      padding: 0;"
        "    }",
        "    .report-hero {"
        "      margin: 0 0 24px;"
        "      padding: 18px;"
        "      background: var(--code-bg);"
        "      border: 1px solid var(--border);"
        "      border-left: 3px solid var(--accent);"
        "      border-radius: 6px;"
        "    }",
        "    .hero-meta {"
        "      display: grid;"
        "      grid-template-columns: repeat(4, minmax(0, 1fr));"
        "      gap: 10px;"
        "    }",
        "    .meta-tile {"
        "      min-width: 0;"
        "      padding: 10px 12px;"
        "      background: var(--surface);"
        "      border: 1px solid var(--border);"
        "      border-radius: 6px;"
        "    }",
        "    .meta-label {"
        "      display: block;"
        "      color: var(--muted);"
        "      font-size: 0.72rem;"
        "      font-weight: 700;"
        "      text-transform: uppercase;"
        "      letter-spacing: 0.04em;"
        "    }",
        "    .meta-value {"
        "      display: block;"
        "      margin-top: 2px;"
        "      color: var(--secondary);"
        "      font-size: 0.98rem;"
        "      font-weight: 800;"
        "      overflow-wrap: anywhere;"
        "    }",
        "    h1, h2, h3 { line-height: 1.25; }",
        "    h1 {"
        "      margin-top: 0; margin-bottom: 0.6em; font-size: 2rem;"
        "      color: var(--primary);"
        "    }",
        "    h2 {"
        "      margin: 1.65em 0 0.65em;"
        "      padding: 0 0 7px;"
        "      font-size: 1.24rem;"
        "      color: var(--primary);"
        "      background: transparent;"
        "      border-bottom: 2px solid var(--accent);"
        "      border-radius: 0;"
        "      scroll-margin-top: 20px;"
        "    }",
        "    h3 {"
        "      margin-top: 1.1em; margin-bottom: 0.45em; font-size: 1.05rem;"
        "      color: var(--accent);"
        "    }",
        "    p, li { color: var(--text); }",
        "    ul { margin: 0.4em 0 0.9em 1.2em; }",
        "    .table-wrap {"
        "      max-width: 100%;"
        "      overflow-x: auto;"
        "      margin: 12px 0 16px;"
        "    }",
        "    table {"
        "      border-collapse: separate;"
        "      border-spacing: 0;"
        "      width: max-content;"
        "      min-width: 100%;"
        "      max-width: none;"
        "      table-layout: auto;"
        "      border: 1px solid var(--border);"
        "      border-radius: 8px;"
        "      overflow: hidden;"
        "      font-size: 0.88rem;"
        "      background: #fff;"
        "    }",
        "    th, td {"
        "      padding: 9px 10px;"
        "      text-align: left;"
        "      vertical-align: top;"
        "      border-bottom: 1px solid var(--border);"
        "      border-right: 1px solid var(--border);"
        "      white-space: nowrap;"
        "      overflow-wrap: normal;"
        "      word-break: normal;"
        "    }",
        "    th:last-child, td:last-child { border-right: 0; }",
        "    tr:last-child td { border-bottom: 0; }",
        "    th {"
        "      background: var(--head);"
        "      font-weight: 750;"
        "      color: var(--secondary);"
        "      position: sticky;"
        "      top: 0;"
        "      z-index: 1;"
        "    }",
        "    tbody tr:hover td { background: var(--code-bg); }",
        "    tbody tr:nth-child(even) td { background: var(--row-alt); }",
        "    .status-tag {"
        "      display: inline-block;"
        "      padding: 0.16em 0.52em;"
        "      color: #fff;"
        "      background: var(--status-color, var(--muted));"
        "      border-radius: 2px;"
        "      font-size: 0.82em;"
        "      font-weight: 760;"
        "      line-height: 1.25;"
        "      white-space: nowrap;"
        "      vertical-align: baseline;"
        "    }",
        "    .status-ok { --status-color: var(--status-ok); }",
        "    .status-warning { --status-color: var(--status-warning); }",
        "    .status-critical { --status-color: var(--status-critical); }",
        "    .status-scoped { --status-color: var(--status-scoped); }",
        "    .report-content img:not(.brand-logo) {"
        "      max-width: 100%;"
        "      height: auto;"
        "      border: 1px solid var(--border);"
        "      border-radius: 6px;"
        "      background: #fff;"
        "    }",
        "    code { background: var(--code-bg); padding: 1px 4px; border-radius: 4px; }",
        "    pre {"
        "      background: var(--code-bg);"
        "      border: 1px solid var(--border);"
        "      border-radius: 8px;"
        "      padding: 12px;"
        "      overflow-x: auto;"
        "    }",
        "    @media (max-width: 900px) {"
        "      .report-shell { padding: 10px; }"
        "      .report-layout { display: block; }"
        "      .report-toc { position: static; max-height: none; margin-bottom: 12px; }"
        "      .toc-list { display: flex; gap: 6px; overflow-x: auto; padding-bottom: 2px; }"
        "      .toc-list a {"
        "        white-space: nowrap; border-left: 0;"
        "        border-bottom: 3px solid transparent;"
        "      }"
        "      .toc-list a:hover {"
        "        border-left-color: transparent;"
        "        border-bottom-color: var(--accent);"
        "      }"
        "      .report-content { padding: 0 16px 16px; border-radius: 8px; }"
        "      .brand-banner { margin: 0 -16px 18px; padding: 16px; }"
        "      .brand-banner { flex-direction: column-reverse; min-height: 0; }"
        "      .brand-logo, .brand-wordmark { margin-left: 0; }"
        "      .hero-meta { grid-template-columns: repeat(2, minmax(0, 1fr)); }"
        "      h2 { margin-left: 0; margin-right: 0; }"
        "      table { min-width: 640px; }"
        "    }",
        "    @media (max-width: 560px) { .hero-meta { grid-template-columns: 1fr; } }",
        *_html_paged_media_style_lines(brand),
    ]


def _html_brand_style_lines(brand: BrandProfile) -> list[str]:
    theme = brand.report_theme
    status_colors = theme.status_colors
    corner_motif = theme.corner_motif
    theme_lines = theme.theme_lines
    return [
        *_html_font_face_style_lines(brand),
        "    :root {",
        f"      --bg: {brand.colors.background};",
        f"      --surface: {brand.colors.surface};",
        f"      --text: {brand.colors.text};",
        f"      --muted: {brand.colors.muted};",
        f"      --border: {brand.colors.border};",
        f"      --head: {brand.colors.header_background};",
        f"      --row-alt: {brand.colors.row_alternate};",
        f"      --code-bg: {brand.colors.code_background};",
        f"      --primary: {brand.colors.primary};",
        f"      --secondary: {brand.colors.secondary};",
        f"      --accent: {brand.colors.accent};",
        f"      --theme-line-color: {theme_lines.color or brand.colors.accent};",
        f"      --corner-motif-fill: {corner_motif.fill or brand.colors.primary};",
        f"      --corner-motif-border: {corner_motif.border or brand.colors.accent};",
        f"      --status-ok: {status_colors.ok or brand.colors.primary};",
        f"      --status-warning: {status_colors.warning};",
        f"      --status-critical: {status_colors.critical};",
        f"      --status-scoped: {status_colors.scoped or brand.colors.muted};",
        f"      --font-family: {brand.font_family};",
        "    }",
    ]


def _html_font_face_style_lines(brand: BrandProfile) -> list[str]:
    lines: list[str] = []
    for font_face in brand.font_faces:
        source = f"url('{_file_data_uri(font_face.path)}')"
        format_hint = _font_format_hint(font_face.path)
        if format_hint:
            source = f"{source} format('{format_hint}')"
        lines.extend(
            [
                "    @font-face {",
                f'      font-family: "{_css_string(font_face.family)}";',
                f"      src: {source};",
                f"      font-weight: {font_face.weight};",
                f"      font-style: {font_face.style};",
                "      font-display: swap;",
                "    }",
            ]
        )
    return lines


def _html_paged_media_style_lines(brand: BrandProfile) -> list[str]:
    footer_text = _css_string(brand.organization_name or brand.report_title)
    logo_width_mm = min(45.0, max(20.0, float(brand.logo_width_mm)))
    section_break_selector = ", ".join(f"h2#{section_id}" for section_id in PRINT_SECTION_BREAK_IDS)
    return [
        "    @page {",
        "      size: A4 landscape;",
        "      margin: 12mm 10mm 13mm;",
        "      @bottom-left {",
        f'        content: "{footer_text}";',
        f"        color: {brand.colors.muted};",
        "        font-size: 7pt;",
        "      }",
        "      @bottom-right {",
        '        content: "Page " counter(page) " of " counter(pages);',
        f"        color: {brand.colors.muted};",
        "        font-size: 7pt;",
        "      }",
        "    }",
        "    @media print {",
        "      * { print-color-adjust: exact; }",
        "      body { background: #fff; font-size: 8.4pt; line-height: 1.38; }",
        "      a { color: var(--secondary); text-decoration: none; }",
        "      .report-shell { max-width: none; margin: 0; padding: 0; }",
        "      .report-layout { display: block; }",
        "      .report-toc { display: none; }",
        "      .report-content {",
        "        background: #fff;",
        "        border: 0;",
        "        border-radius: 0;",
        "        box-shadow: none;",
        "        padding: 0;",
        "        overflow: visible;",
        "      }",
        "      .brand-banner {",
        "        margin: 0 0 6mm;",
        "        min-height: 34mm;",
        "        padding: 13mm 7mm 5mm;",
        "        border-radius: 0;",
        "        background: #fff;",
        "        break-inside: avoid;",
        "        page-break-inside: avoid;",
        "      }",
        "      .brand-banner::before {",
        "        inset: 0 44mm auto 0;",
        "        height: 20mm;",
        "      }",
        "      .brand-theme-lines {",
        "        width: 68%;",
        "        height: 10mm;",
        "        margin: -10mm 0 3mm;",
        "      }",
        "      .brand-theme-lines span { margin-top: 2mm; }",
        "      .brand-banner::after {",
        "        right: -11mm;",
        "        top: -17mm;",
        "        width: 34mm;",
        "        height: 34mm;",
        "      }",
        f"      .brand-logo {{ width: {logo_width_mm:.1f}mm; max-height: 17mm; }}",
        "      .brand-wordmark { max-width: 65mm; }",
        "      .report-hero {",
        "        margin: 0 0 6mm;",
        "        padding: 4mm;",
        "        border-radius: 2mm;",
        "        break-inside: avoid;",
        "        page-break-inside: avoid;",
        "      }",
        "      .hero-meta { grid-template-columns: repeat(3, minmax(0, 1fr)); }",
        "      .meta-tile { padding: 3mm; break-inside: avoid; page-break-inside: avoid; }",
        "      .meta-label { font-size: 6.5pt; letter-spacing: 0.03em; }",
        "      .meta-value { font-size: 8.4pt; }",
        "      h1 { font-size: 18pt; margin: 0 0 4mm; }",
        "      h2 {",
        "        margin: 7mm 0 3mm;",
        "        padding: 3mm;",
        "        border-radius: 2mm;",
        "        font-size: 12pt;",
        "        break-after: avoid;",
        "        page-break-after: avoid;",
        "      }",
        "      h3 {",
        "        margin: 5mm 0 2mm;",
        "        font-size: 9.6pt;",
        "        break-after: avoid;",
        "        page-break-after: avoid;",
        "      }",
        f"      {section_break_selector} {{",
        "        break-before: page;",
        "        page-break-before: always;",
        "      }",
        "      h2#runtime-and-control-plane-health {",
        "        break-before: auto;",
        "        page-break-before: auto;",
        "      }",
        "      p, li { orphans: 2; widows: 2; }",
        "      ul { margin: 0.25em 0 0.65em 1.05em; }",
        "      .table-wrap { overflow: visible; margin: 3mm 0 4mm; }",
        "      table.report-table-wide { font-size: 6.35pt; }",
        "      table.report-table-extra-wide { font-size: 5.95pt; }",
        "      table.report-table-ultra-wide { font-size: 5.6pt; }",
        "      table {",
        "        width: 100%;",
        "        min-width: 0;",
        "        max-width: 100%;",
        "        table-layout: auto;",
        "        border-collapse: collapse;",
        "        border-radius: 0;",
        "        font-size: 6.8pt;",
        "        break-inside: auto;",
        "      }",
        "      thead { display: table-header-group; }",
        "      tr { break-inside: avoid; page-break-inside: avoid; }",
        "      th, td {",
        "        padding: 3pt 3.4pt;",
        "        white-space: normal;",
        "        overflow-wrap: anywhere;",
        "        word-break: normal;",
        "        hyphens: none;",
        "        line-height: 1.24;",
        "      }",
        "      th {",
        "        position: static;",
        "        overflow-wrap: normal;",
        "        word-break: normal;",
        "        hyphens: none;",
        "        line-height: 1.22;",
        "      }",
        "      tbody tr:hover td { background: transparent; }",
        "      .status-tag {",
        "        padding: 1.1pt 3.6pt;",
        "        border-radius: 1pt;",
        "        font-size: 0.78em;",
        "        line-height: 1.15;",
        "      }",
        "      .report-content img:not(.brand-logo) {",
        "        max-width: 100%;",
        "        max-height: 120mm;",
        "        break-inside: avoid;",
        "        page-break-inside: avoid;",
        "      }",
        "      pre { white-space: pre-wrap; }",
        "    }",
    ]


def _css_string(value: str) -> str:
    normalized = " ".join(value.split())
    return normalized.replace("\\", "\\\\").replace('"', '\\"')


def _html_brand_banner(brand: BrandProfile) -> str:
    organization = brand.organization_name or brand.report_title
    banner_class = (
        "brand-banner brand-corner-motif"
        if brand.report_theme.corner_motif.enabled
        else "brand-banner"
    )
    return "\n".join(
        [
            f'      <header class="{banner_class}">',
            '        <div class="brand-banner-copy">',
            *_html_theme_lines(brand),
            f'          <p class="brand-kicker">{escape(organization)}</p>',
            f'          <p class="brand-banner-title">{escape(brand.report_title)}</p>',
            "        </div>",
            f"        {_html_logo_or_wordmark(brand)}",
            "      </header>",
        ]
    )


def _html_theme_lines(brand: BrandProfile) -> list[str]:
    theme_lines = brand.report_theme.theme_lines
    if not theme_lines.enabled or theme_lines.count <= 0:
        return []
    return [
        '          <span class="brand-theme-lines" aria-hidden="true">',
        *["          <span>&nbsp;</span>" for _ in range(theme_lines.count)],
        "          </span>",
    ]


def _html_report_hero(report: Report) -> str:
    counts = _status_counts(report)
    status = report.overall_status.value
    status_label = _status_label(status)
    return "\n".join(
        [
            '        <section class="report-hero" aria-label="Report summary">',
            '          <div class="hero-meta">',
            _html_meta_tile("Assessment", _status_tag_marker(status_label, status)),
            _html_meta_tile("Environment", escape(report.environment)),
            _html_meta_tile("Collectors", escape(_collector_count_summary(counts))),
            "          </div>",
            "        </section>",
        ]
    )


def _html_meta_tile(label: str, value_html: str) -> str:
    return "\n".join(
        [
            '            <div class="meta-tile">',
            f'              <span class="meta-label">{escape(label)}</span>',
            f'              <span class="meta-value">{value_html}</span>',
            "            </div>",
        ]
    )


def _status_counts(report: Report) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in report.collectors:
        label = _status_label(item.status.value)
        counts[label] = counts.get(label, 0) + 1
    return counts


def _collector_count_summary(counts: dict[str, int]) -> str:
    if not counts:
        return "0"
    order = ["No Findings", "Needs Review", "Action Required", "Not Assessed"]
    parts = [f"{label} {counts[label]}" for label in order if counts.get(label, 0) > 0]
    return ", ".join(parts) if parts else "0"


def _status_label(status: str) -> str:
    return {
        "ok": "No Findings",
        "warning": "Needs Review",
        "critical": "Action Required",
        "failed": "Action Required",
        "skipped_config": "Not Assessed",
        "skipped_permission": "Not Assessed",
        "skipped_network": "Not Assessed",
    }.get(status, status.replace("_", " ").title())


def _status_class(label_or_status: str) -> str:
    normalized = label_or_status.strip().lower().replace("_", " ")
    if normalized in {"ok", "no findings"}:
        return "ok"
    if normalized in {"warning", "needs review"}:
        return "warning"
    if normalized in {"critical", "failed", "action required"}:
        return "critical"
    return "scoped"


def _status_tag_marker(label: str, status: str | None = None) -> str:
    css_class = _status_class(status or label)
    return f'<strong class="status-tag status-{css_class}">{escape(label)}</strong>'


def _decorate_status_labels(html_body: str) -> str:
    parts = re.split(r"(<h[1-6]\b[^>]*>.*?</h[1-6]>)", html_body, flags=re.DOTALL)
    for index, part in enumerate(parts):
        if re.fullmatch(r"<h[1-6]\b[^>]*>.*?</h[1-6]>", part, flags=re.DOTALL):
            continue
        parts[index] = _decorate_status_label_fragment(part)
    return "".join(parts)


def _decorate_status_label_fragment(html_body: str) -> str:
    replacements = {
        "Action Required": _status_tag_marker("Action Required"),
        "Needs Review": _status_tag_marker("Needs Review"),
        "Review Activity": _status_tag_marker("Review Activity", "warning"),
        "No Findings": _status_tag_marker("No Findings"),
        "Not Assessed": _status_tag_marker("Not Assessed"),
    }
    for label, badge in replacements.items():
        html_body = html_body.replace(label, badge)
    return html_body


def _markdown_headings(markdown_text: str) -> list[tuple[int, str, str]]:
    headings: list[tuple[int, str, str]] = []
    seen: dict[str, int] = {}
    for raw_line in markdown_text.splitlines():
        line = raw_line.strip()
        if line.startswith("### "):
            level = 3
            heading = line[4:].strip()
        elif line.startswith("## "):
            level = 2
            heading = line[3:].strip()
        else:
            continue
        if not heading:
            continue
        base_slug = _slugify_heading(heading)
        count = seen.get(base_slug, 0)
        seen[base_slug] = count + 1
        slug = base_slug if count == 0 else f"{base_slug}-{count + 1}"
        headings.append((level, heading, slug))
    return headings


def _slugify_heading(value: str) -> str:
    value = re.sub(
        r"\s+\((?:No Findings|Needs Review|Review Activity|Action Required|Not Assessed)\)\s*$",
        "",
        value,
    )
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized or "section"


def _add_heading_ids(html_body: str, headings: list[tuple[int, str, str]]) -> str:
    for level, heading, slug in headings:
        original = f"<h{level}>{escape(heading)}</h{level}>"
        replacement = f'<h{level} id="{escape(slug)}">{escape(heading)}</h{level}>'
        html_body = html_body.replace(original, replacement, 1)
    return html_body


def _html_section_nav(sections: list[tuple[str, str]]) -> str:
    if not sections:
        return ""
    links = [
        f'          <li><a href="#{escape(slug)}">{escape(heading)}</a></li>'
        for heading, slug in sections
    ]
    return "\n".join(
        [
            '      <nav class="report-toc" aria-label="Report sections">',
            '        <p class="toc-title">Report Sections</p>',
            '        <ol class="toc-list">',
            *links,
            "        </ol>",
            "      </nav>",
        ]
    )


def _html_logo_or_wordmark(brand: BrandProfile) -> str:
    if brand.logo_path:
        return (
            f'<img class="brand-logo" src="{_logo_data_uri(brand.logo_path)}" '
            f'alt="{escape(brand.logo_alt)}">'
        )
    text = brand.organization_name or brand.report_title
    return f'<div class="brand-wordmark">{escape(text)}</div>'


def _logo_data_uri(path: str) -> str:
    return _file_data_uri(path)


def _file_data_uri(path: str) -> str:
    file_path = Path(path)
    mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    encoded = b64encode(file_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _font_format_hint(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix == ".woff2":
        return "woff2"
    if suffix == ".woff":
        return "woff"
    if suffix == ".otf":
        return "opentype"
    if suffix == ".ttf":
        return "truetype"
    return ""


def _wrap_html_tables(html_body: str) -> str:
    return re.sub(r"<table>(.*?)</table>", _wrap_html_table, html_body, flags=re.DOTALL)


def _wrap_html_table(match: re.Match[str]) -> str:
    table_body = match.group(1)
    column_count = _html_table_column_count(table_body)
    table_classes = ["report-table", f"report-table-cols-{column_count}"]
    wrap_classes = ["table-wrap", f"table-wrap-cols-{column_count}"]
    density_class = _html_table_density_class(column_count)
    if density_class:
        table_classes.append(density_class)
        wrap_classes.append(density_class.replace("report-table", "table-wrap"))
    column_attr = f' data-column-count="{column_count}"' if column_count > 0 else ""
    return (
        f'<div class="{" ".join(wrap_classes)}">'
        f'<table class="{" ".join(table_classes)}"{column_attr}>{table_body}</table>'
        "</div>"
    )


def _html_table_column_count(table_body: str) -> int:
    first_row_match = re.search(r"<tr>(.*?)</tr>", table_body, flags=re.DOTALL)
    if first_row_match is None:
        return 0
    return len(re.findall(r"<t[hd]>", first_row_match.group(1)))


def _html_table_density_class(column_count: int) -> str:
    if column_count >= 12:
        return "report-table-ultra-wide"
    if column_count >= 10:
        return "report-table-extra-wide"
    if column_count >= 8:
        return "report-table-wide"
    return ""
