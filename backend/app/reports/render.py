"""Render a report document to HTML and PDF (step 3.9).

HTML is Jinja2 with autoescaping on, so a finding title containing markup is escaped
rather than rendered. The PDF is WeasyPrint over the same HTML, which keeps the two
exports identical by construction.

WeasyPrint needs system libraries (pango, cairo, ...) that are installed in the API
image but not on every developer machine. When they are missing, :func:`render_pdf`
raises :class:`PdfUnavailable` instead of returning a corrupt file, and the endpoint
turns that into a clear 503 - the HTML report still works.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.reports.document import ReportDocument

TEMPLATE_DIR = Path(__file__).parent / "templates"

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


class PdfUnavailable(RuntimeError):
    """The process cannot produce PDFs: WeasyPrint or its system libraries are absent."""


def render_html(document: ReportDocument) -> str:
    return _env.get_template("report.html.j2").render(doc=document)


def render_pdf(html: str) -> bytes:
    try:
        import weasyprint

        return weasyprint.HTML(string=html, base_url=str(TEMPLATE_DIR)).write_pdf()
    except PdfUnavailable:
        raise
    except (ImportError, OSError, ValueError) as exc:
        raise PdfUnavailable(
            "PDF export is unavailable in this process: WeasyPrint needs the pango and "
            "cairo system libraries (they are installed in the API image)"
        ) from exc


__all__ = ["render_html", "render_pdf", "PdfUnavailable"]
