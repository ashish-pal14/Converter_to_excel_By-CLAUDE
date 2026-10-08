"""Join tables that continue from one page to the next."""
from __future__ import annotations

from typing import List

from .cleaning import looks_like_header
from .models import PageResult, Table


def _norm_row(row) -> List[str]:
    return [" ".join(str(c).lower().split()) for c in row]


def _continues(prev: Table, nxt: Table) -> bool:
    if prev.n_cols != nxt.n_cols or prev.n_cols < 2:
        return False
    # the table must end low on its page and the next must start high on its page
    if prev.bbox[3] < 0.55 or nxt.bbox[1] > 0.45:
        return False
    if prev.rows and nxt.rows and _norm_row(prev.rows[0]) == _norm_row(nxt.rows[0]):
        return True                       # repeated header row
    # no header on the continuation: next table must start with data
    return not looks_like_header(nxt.rows[0]) if nxt.rows else False


def _only_blocks_between(page: PageResult, table: Table, where: str) -> bool:
    """True if table is the last (or first) real block, ignoring headers/footers."""
    blocks = page.blocks
    idx = blocks.index(table)
    others = blocks[idx + 1:] if where == "last" else blocks[:idx]
    for b in others:
        in_margin = b.bbox[1] >= 0.88 or b.bbox[3] <= 0.13
        if in_margin:
            continue                     # page header / footer
        if isinstance(b, Table) or len(b.lines) > 3:
            return False
    return True


def stitch(pages: List[PageResult]) -> List[PageResult]:
    """Merge continuation tables into the table they continue (in place).

    A table stays "open" while it is the last block on its page. If the next
    page starts with a table of the same width that is either headed by the
    same header row or starts directly with data, the rows are appended.
    Chains across 3+ pages work because the open table carries forward.
    """
    open_tbl: Table | None = None

    def body_tables(page):
        return [t for t in page.tables if not (t.bbox[1] >= 0.88 or t.bbox[3] <= 0.13)]

    for page in pages:
        tables = body_tables(page)
        first = tables[0] if tables else None
        if (open_tbl is not None and first is not None
                and _only_blocks_between(page, first, "first")
                and _continues(open_tbl, first)):
            _append(open_tbl, first)
            page.blocks.remove(first)
            page.notes.append(f"table continued from page {open_tbl.page}")
            tables = body_tables(page)
            if tables:
                last = tables[-1]
                open_tbl = last if _only_blocks_between(page, last, "last") else None
            # else: the whole page was the continuation, keep the table open
            continue
        if tables and _only_blocks_between(page, tables[-1], "last"):
            open_tbl = tables[-1]
        else:
            open_tbl = None
    return pages


def _append(prev: Table, nxt: Table) -> None:
    # skip every repeated header row (multi-row headers repeat as a block)
    skip = 0
    while (skip < min(3, len(prev.rows), len(nxt.rows))
           and _norm_row(prev.rows[skip]) == _norm_row(nxt.rows[skip])):
        skip += 1
    offset = len(prev.rows) - skip
    if prev.confidence is not None or nxt.confidence is not None:
        pc = prev.confidence or [[100.0] * prev.n_cols for _ in prev.rows]
        nc = nxt.confidence or [[100.0] * nxt.n_cols for _ in nxt.rows]
        prev.confidence = pc + nc[skip:]
    if prev.colors is not None or nxt.colors is not None:
        pcol = prev.colors or [[None] * prev.n_cols for _ in prev.rows]
        ncol = nxt.colors or [[None] * nxt.n_cols for _ in nxt.rows]
        prev.colors = pcol + ncol[skip:]
    prev.page_starts.append((len(prev.rows), nxt.page))
    prev.rows.extend(nxt.rows[skip:])
    for r0, c0, r1, c1 in nxt.merges:
        if r0 >= skip:
            prev.merges.append((r0 + offset, c0, r1 + offset, c1))
    if not prev.pages:
        prev.pages = [prev.page]
    prev.pages.append(nxt.page)
    prev.bbox = (prev.bbox[0], prev.bbox[1], prev.bbox[2], nxt.bbox[3])
