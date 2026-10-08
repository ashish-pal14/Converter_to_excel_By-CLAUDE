"""Reconstruct tables and text blocks from positioned words.

This is used for:
  * digital PDFs whose tables have no ruling lines ("stream" tables)
  * text that sits outside ruled tables
  * OCR output from scanned pages

Algorithm
---------
1. Group words into visual lines by vertical overlap.
2. Split each line into cells wherever the horizontal gap is large compared
   with the font height (a normal space is ~0.3 em, a column gap is > 1 em).
3. Find table regions: runs of consecutive lines that contain 2+ cells.
4. Inside a region, find column boundaries as vertical whitespace "rivers"
   (x ranges that no narrow cell touches), then place every cell into the
   column it overlaps most. Cells spanning several columns are recorded as
   merged ranges.
5. Join wrapped lines (a description that continues on the next line) back
   into the row above.
"""
from __future__ import annotations

import statistics
import re
from typing import List, Optional, Sequence, Tuple

import numpy as np

from .models import Table, TextBlock, Word

Cell = Tuple[float, float, str, float, Optional[str]]  # x0, x1, text, min_conf, colour


# --------------------------------------------------------------------------- lines
def group_lines(words: Sequence[Word]) -> List[List[Word]]:
    """Group words into lines using vertical overlap, top to bottom."""
    words = sorted((w for w in words if w.text.strip()), key=lambda w: (w.cy, w.x0))
    lines: List[List[Word]] = []
    bands: List[Tuple[float, float]] = []
    for w in words:
        placed = False
        # check the last few lines only: lines are produced in vertical order
        for i in range(len(lines) - 1, max(-1, len(lines) - 4), -1):
            top, bottom = bands[i]
            overlap = min(bottom, w.bottom) - max(top, w.top)
            if overlap >= 0.5 * min(bottom - top, w.height):
                lines[i].append(w)
                bands[i] = (min(top, w.top), max(bottom, w.bottom))
                placed = True
                break
        if not placed:
            lines.append([w])
            bands.append((w.top, w.bottom))
    for line in lines:
        line.sort(key=lambda w: w.x0)
    lines.sort(key=lambda ln: statistics.median(w.cy for w in ln))
    return _merge_offset_lines(lines)


def _merge_offset_lines(lines: List[List[Word]]) -> List[List[Word]]:
    """Join a label and its value when the value sits a little lower or higher.

    Some statements print amounts in a different font or colour that rides a
    few pixels off the label's baseline, so the two don't overlap enough to
    count as one line. If two neighbouring lines don't overlap horizontally
    and their centres are close, they are the same row.
    """
    out: List[List[Word]] = []
    for ln in lines:
        if out:
            prev = out[-1]
            h = min(statistics.median(w.height for w in prev), statistics.median(w.height for w in ln))
            dy = abs(statistics.median(w.cy for w in ln) - statistics.median(w.cy for w in prev))
            px0, px1 = min(w.x0 for w in prev), max(w.x1 for w in prev)
            lx0, lx1 = min(w.x0 for w in ln), max(w.x1 for w in ln)
            apart = lx0 > px1 + h or lx1 < px0 - h
            short = min(len(prev), len(ln)) <= 4      # one side is a value or a label
            if apart and (dy < 0.8 * h or (short and dy < 1.05 * h)):
                prev.extend(ln)
                prev.sort(key=lambda w: w.x0)
                continue
        out.append(ln)
    return out


def split_cells(line: Sequence[Word], gap_factor: float = 1.0) -> List[Cell]:
    """Split one line into cells where the gap between words is wide."""
    if not line:
        return []
    h = statistics.median(w.height for w in line)
    threshold = h * gap_factor
    cells: List[Cell] = []
    cur = [line[0]]
    for prev, w in zip(line, line[1:]):
        if w.x0 - prev.x1 > threshold:
            cells.append(_cell(cur))
            cur = [w]
        else:
            cur.append(w)
    cells.append(_cell(cur))
    return cells


