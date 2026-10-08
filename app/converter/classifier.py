"""Decide, page by page, whether text can be read directly or needs OCR."""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

import pymupdf


@dataclass
class PageClass:
    kind: str            # "digital" | "scanned" | "empty"
    text_chars: int
    image_coverage: float
    garbage_ratio: float
    reason: str


def _garbage_ratio(text: str) -> float:
    """Share of characters that indicate a broken font mapping or bad OCR layer."""
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    bad = 0
    for c in chars:
        cat = unicodedata.category(c)
        if c == "\ufffd" or cat in ("Co", "Cn", "Cc", "Cs"):
            bad += 1
    return bad / len(chars)


def _image_coverage(page: pymupdf.Page) -> float:
    area = abs(page.rect)
    if area <= 0:
        return 0.0
    covered = 0.0
    try:
        for info in page.get_image_info():
            r = pymupdf.Rect(info["bbox"]) & page.rect
            covered += abs(r)
    except Exception:
        return 0.0
    return min(covered / area, 1.0)


def _drawing_segments(page: pymupdf.Page) -> int:
    """Number of path segments on the page (letters drawn as outlines produce thousands)."""
    try:
        return sum(len(d["items"]) for d in page.get_drawings())
    except Exception:
        return 0


def classify_page(page: pymupdf.Page, force_ocr: bool = False) -> PageClass:
    text = page.get_text("text") or ""
    n_chars = sum(1 for c in text if not c.isspace())
    coverage = _image_coverage(page)
    garbage = _garbage_ratio(text)

    if force_ocr:
        return PageClass("scanned", n_chars, coverage, garbage, "OCR forced by user")
    if n_chars < 25 and coverage < 0.10:
        # "Print to PDF" and some report generators draw every letter as a
        # vector shape. There is no text layer, but the page is full of
        # content, so it must be rendered and read with OCR.
        segments = _drawing_segments(page)
        if segments >= 300:
            return PageClass("scanned", n_chars, coverage, garbage,
                             "text is drawn as shapes (printed to PDF)")
        if n_chars == 0:
            return PageClass("empty", 0, coverage, 0.0, "no text or images")
    if garbage > 0.25:
        return PageClass("scanned", n_chars, coverage, garbage,
                         "text layer is unreadable (broken font encoding)")
    if n_chars < 25 and coverage > 0.10:
        return PageClass("scanned", n_chars, coverage, garbage, "page is an image")
    if coverage > 0.60 and n_chars < 150:
        return PageClass("scanned", n_chars, coverage, garbage,
                         "mostly image with little text")
    return PageClass("digital", n_chars, coverage, garbage, "text layer found")
