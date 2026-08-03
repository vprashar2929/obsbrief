from __future__ import annotations

import os
import sys
from pathlib import Path


def write_pdf_from_html(html_document: str, pdf_path: Path, *, base_url: Path) -> None:
    _prepare_weasyprint_native_library_path()
    try:
        from weasyprint import HTML
    except OSError as exc:
        raise RuntimeError(
            "PDF generation requires WeasyPrint native libraries. Install the platform "
            "packages documented by WeasyPrint, including GLib/Pango, then rerun the report."
        ) from exc

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html_document, base_url=str(base_url.resolve())).write_pdf(str(pdf_path))


def _prepare_weasyprint_native_library_path() -> None:
    if sys.platform != "darwin":
        return

    library_dirs = [
        str(path) for path in (Path("/opt/homebrew/lib"), Path("/usr/local/lib")) if path.is_dir()
    ]
    if not library_dirs:
        return

    for env_name in ("DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH"):
        current = [item for item in os.environ.get(env_name, "").split(":") if item]
        updated = [item for item in library_dirs if item not in current]
        if updated:
            os.environ[env_name] = ":".join([*updated, *current])
