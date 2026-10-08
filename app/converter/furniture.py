"""Remove page furniture: headers and footers repeated on every page.

Statements print the bank name, statement period, support address and
"Page N of M" on every page. Left in, they become junk rows and junk tables
and stop tables from joining across pages. The first copy is kept (it often
holds useful details such as the statement period); repeats are dropped.
OCR reads the same header slightly differently from page to page
("CSB Bank" / "CSS Bank"), so near-identical lines are treated as equal.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Dict, List

from .models import PageResult, Table, TextBlock

TOP, BOTTOM = 0.13, 0.88


def _letters(text: str) -> str:
    """Letters only, so page numbers and dates don't matter."""
    return re.sub(r"[^a-z]+", "", text.lower())


def _block_texts(b) -> List[str]:
    if isinstance(b, Table):
        return [" ".join(c for c in row if c) for row in b.rows]
    return list(b.lines)


def _in_margin(b) -> bool:
    return b.bbox[3] <= TOP or b.bbox[1] >= BOTTOM


def _canonical(seen_on: Dict[str, set]) -> Dict[str, str]:
    canon: Dict[str, str] = {}
    reps: List[str] = []
    for s in sorted(seen_on, key=len, reverse=True):
        for r in reps:
            if SequenceMatcher(None, s, r).ratio() >= 0.8:
                canon[s] = r
                break
        else:
            reps.append(s)
            canon[s] = s
    return canon


def remove_furniture(pages: List[PageResult]) -> None:
    real = [p for p in pages if p.blocks]
    if len(real) < 2:
        return
    needed = max(2, math.ceil(0.4 * len(real)))

    seen_on: Dict[str, set] = defaultdict(set)
    for p in real:
        for b in p.blocks:
            if _in_margin(b):
                for t in _block_texts(b):
                    s = _letters(t)
                    if len(s) >= 4:
                        seen_on[s].add(p.number)
    if not seen_on:
        return
    canon = _canonical(seen_on)
    pages_of: Dict[str, set] = defaultdict(set)
    for s, pg in seen_on.items():
        pages_of[canon[s]] |= pg
    repeated = {c for c, pg in pages_of.items() if len(pg) >= needed}
    if not repeated:
        return

    def sig(text: str) -> str:
        s = _letters(text)
        return canon.get(s, s)

    kept: set = set()
    for p in real:
        new_blocks = []
        for b in p.blocks:
            if not _in_margin(b):
                new_blocks.append(b)
                continue
            texts = _block_texts(b)
            sigs = [sig(t) for t in texts]
            if all(s in repeated or len(s) < 4 for s in sigs):
                key = tuple(s for s in sigs if s in repeated)
                if not key or all(s in kept for s in key):
                    continue                    # a repeat: drop the whole block
                kept.update(key)
                new_blocks.append(b)
                continue
            if isinstance(b, TextBlock):
                lines = []
                for t, s in zip(texts, sigs):
                    if s in repeated and s in kept:
                        continue
                    if s in repeated:
                        kept.add(s)
                    lines.append(t)
                if lines:
                    b.lines = lines
                    new_blocks.append(b)
            else:
                new_blocks.append(b)
        if len(new_blocks) != len(p.blocks):
            p.notes.append("repeated header/footer removed")
        p.blocks = new_blocks