def _cell(words: Sequence[Word]) -> Cell:
    voters = [w for w in words if any(ch.isalnum() for ch in w.text)] or list(words)
    colors = [w.color for w in voters if w.color]
    # a cell is coloured when most of its (non-punctuation) words are
    color = max(set(colors), key=colors.count) if len(colors) * 2 >= len(voters) else None
    return (
        min(w.x0 for w in words),
        max(w.x1 for w in words),
        " ".join(w.text for w in words),
        min(w.conf for w in words),
        color,
    )


# --------------------------------------------------------------------------- regions
def _line_box(line: Sequence[Word]) -> Tuple[float, float, float, float]:
    return (min(w.x0 for w in line), min(w.top for w in line),
            max(w.x1 for w in line), max(w.bottom for w in line))


def build_blocks(words: Sequence[Word], page_w: float, page_h: float,
                 gap_factor: float = 1.0, min_table_rows: int = 2) -> list:
    """Return Table and TextBlock objects in reading order."""
    lines = group_lines(words)
    if not lines:
        return []
    line_cells = [split_cells(ln, gap_factor) for ln in lines]
    heights = [statistics.median(w.height for w in ln) for ln in lines]
    med_h = statistics.median(heights) if heights else 10.0
    boxes = [_line_box(ln) for ln in lines]

    # mark lines that look tabular
    multi = [len(c) >= 2 for c in line_cells]

    # Rows of some statements are spaced far apart (3-4 line heights). Learn the
    # usual spacing on this page so widely spaced rows still form one table.
    gaps = [boxes[k][1] - boxes[k - 1][3] for k in range(1, len(lines))]
    wide = sorted(g for g in gaps if g > med_h)
    typical_wide = wide[len(wide) // 2] if len(wide) >= 3 else 0.0
    max_gap = max(2.5 * med_h, 1.6 * typical_wide)

    # build runs of tabular lines; allow one single-cell line inside a run
    # (section header or wrapped cell) when both neighbours are tabular
    regions: List[Tuple[int, int]] = []
    i, n = 0, len(lines)
    while i < n:
        if not multi[i]:
            i += 1
            continue
        start = end = i
        j = i + 1
        while j < n:
            big_gap = boxes[j][1] - boxes[j - 1][3] > max_gap
            if big_gap:
                break
            if multi[j]:
                end = j
                j += 1
            elif heights[j] > 1.3 * med_h:
                break  # a heading ends the table
            elif j + 1 < n and multi[j + 1] and boxes[j + 1][1] - boxes[j][3] <= max_gap:
                j += 1  # tolerate one single-cell line
            else:
                break
        # keep trailing continuation lines (e.g. the time under the last date)
        while (end + 1 < n and not multi[end + 1] and heights[end + 1] <= 1.3 * med_h
               and boxes[end + 1][1] - boxes[end][3] < 0.8 * med_h):
            end += 1
        rows_in_region = sum(multi[start:end + 1])
        if rows_in_region >= min_table_rows:
            regions.append((start, end))
        i = end + 1

    blocks: list = []
    used = set()
    for start, end in regions:
        table = _region_to_table(lines[start:end + 1], line_cells[start:end + 1],
                                 heights[start:end + 1], page_w, page_h)
        if table is not None and table.n_cols >= 2:
            blocks.append((boxes[start][1], table))
            used.update(range(start, end + 1))

    # remaining lines become text blocks (consecutive lines grouped)
    current: List[str] = []
    cur_top = None
    cur_box = None
    for idx in range(n):
        if idx in used:
            if current:
                blocks.append((cur_top, TextBlock(current, _norm(cur_box, page_w, page_h))))
                current, cur_top, cur_box = [], None, None
            continue
        text = "    ".join(c[2] for c in line_cells[idx])
        if cur_top is None:
            cur_top = boxes[idx][1]
            cur_box = list(boxes[idx])
        else:
            gap = boxes[idx][1] - boxes[idx - 1][3]
            if gap > 2.0 * med_h:
                blocks.append((cur_top, TextBlock(current, _norm(cur_box, page_w, page_h))))
                current, cur_top, cur_box = [], boxes[idx][1], list(boxes[idx])
            cur_box = [min(cur_box[0], boxes[idx][0]), min(cur_box[1], boxes[idx][1]),
                       max(cur_box[2], boxes[idx][2]), max(cur_box[3], boxes[idx][3])]
        current.append(text)
    if current:
        blocks.append((cur_top, TextBlock(current, _norm(cur_box, page_w, page_h))))

    blocks.sort(key=lambda b: b[0])
    return [b for _, b in blocks]


def _norm(box, page_w, page_h):
    if not box:
        return (0, 0, 0, 0)
    return (box[0] / page_w, box[1] / page_h, box[2] / page_w, box[3] / page_h)


# --------------------------------------------------------------------------- columns
def find_columns(cells_per_line: List[List[Cell]], min_gap: float) -> List[Tuple[float, float]]:
    """Find column x-ranges from whitespace rivers between narrow cells."""
    all_cells = [c for cells in cells_per_line if len(cells) >= 2 for c in cells]
    if not all_cells:
        return []
    x_min = min(c[0] for c in all_cells)
    x_max = max(c[1] for c in all_cells)
    width = x_max - x_min
    if width <= 0:
        return [(x_min, x_max)]

    # cells wider than 45% of the region are likely spanning titles; ignore
    narrow = [c for c in all_cells if (c[1] - c[0]) < 0.45 * width] or all_cells

    scale = 1.0 if width > 200 else 4.0  # finer bins for tiny regions
    size = int(width * scale) + 2
    coverage = np.zeros(size, dtype=np.int32)
    for x0, x1, *_ in narrow:
        a = int((x0 - x_min) * scale)
        b = int((x1 - x_min) * scale) + 1
        coverage[a:b] += 1

    columns: List[Tuple[float, float]] = []
    in_col, start = False, 0
    for k, v in enumerate(coverage):
        if v > 0 and not in_col:
            in_col, start = True, k
        elif v == 0 and in_col:
            in_col = False
            columns.append((start / scale + x_min, k / scale + x_min))
    if in_col:
        columns.append((start / scale + x_min, size / scale + x_min))

    # merge columns separated by a gap too thin to be real
    merged: List[List[float]] = []
    for c in columns:
        if merged and c[0] - merged[-1][1] < min_gap:
            merged[-1][1] = c[1]
        else:
            merged.append([c[0], c[1]])
    return [(a, b) for a, b in merged]


def _overlap(a0, a1, b0, b1) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _split_on_word_rivers(lines, cells_per_line, columns, med_h):
    """Split columns at narrow gaps that run through every row.

    Cell splitting needs a wide gap, but some layouts put the date only a
    little left of the description. A gap between words that lines up in
    every row of a table is a column boundary even if it is narrow.
    """
    rows = [ln for ln, cells in zip(lines, cells_per_line) if len(cells) >= 2]
    if len(rows) < 4:
        return columns
    # a boundary must be clearly wider than an ordinary space between words,
    # otherwise "01 Jul 26" in a fixed-width layout splits into three columns
    spaces = [b.x0 - a.x1 for ln in rows for a, b in zip(ln, ln[1:])
              if 0 < b.x0 - a.x1 < 1.5 * med_h]
    space = statistics.median(spaces) if spaces else 0.3 * med_h
    min_gap = max(0.45 * med_h, 1.8 * space)
    out = []
    for c0, c1 in columns:
        words = [[w for w in ln if c0 - 1 <= w.cx <= c1 + 1] for ln in rows]
        words = [ws for ws in words if ws]
        if len(words) < 4:
            out.append((c0, c1))
            continue
        width = int(c1 - c0) + 2
        cover = np.zeros(width, dtype=np.int32)
        for ws in words:
            for w in ws:
                cover[max(0, int(w.x0 - c0)):max(0, int(w.x1 - c0)) + 1] += 1
        bounds = []
        k = 0
        while k < width:
            if cover[k] == 0:
                start = k
                while k < width and cover[k] == 0:
                    k += 1
                gap = k - start
                left_ok = start > 0
                right_ok = k < width - 1
                if gap >= min_gap and left_ok and right_ok:
                    x = c0 + start + gap / 2
                    left = sum(1 for ws in words if any(w.cx < x for w in ws))
                    right = sum(1 for ws in words if any(w.cx > x for w in ws))
                    if left >= 0.6 * len(words) and right >= 0.6 * len(words):
                        bounds.append((c0 + start, c0 + k))
            k += 1
        if not bounds:
            out.append((c0, c1))
            continue
        a = c0
        for g0, g1 in bounds:
            out.append((a, g0))
            a = g1
        out.append((a, c1))
    return out


def _resplit_cells(line, cells, columns):
    """Break cells that straddle a column boundary into per-column pieces."""
    if len(columns) < 2:
        return cells
    edges = [(columns[k][1] + columns[k + 1][0]) / 2 for k in range(len(columns) - 1)]
    out = []
    for cell in cells:
        x0, x1 = cell[0], cell[1]
        inner = [e for e in edges if x0 < e < x1]
        if not inner:
            out.append(cell)
            continue
        words = [w for w in line if x0 - 0.5 <= w.x0 and w.x1 <= x1 + 0.5]
        groups, current, bi = [], [], 0
        for w in sorted(words, key=lambda w: w.x0):
            while bi < len(inner) and w.cx > inner[bi]:
                if current:
                    groups.append(current)
                current = []
                bi += 1
            current.append(w)
        if current:
            groups.append(current)
        out.extend(_cell(g) for g in groups)
    return out


def _region_to_table(lines, cells_per_line, heights, page_w, page_h):
    med_h = statistics.median(heights) if heights else 10.0
    columns = find_columns(cells_per_line, min_gap=0.4 * med_h)
    columns = _split_on_word_rivers(lines, cells_per_line, columns or [], med_h)
    if len(columns) < 2:
        return None
    cells_per_line = [_resplit_cells(ln, cells, columns) if len(cells) >= 2 else cells
                      for ln, cells in zip(lines, cells_per_line)]

    n_cols = len(columns)
    rows: List[List[str]] = []
    confs: List[List[float]] = []
    colors: List[List[Optional[str]]] = []
    merges = []
    tops: List[float] = []
    bottoms: List[float] = []
    for ln, cells in zip(lines, cells_per_line):
        row = [""] * n_cols
        conf = [100.0] * n_cols
        color: List[Optional[str]] = [None] * n_cols
        for x0, x1, text, cf, col in cells:
            overlaps = [_overlap(x0, x1, c0, c1) for c0, c1 in columns]
            if max(overlaps) <= 0:
                # falls in a gap: nearest column by centre
                cx = (x0 + x1) / 2
                best = min(range(n_cols), key=lambda k: abs(cx - (columns[k][0] + columns[k][1]) / 2))
                spanned = [best]
            else:
                best = int(np.argmax(overlaps))
                # a column counts as spanned when the cell covers its centre
                spanned = [k for k, (c0, c1) in enumerate(columns)
                           if x0 <= (c0 + c1) / 2 <= x1] or [best]
            target = min(spanned) if len(spanned) > 1 else best
            row[target] = (row[target] + " " + text).strip() if row[target] else text
            conf[target] = min(conf[target], cf)
            color[target] = color[target] or col
            if len(spanned) > 1 and len(cells) == 1:
                merges.append((len(rows), min(spanned), len(rows), max(spanned)))
        rows.append(row)
        confs.append(conf)
        colors.append(color)
        tops.append(min(w.top for w in ln))
        bottoms.append(max(w.bottom for w in ln))

    rows, confs, colors, merges = _join_wrapped_rows(rows, confs, colors, merges, tops, bottoms, med_h)

    x0 = min(c[0] for c in columns)
    x1 = max(c[1] for c in columns)
    bbox = (x0 / page_w, tops[0] / page_h, x1 / page_w, bottoms[-1] / page_h)
    return Table(rows=rows, source="stream", bbox=bbox, merges=merges, confidence=confs,
                 colors=colors if any(c for r in colors for c in r) else None)


TIME_RX = re.compile(r"^\d{1,2}([:.]\d{2})?\s*([AaPp]\.?[Mm]\.?)$|^\d{1,2}:\d{2}(:\d{2})?$")
MONEYISH = re.compile(r"\d[\d,]*\.\d{2}\b|^[^\d]*\d{1,3}(,\d{2,3})+\b")


def _looks_numeric(s: str) -> bool:
    digits = sum(ch.isdigit() for ch in s)
    return digits > 0 and digits >= 0.5 * len(s.replace(" ", ""))


def _compact_digits(s: str) -> str:
    """OCR sometimes splits digits with a space ('0 7 pm'): join them back."""
    return re.sub(r"(?<=\d) (?=\d\b)|(?<=\b\d) (?=\d)", "", s.strip())


def _is_time(s: str) -> bool:
    return bool(TIME_RX.match(s.strip()) or TIME_RX.match(_compact_digits(s)))


def _is_amount(s: str) -> bool:
    s = s.strip()
    if _is_time(s):
        return False
    return bool(MONEYISH.search(s)) or (_looks_numeric(s) and len(s) >= 3)


def _join_wrapped_rows(rows, confs, colors, merges, tops, bottoms, med_h):
    """Append continuation lines to the row above.

    A continuation line sits much closer to the line above than rows sit to
    each other, fills fewer columns than a normal row, and carries no amount.
    Typical cases: a description wrapped onto two lines, or a date whose time
    is printed underneath ("06 Jul 26 03:" / "04 pm").
    """
    if len(rows) < 2:
        return rows, confs, colors, merges
    merged_rows = {m[0] for m in merges}
    full_counts = [sum(1 for c in r if c) for r in rows]
    typical = statistics.median(full_counts) if full_counts else 0
    gaps = [tops[i] - bottoms[i - 1] for i in range(1, len(rows))]
    row_gap = statistics.median(gaps) if gaps else med_h
    close = min(0.8 * med_h, 0.5 * row_gap) if row_gap > med_h else 0.6 * med_h

    out_rows, out_conf, out_col = [rows[0]], [confs[0]], [colors[0]]
    out_bottoms = [bottoms[0]]
    row_map = {0: 0}
    for i in range(1, len(rows)):
        r = rows[i]
        filled = [k for k, c in enumerate(r) if c]
        prev = out_rows[-1]
        gap = tops[i] - out_bottoms[-1]
        is_continuation = (
            filled
            and typical >= 2
            and len(filled) < typical
            and i not in merged_rows
            and gap < close
            and all(prev[k] for k in filled)          # extends text that is already there
            and not any(_is_amount(r[k]) for k in filled)
            and (filled[0] != 0 or _is_time(r[0]) or typical >= 3)
        )
        if is_continuation:
            for k in filled:
                piece = _compact_digits(r[k]) if _is_time(r[k]) else r[k]
                prev[k] = prev[k] + " " + piece
                out_conf[-1][k] = min(out_conf[-1][k], confs[i][k])
            out_bottoms[-1] = bottoms[i]
            row_map[i] = len(out_rows) - 1
        else:
            out_rows.append(r)
            out_conf.append(confs[i])
            out_col.append(colors[i])
            out_bottoms.append(bottoms[i])
            row_map[i] = len(out_rows) - 1
    new_merges = [(row_map[a], b, row_map[c], d) for a, b, c, d in merges]
    return out_rows, out_conf, out_col, new_merges
