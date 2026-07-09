from __future__ import annotations

from io import BytesIO
from typing import Iterable

import pdfplumber
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Highlight
from pypdf.generic import ArrayObject, FloatObject, NameObject, NumberObject


def _estimated_row_rect(width: float, height: float, row_order: int) -> tuple[float, float, float, float]:
    row_height = 42.0
    top_margin = 100.0
    left_margin = 40.0
    right_margin = 30.0
    row_index = max(int(row_order), 1) - 1
    y_top = max(height - top_margin - (row_index * row_height), 24.0)
    y_bottom = max(y_top - 34.0, 18.0)
    return left_margin, y_bottom, max(width - right_margin, left_margin + 10.0), y_top


def _row_background_rect(
    page,
    width: float,
    height: float,
    min_top: float,
    max_bottom: float,
) -> tuple[float, float, float, float] | None:
    """Find the printed row-striping rectangles (alternating column-cell
    fills) whose vertical span covers the matched text, and use their exact
    union as the highlight box. This matches the row's real printed
    boundaries pixel-for-pixel instead of approximating them from text
    positions, so the highlight looks flush with the row instead of falling
    a bit short at an edge."""
    center = (min_top + max_bottom) / 2.0
    candidates = [
        r
        for r in page.rects
        if r.get("fill")
        and (r["x1"] - r["x0"]) > 4.0
        and r["top"] <= center <= r["bottom"]
    ]
    if not candidates:
        return None

    top = min(r["top"] for r in candidates)
    bottom = max(r["bottom"] for r in candidates)
    same_row = [r for r in candidates if abs(r["top"] - top) < 0.5 and abs(r["bottom"] - bottom) < 0.5]
    if not same_row:
        return None

    x1 = min(r["x0"] for r in same_row)
    x2 = max(r["x1"] for r in same_row)
    if (x2 - x1) < width * 0.5:
        return None

    return x1, height - bottom, x2, height - top


def _best_hit_cluster(hits: list[dict], row_window: float = 7.0) -> list[dict]:
    """A search term (check number, amount, ...) can legitimately appear
    more than once on a statement page - e.g. a bounced check is deposited,
    returned, and redeposited with the identical check number and amount on
    the same page, or a batch of same-timestamp deposits repeats a shared
    date/time term on many consecutive single-line rows. Taking the min/max
    span across every hit for every term would draw one box spanning all of
    those unrelated rows.

    Instead of chaining hits that are merely close to their neighbor (which
    still merges an entire block of tightly-packed consecutive rows into one
    cluster), anchor an independent window around every hit and score it by
    how many distinct terms fall inside that window. The intended row is the
    one whose window covers the most distinct search terms; a shared term
    that also happens to land in a neighboring row's window doesn't inflate
    that neighbor's score, since it is the same term index already counted
    for its own row.
    """
    if not hits:
        return []

    def center(hit: dict) -> float:
        return (float(hit["top"]) + float(hit["bottom"])) / 2.0

    best_score = -1
    best_group: list[dict] = [hits[0]]
    for anchor in hits:
        anchor_center = center(anchor)
        group = [hit for hit in hits if abs(center(hit) - anchor_center) <= row_window]
        score = len({hit.get("_term_index") for hit in group})
        if score > best_score:
            best_score = score
            best_group = group
    return best_group


