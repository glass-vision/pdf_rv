from __future__ import annotations

from io import BytesIO

from pypdf import PdfReader, PdfWriter


def assemble_pdf(page_pdf_bytes: list[bytes]) -> bytes:
    writer = PdfWriter()

    for page_bytes in page_pdf_bytes:
        reader = PdfReader(BytesIO(page_bytes))
        for page in reader.pages:
            writer.add_page(page)

    output = BytesIO()
    writer.write(output)
    return output.getvalue()
