"""Extract tables and text from pages that have a real text layer."""
from __future__ import annotations

import bisect
from typing import List, Tuple

from .layout import build_blocks
from .models import Table, TextBlock, Word

LATTICE_SETTINGS = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
    "snap_tolerance": 3,
    "join_tolerance": 3,
    "intersection_tolerance": 3,
    "edge_min_length": 8,
}


def _cluster(values: List[float], tol: float = 2.0) -> List[float]:
    values = sorted(values)
    out: List[float] = []
    for v in values:
        if not out or v - out[-1] > tol:
            out.append(v)
    return out


def _index(edges: List[float], v: float, tol: float = 2.0) -> int:
    i = bisect.bisect_left(edges, v - tol)
    return min(i, len(edges) - 1)


def _lattice_table(pl_table, page_w: float, page_h: float) -> Table | None:
    """Convert a pdfplumber table into our Table, recovering merged cells."""
    cells = [c for c in pl_table.cells if c]
    if len(cells) < 4:
        return None
    xs = _cluster([c[0] for c in cells] + [c[2] for c in cells])
    ys = _cluster([c[1] for c in cells] + [c[3] for c in cells])
    n_cols, n_rows = len(xs) - 1, len(ys) - 1
    if n_cols < 2 or n_rows < 2:
        return None

    page = pl_table.page
    grid = [[""] * n_cols for _ in range(n_rows)]
    merges: List[Tuple[int, int, int, int]] = []
    for (x0, top, x1, bottom) in cells:
        c0, c1 = _index(xs, x0), _index(xs, x1) - 1
        r0, r1 = _index(ys, top), _index(ys, bottom) - 1
        c1, r1 = max(c0, c1), max(r0, r1)
        if r0 >= n_rows or c0 >= n_cols:
            continue
        try:
            crop = page.crop((x0, top, x1, bottom), strict=False)
            text = crop.extract_text(x_tolerance=1.5, y_tolerance=2) or ""
        except Exception:
            text = ""
        grid[r0][c0] = " ".join(text.split())  # collapse internal line breaks
        if r1 > r0 or c1 > c0:
            merges.append((r0, c0, min(r1, n_rows - 1), min(c1, n_cols - 1)))

    filled = sum(1 for r in grid for c in r if c)
    if filled < 2:
        return None  # a box drawn around a paragraph, not a table
    x0, top, x1, bottom = pl_table.bbox
    t = Table(rows=grid, source="lattice", merges=merges,
              bbox=(x0 / page_w, top / page_h, x1 / page_w, bottom / page_h))
    return t


def extract_digital_page(pl_page, gap_factor: float = 1.0) -> list:
    """Return blocks (Table / TextBlock) in reading order for a pdfplumber page."""
    page_w, page_h = float(pl_page.width), float(pl_page.height)
    blocks: List[Tuple[float, object]] = []
    table_boxes = []

    try:
        found = pl_page.find_tables(table_settings=LATTICE_SETTINGS)
    except Exception:
        found = []
    for pt in found:
        t = _lattice_table(pt, page_w, page_h)
        if t is None:
            continue
        x0, top, x1, bottom = pt.bbox
        # skip tables nested inside one we already have
        if any(a <= x0 + 1 and b <= top + 1 and c >= x1 - 1 and d >= bottom - 1
               for a, b, c, d in table_boxes):
            continue
        table_boxes.append(pt.bbox)
        blocks.append((top, t))

    words: List[Word] = []
    for w in pl_page.extract_words(x_tolerance=1.5, y_tolerance=2.5,
                                    keep_blank_chars=False, use_text_flow=False):
        cx = (w["x0"] + w["x1"]) / 2
        cy = (w["top"] + w["bottom"]) / 2
        if any(a <= cx <= c and b <= cy <= d for a, b, c, d in table_boxes):
            continue
        words.append(Word(w["text"], w["x0"], w["top"], w["x1"], w["bottom"]))

    for b in build_blocks(words, page_w, page_h, gap_factor=gap_factor):
        blocks.append((b.bbox[1] * page_h, b))

    blocks.sort(key=lambda x: x[0])
    return [b for _, b in blocks]


def words_in_rect(pl_page, rect) -> int:
    """Count words whose centre lies inside rect (x0, top, x1, bottom)."""
    x0, top, x1, bottom = rect
    n = 0
    for ch in pl_page.chars:
        cx = (ch["x0"] + ch["x1"]) / 2
        cy = (ch["top"] + ch["bottom"]) / 2
        if x0 <= cx <= x1 and top <= cy <= bottom and ch["text"].strip():
            n += 1
    return n


__all__ = ["extract_digital_page", "words_in_rect", "Table", "TextBlock"]