def _text_match_row_rect(
    pdf_bytes: bytes,
    width: float,
    height: float,
    search_terms: Iterable[str],
) -> tuple[float, float, float, float] | None:
    terms = [term.strip() for term in search_terms if term and term.strip()]
    if not terms:
        return None

    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        if not pdf.pages:
            return None
        page = pdf.pages[0]
        hits = []
        for term_index, term in enumerate(terms):
            for hit in page.search(term, regex=False, return_chars=True):
                hit["_term_index"] = term_index
                hits.append(hit)
        if not hits:
            return None
        words = page.extract_words()
        word_tops = sorted({float(word["top"]) for word in words})

        cluster = _best_hit_cluster(hits)
        min_top = min(float(hit["top"]) for hit in cluster)
        max_bottom = max(float(hit["bottom"]) for hit in cluster)

        row_rect = _row_background_rect(page, width, height, min_top, max_bottom)
        if row_rect is not None:
            return row_rect

        # A row's own detail/description cell can wrap onto its own second
        # (or third) printed line, e.g. a long transfer reference. That
        # continuation line has no word starting near the row's own left
        # edge - only the first line of a *new* row does. Treating any word
        # anywhere on the page as "the next row" cuts the highlight off
        # after just the first line, leaving the continuation outside the
        # box. Only accept a top as the next row boundary if some word there
        # starts close to this row's own left edge.
        first_line_words = [w for w in words if abs(float(w["top"]) - min_top) < 1.5]
        left_edge = min((float(w["x0"]) for w in first_line_words), default=None)

        # Some statement layouts print a rotated marginal watermark/footer
        # (e.g. a vertical form-code strip along the page edge). pdfplumber
        # reports its glyphs as "words" positioned well to the left of the
        # table's real left column, with inflated bounding-box heights since
        # the glyphs are rotated 90 degrees. Left uncaught, one of those
        # fragments can get mistaken for a neighboring row and pull the box
        # toward it. The table's real content never starts meaningfully to
        # the left of this row's own left edge, so drop anything that does.
        table_words = words if left_edge is None else [w for w in words if float(w["x0"]) > left_edge - 10.0]

        if left_edge is not None:
            row_start_tops = sorted({
                float(w["top"]) for w in table_words if abs(float(w["x0"]) - left_edge) < 5.0
            })
            next_tops = [top for top in row_start_tops if top > max_bottom + 1.0]
        else:
            next_tops = [top for top in word_tops if top > max_bottom + 1.0]

        # Split the whitespace gap to the neighboring row evenly on both
        # sides instead of a fixed pad above and a different fixed pad
        # below. An uneven split (e.g. 2pt above, 3pt below) is only a
        # rounding error on paper, but at 100% zoom it reads as the box
        # sitting closer to one neighbor than the other instead of centered
        # in its own row.
        prior_bottoms = [float(w["bottom"]) for w in table_words if float(w["top"]) < min_top - 0.5]
        prev_bottom = max(prior_bottoms) if prior_bottoms else None

    top_bound = (prev_bottom + min_top) / 2.0 if prev_bottom is not None else max(min_top - 4.0, 0.0)
    bottom_bound = (max_bottom + next_tops[0]) / 2.0 if next_tops else (max_bottom + 4.0)
    top_bound = max(top_bound, 0.0)
    bottom_bound = min(bottom_bound, height)
    y_top = height - top_bound
    y_bottom = height - bottom_bound
    return 40.0, max(y_bottom, 18.0), max(width - 30.0, 50.0), min(y_top, height - 18.0)


def highlight_pdf_row(pdf_bytes: bytes, row_order: int, search_terms: Iterable[str] = ()) -> bytes:
    reader = PdfReader(BytesIO(pdf_bytes))
    if not reader.pages:
        return pdf_bytes

    writer = PdfWriter()
    writer.append(reader)
    page = writer.pages[0]
    if "/Annots" in page:
        del page["/Annots"]
    width = float(page.mediabox.width)
    height = float(page.mediabox.height)
    x1, y1, x2, y2 = _text_match_row_rect(pdf_bytes, width, height, search_terms) or _estimated_row_rect(
        width,
        height,
        row_order,
    )
    x1 = max(x1, 18.0)
    y1 = max(y1, 18.0)
    x2 = min(x2, width - 18.0)
    y2 = min(y2, height - 18.0)
    annotation = Highlight(
        rect=(x1, y1, x2, y2),
        quad_points=ArrayObject(
            [
                FloatObject(x1), FloatObject(y2),
                FloatObject(x2), FloatObject(y2),
                FloatObject(x1), FloatObject(y1),
                FloatObject(x2), FloatObject(y1),
            ]
        ),
        highlight_color="ffff00",
    )
    annotation.update(
        {
            NameObject("/Border"): ArrayObject([NumberObject(0), NumberObject(0), NumberObject(0)]),
        }
    )
    writer.add_annotation(
        page_number=0,
        annotation=annotation,
    )

    output = BytesIO()
    writer.write(output)
    return output.getvalue()
