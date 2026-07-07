from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO

from pypdf import PdfReader, PdfWriter

try:
    import pdfplumber
except ImportError:  # pragma: no cover - exercised only when optional dependency is absent.
    pdfplumber = None


@dataclass(slots=True)
class SplitPdfPage:
    page_number: int
    pdf_bytes: bytes
    raw_text: str


def split_pdf_pages(
    pdf_bytes: bytes,
    on_page: Callable[[int], None] | None = None,
) -> list[SplitPdfPage]:
    reader = PdfReader(BytesIO(pdf_bytes))
    pages: list[SplitPdfPage] = []

    plumber_pages = None
    if pdfplumber is not None:
        plumber_pages = pdfplumber.open(BytesIO(pdf_bytes))

    try:
        for index, page in enumerate(reader.pages, start=1):
            writer = PdfWriter()
            writer.add_page(page)
            output = BytesIO()
            writer.write(output)
            raw_text = ""
            if plumber_pages is not None and index <= len(plumber_pages.pages):
                raw_text = plumber_pages.pages[index - 1].extract_text() or ""
            if not raw_text:
                raw_text = page.extract_text() or ""

            pages.append(
                SplitPdfPage(
                    page_number=index,
                    pdf_bytes=output.getvalue(),
                    raw_text=raw_text,
                )
            )
            if on_page is not None:
                on_page(index)
    finally:
        if plumber_pages is not None:
            plumber_pages.close()

    return pages
