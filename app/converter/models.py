"""Plain data structures passed between converter stages."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class Word:
    """A single word with its position on the page (PDF points or pixels)."""
    text: str
    x0: float
    top: float
    x1: float
    bottom: float
    conf: float = 100.0  # OCR confidence 0-100; digital text is always 100
    color: Optional[str] = None  # hex ink colour when the text is clearly coloured (e.g. "1AA39A")
    size: Optional[float] = None   # font size in points, when known
    bold: bool = False

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def height(self) -> float:
        return max(self.bottom - self.top, 0.1)


@dataclass
class Segment:
    """A piece of text at a fixed place on the page, for exact-copy output.

    Coordinates are in points from the page's top-left corner.
    """
    text: str
    x0: float
    top: float
    x1: float
    bottom: float
    size: float = 10.0
    bold: bool = False
    color: Optional[str] = None        # text colour (hex)
    fill: Optional[str] = None         # background colour (hex)
    conf: float = 100.0
    boxed: bool = False                # a cell of a ruled table: keep its borders and span
    align: Optional[str] = None        # "left" | "right" | "center" when known

    @property
    def height(self) -> float:
        return max(self.bottom - self.top, 0.1)


@dataclass
class Table:
    rows: List[List[str]]
    source: str                      # "lattice", "stream", "ocr-grid", "ocr-stream"
    bbox: Tuple[float, float, float, float] = (0, 0, 0, 0)  # normalised 0..1 page coords
    # merged ranges as (row0, col0, row1, col1), inclusive, 0-based
    merges: List[Tuple[int, int, int, int]] = field(default_factory=list)
    # parallel to rows: per-cell OCR confidence (None for digital)
    confidence: Optional[List[List[float]]] = None
    # parallel to rows: per-cell ink colour (hex) when coloured, else None
    colors: Optional[List[List[Optional[str]]]] = None
    page: int = 0                    # 1-based page number where the table starts
    pages: List[int] = field(default_factory=list)  # all pages it spans after stitching
    # (first row index, page) for each page a stitched table continues onto
    page_starts: List[Tuple[int, int]] = field(default_factory=list)
    density: float = 1.0          # mean text lines per filled cell (grids only)
    inner_columns: float = 0.0    # share of cells that contain their own columns (grids only)

    def page_of_row(self, i: int) -> int:
        page = self.page
        for start, p in self.page_starts:
            if i >= start:
                page = p
        return page

    @property
    def n_cols(self) -> int:
        return max((len(r) for r in self.rows), default=0)

    def normalise(self) -> "Table":
        """Pad rows to equal width and drop fully empty rows/columns."""
        width = self.n_cols
        rows = [list(r) + [""] * (width - len(r)) for r in self.rows]
        conf = None
        if self.confidence is not None:
            conf = [list(c) + [100.0] * (width - len(c)) for c in self.confidence]
        cols = None
        if self.colors is not None:
            cols = [list(c) + [None] * (width - len(c)) for c in self.colors]

        keep_rows = [i for i, r in enumerate(rows) if any(str(c).strip() for c in r)]
        keep_cols = [j for j in range(width) if any(str(rows[i][j]).strip() for i in keep_rows)]
        row_map = {old: new for new, old in enumerate(keep_rows)}
        col_map = {old: new for new, old in enumerate(keep_cols)}

        self.rows = [[rows[i][j] for j in keep_cols] for i in keep_rows]
        if conf is not None:
            self.confidence = [[conf[i][j] for j in keep_cols] for i in keep_rows]
        if cols is not None:
            self.colors = [[cols[i][j] for j in keep_cols] for i in keep_rows]

        merges = []
        for r0, c0, r1, c1 in self.merges:
            rs = [row_map[i] for i in range(r0, r1 + 1) if i in row_map]
            cs = [col_map[j] for j in range(c0, c1 + 1) if j in col_map]
            if rs and cs and (len(rs) > 1 or len(cs) > 1):
                merges.append((min(rs), min(cs), max(rs), max(cs)))
        self.merges = merges
        return self


@dataclass
class TextBlock:
    lines: List[str]
    bbox: Tuple[float, float, float, float] = (0, 0, 0, 0)


@dataclass
class PageResult:
    number: int                  # 1-based
    kind: str                    # "digital" | "scanned" | "empty"
    blocks: list = field(default_factory=list)   # Table | TextBlock, in reading order
    notes: List[str] = field(default_factory=list)
    mean_confidence: Optional[float] = None
    segments: List[Segment] = field(default_factory=list)   # exact-copy content
    # filled background areas in points: (x0, top, x1, bottom, hex colour)
    fills: List[Tuple[float, float, float, float, str]] = field(default_factory=list)
    # horizontal separator lines in points: (x0, y, x1, hex colour)
    rules: List[Tuple[float, float, float, str]] = field(default_factory=list)
    # pictures in points: ((x0, top, x1, bottom), png bytes)
    pictures: List[Tuple[Tuple[float, float, float, float], bytes]] = field(default_factory=list)
    width: float = 595.0                                     # page size in points
    height: float = 842.0

    @property
    def tables(self) -> List[Table]:
        return [b for b in self.blocks if isinstance(b, Table)]
