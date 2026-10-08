"""Collect positioned text for exact-copy output.

A segment is a run of words on one line that belong together (a value, a
label, a phrase), or the content of one ruled-table cell. Segments keep their
position, size, weight, colour and background so the spreadsheet can be laid
out like the page.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter
from typing import List, Optional, Sequence, Tuple

from .layout import group_lines
from .models import Segment, Word


# --------------------------------------------------------------------------- colours
def pdf_color_to_hex(color) -> Optional[str]:
    """pdfplumber colours are gray (1 value), RGB (3) or CMYK (4) in 0..1."""
    if color is None:
        return None
    if isinstance(color, (int, float)):
        color = (color,)
    try:
        vals = [float(v) for v in color]
    except (TypeError, ValueError):
        return None
    if len(vals) == 1:
        r = g = b = vals[0]
    elif len(vals) == 3:
        r, g, b = vals
    elif len(vals) == 4:
        c, m, y, k = vals
        r, g, b = (1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k)
    else:
        return None
    return "{:02X}{:02X}{:02X}".format(*(max(0, min(255, round(v * 255))) for v in (r, g, b)))


def is_whiteish(hex_color: Optional[str], limit: int = 238) -> bool:
    if not hex_color:
        return True
    return all(int(hex_color[i:i + 2], 16) >= limit for i in (0, 2, 4))


def is_blackish(hex_color: Optional[str], limit: int = 60) -> bool:
    if not hex_color:
        return True
    return all(int(hex_color[i:i + 2], 16) <= limit for i in (0, 2, 4))


def luminance(hex_color: str) -> float:
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    return 0.299 * r + 0.587 * g + 0.114 * b


# --------------------------------------------------------------------------- words -> segments
def _majority(values):
    vals = [v for v in values if v]
    if not vals:
        return None
    v, n = Counter(vals).most_common(1)[0]
    return v if n * 2 >= len(values) else None


ASCENDERS = set("bdfhklt0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ()[]{}|/\\$₹€£%&@#!?'\"")
DESCENDERS = set("gjpqy,;()[]{}|")


def em_ratio(text: str) -> float:
    """Height of a word's ink box as a share of the font size.

    'Spends' (capital + descender) is much taller than 'name' (x-height
    letters only) in the same font; dividing by this ratio gives a consistent
    font size from OCR box heights.
    """
    asc = any(ch in ASCENDERS for ch in text)
    desc = any(ch in DESCENDERS for ch in text)
    if asc and desc:
        return 0.92
    if asc:
        return 0.72
    if desc:
        return 0.73
    return 0.52


def ocr_size(words: Sequence[Word]) -> float:
    """Font size (in the words' units) from their box heights."""
    estimates = [w.height / em_ratio(w.text) for w in words if w.text.strip()]
    return statistics.median(estimates) if estimates else 10.0


def _make_segment(words: Sequence[Word], scale: float, dx: float, dy: float,
                  size_from_height: bool) -> Segment:
    x0 = min(w.x0 for w in words) * scale + dx
    x1 = max(w.x1 for w in words) * scale + dx
    top = min(w.top for w in words) * scale + dy
    bottom = max(w.bottom for w in words) * scale + dy
    sizes = [w.size for w in words if w.size]
    if sizes:
        size = statistics.median(sizes)
    elif size_from_height:
        # still in image units here, converted to points by the caller
        size = ocr_size(words) * scale
    else:
        size = 10.0
    if not size_from_height:
        size = max(6.0, min(28.0, round(size * 2) / 2))
    text = " ".join(w.text for w in words)
    if size_from_height:
        text = fix_bullet(text)
    return Segment(
        text=text,
        x0=x0, top=top, x1=x1, bottom=bottom, size=size,
        bold=sum(1 for w in words if w.bold) * 2 > len(words),
        # punctuation-only fragments ('.', ':') are too small to show their colour
        color=_majority([w.color for w in words if any(ch.isalnum() for ch in w.text)] or
                        [w.color for w in words]),
        conf=min(w.conf for w in words),
    )


_BULLET = re.compile(r"^(?:[eoO«°*·•]|e\.)\s+(?=[A-Z0-9])")


def fix_bullet(text: str) -> str:
    """OCR reads list bullets as 'e', 'o' or '«'; show them as bullets again."""
    return _BULLET.sub("• ", text, count=1)


def words_to_segments(words: Sequence[Word], gap_factor: float = 1.0, scale: float = 1.0,
                      dx: float = 0.0, dy: float = 0.0, size_from_height: bool = False) -> List[Segment]:
    """Group words into lines, then split lines at wide gaps."""
    segments: List[Segment] = []
    for line in group_lines(words):
        # group_lines may join a label with a slightly offset value; split them
        # back by vertical position so each keeps its own place
        h = statistics.median(w.height for w in line)
        current = [line[0]]
        for prev, w in zip(line, line[1:]):
            far = w.x0 - prev.x1 > h * gap_factor
            other_row = abs(w.cy - prev.cy) > 0.6 * h
            if far or other_row:
                segments.append(_make_segment(current, scale, dx, dy, size_from_height))
                current = [w]
            else:
                current.append(w)
        segments.append(_make_segment(current, scale, dx, dy, size_from_height))
    return segments


def apply_fills(segments: List[Segment], fills: List[Tuple[float, float, float, float, str]],
                page_area: float) -> None:
    """Give each segment the colour of the smallest filled box behind it."""
    usable = [f for f in fills if (f[2] - f[0]) * (f[3] - f[1]) < 0.6 * page_area
              and not is_whiteish(f[4])]
    for s in segments:
        cx, cy = (s.x0 + s.x1) / 2, (s.top + s.bottom) / 2
        best = None
        for x0, y0, x1, y1, color in usable:
            if x0 - 0.5 <= cx <= x1 + 0.5 and y0 - 0.5 <= cy <= y1 + 0.5:
                area = (x1 - x0) * (y1 - y0)
                if best is None or area < best[0]:
                    best = (area, color)
        if best:
            s.fill = best[1]
            if s.color is None and luminance(best[1]) < 110:
                s.color = "FFFFFF"   # text on a dark band is light


# --------------------------------------------------------------------------- digital pages
def _is_bold(fontname: str) -> bool:
    f = (fontname or "").lower()
    return any(k in f for k in ("bold", "black", "heavy", "semibold", "demi"))


def digital_fills(pl_page) -> List[Tuple[float, float, float, float, str]]:
    fills = []
    area = float(pl_page.width) * float(pl_page.height)
    for r in pl_page.rects:
        if not r.get("fill"):
            continue
        color = pdf_color_to_hex(r.get("non_stroking_color"))
        w, h = r["x1"] - r["x0"], r["bottom"] - r["top"]
        if color and not is_whiteish(color) and w * h < 0.6 * area and w > 4 and h > 4:
            fills.append((r["x0"], r["top"], r["x1"], r["bottom"], color))
    return fills


def digital_rules(pl_page, table_boxes=()) -> List[Tuple[float, float, float, str]]:
    """Horizontal lines (and hairline rectangles) drawn between rows."""
    rules = []
    width = float(pl_page.width)
    items = [(ln["x0"], ln["top"], ln["x1"], ln["bottom"], ln.get("stroking_color")) for ln in pl_page.lines]
    items += [(r["x0"], r["top"], r["x1"], r["bottom"], r.get("non_stroking_color") or r.get("stroking_color"))
              for r in pl_page.rects if r["bottom"] - r["top"] <= 1.5]
    for x0, top, x1, bottom, col in items:
        if bottom - top > 1.5 or x1 - x0 < 0.1 * width:
            continue
        y = (top + bottom) / 2
        if any(a - 1 <= (x0 + x1) / 2 <= c + 1 and b - 2 <= y <= d + 2 for a, b, c, d in table_boxes):
            continue
        rules.append((x0, y, x1, pdf_color_to_hex(col) or "7F7F7F"))
    return rules


def digital_segments(pl_page, lattice_settings: dict) -> List[Segment]:
    segments: List[Segment] = []
    table_boxes = []
    try:
        found = pl_page.find_tables(table_settings=lattice_settings)
    except Exception:
        found = []
    for pt in found:
        cells = [c for c in pt.cells if c]
        if len(cells) < 4:
            continue
        texts = []
        for (x0, top, x1, bottom) in cells:
            try:
                crop = pl_page.crop((x0, top, x1, bottom), strict=False)
                chars = [ch for ch in crop.chars if ch["text"].strip()]
                text = " ".join((crop.extract_text(x_tolerance=1.5, y_tolerance=2) or "").split())
            except Exception:
                chars, text = [], ""
            texts.append(text)
            size = statistics.median(ch["size"] for ch in chars) if chars else 9.0
            bold = sum(_is_bold(ch.get("fontname", "")) for ch in chars) * 2 > len(chars) if chars else False
            color = _majority([pdf_color_to_hex(ch.get("non_stroking_color")) for ch in chars])
            if color and is_blackish(color):
                color = None
            align = None
            if chars:
                left_gap = min(ch["x0"] for ch in chars) - x0
                right_gap = x1 - max(ch["x1"] for ch in chars)
                if right_gap < left_gap * 0.5:
                    align = "right"
                elif abs(left_gap - right_gap) < 2 and left_gap > 4:
                    align = "center"
            segments.append(Segment(text, x0, top, x1, bottom, size=size, bold=bold,
                                    color=color, boxed=True, align=align))
        if sum(1 for t in texts if t) >= 2:
            table_boxes.append(pt.bbox)
        else:
            segments = segments[:len(segments) - len(cells)]  # a frame, not a table

    words: List[Word] = []
    for w in pl_page.extract_words(x_tolerance=1.5, y_tolerance=2.5, keep_blank_chars=False,
                                   use_text_flow=False,
                                   extra_attrs=["size", "fontname", "non_stroking_color"]):
        cx, cy = (w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2
        if any(a <= cx <= c and b <= cy <= d for a, b, c, d in table_boxes):
            continue
        color = pdf_color_to_hex(w.get("non_stroking_color"))
        if color and is_blackish(color):
            color = None
        words.append(Word(w["text"], w["x0"], w["top"], w["x1"], w["bottom"],
                          color=color, size=w.get("size"), bold=_is_bold(w.get("fontname", ""))))
    segments.extend(words_to_segments(words, gap_factor=1.0))

    fills = []
    for r in pl_page.rects:
        if not r.get("fill"):
            continue
        color = pdf_color_to_hex(r.get("non_stroking_color"))
        if color:
            fills.append((r["x0"], r["top"], r["x1"], r["bottom"], color))
    apply_fills(segments, fills, float(pl_page.width) * float(pl_page.height))
    return segments
