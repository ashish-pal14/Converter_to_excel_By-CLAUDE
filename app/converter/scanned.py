"""OCR pipeline for scanned pages and images.

Steps
  1. render the page (or a clip of it) to a grayscale image
  2. fix 90/180/270 rotation (Tesseract OSD) and small skew (projection profile)
  3. normalise contrast
  4. detect ruled table grids with morphology, including merged cells
  5. erase ruling lines so they don't become OCR noise
  6. OCR the page once, then drop each word into its grid cell; words outside
     grids go through the same layout engine as digital pages
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from typing import List, Optional, Tuple

import cv2
import numpy as np
import pymupdf
import pytesseract

from .layout import build_blocks, group_lines, split_cells
from .models import Segment, Table, TextBlock, Word
from .segments import is_whiteish, luminance, ocr_size, words_to_segments


# --------------------------------------------------------------------------- rendering
def choose_dpi(page: pymupdf.Page, clip: Optional[pymupdf.Rect] = None,
               lo: int = 200, hi: int = 400) -> int:
    """Render at the scan's native resolution, clamped to a sensible range."""
    best = 300
    try:
        infos = page.get_image_info()
        area = clip or page.rect
        biggest = None
        for info in infos:
            r = pymupdf.Rect(info["bbox"]) & area
            if r.is_empty:
                continue
            if biggest is None or abs(r) > abs(biggest[0]):
                biggest = (r, info)
        if biggest:
            r, info = biggest
            bbox = pymupdf.Rect(info["bbox"])
            if bbox.width > 0 and info.get("width"):
                best = int(info["width"] / (bbox.width / 72.0))
    except Exception:
        pass
    return int(min(max(best, lo), hi))


def render_rgb(page: pymupdf.Page, dpi: int, clip: Optional[pymupdf.Rect] = None) -> np.ndarray:
    pix = page.get_pixmap(dpi=dpi, clip=clip, colorspace=pymupdf.csRGB, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    return img.copy()


def ink_gray(rgb: np.ndarray) -> np.ndarray:
    """Grayscale that keeps coloured text dark.

    Ordinary luminance turns light teal/green/orange text (often used for
    credits and refunds) into pale grey that OCR drops. Taking the darkest
    channel keeps white paper white and makes any saturated ink dark.
    """
    return rgb.min(axis=2)


def render_gray(page: pymupdf.Page, dpi: int, clip: Optional[pymupdf.Rect] = None) -> np.ndarray:
    pix = page.get_pixmap(dpi=dpi, clip=clip, colorspace=pymupdf.csGRAY, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    return img.copy()


# --------------------------------------------------------------------------- cleanup
_ROT = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}


def _quick_conf(gray: np.ndarray) -> float:
    """Mean OCR confidence on a central strip: cheap way to compare rotations."""
    h, w = gray.shape
    crop = gray[int(h * 0.2):int(h * 0.6), int(w * 0.1):int(w * 0.9)]
    scale = 1400 / max(crop.shape)
    if scale < 1:
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    try:
        d = pytesseract.image_to_data(crop, config="--oem 1 --psm 6",
                                      output_type=pytesseract.Output.DICT)
    except Exception:
        return 0.0
    confs = [float(c) for c, t in zip(d["conf"], d["text"]) if str(t).strip() and float(c) >= 0]
    words = [c for c in confs if c > 60]
    return len(words) * (sum(confs) / len(confs) if confs else 0)


def _looks_sideways(gray: np.ndarray) -> bool:
    """Text lines make the row profile spiky; sideways text makes the column profile spiky."""
    scale = 1000 / max(gray.shape)
    small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    _, bw = cv2.threshold(small, 0, 1, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    rows = bw.sum(axis=1).astype(float)
    cols = bw.sum(axis=0).astype(float)
    r = np.var(np.diff(rows)) / (rows.mean() ** 2 + 1e-6)
    c = np.var(np.diff(cols)) / (cols.mean() ** 2 + 1e-6)
    return c > 1.5 * r


def fix_orientation(gray: np.ndarray) -> Tuple[np.ndarray, int]:
    """Rotate pages scanned sideways or upside down."""
    angle, conf = 0, 0.0
    try:
        img = gray
        scale = 2500 / max(gray.shape)
        if scale < 1:
            img = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        osd = pytesseract.image_to_osd(img, config="--psm 0")
        angle = int(re.search(r"Rotate: (\d+)", osd).group(1))
        conf = float(re.search(r"Orientation confidence: ([\d.]+)", osd).group(1))
    except Exception:
        pass
    if angle in _ROT and conf >= 1.5:
        return cv2.rotate(gray, _ROT[angle]), angle
    # OSD unsure: check the layout and compare candidate rotations by OCR quality
    if _looks_sideways(gray):
        best, best_score = 0, _quick_conf(gray)
        for a in (90, 270):
            score = _quick_conf(cv2.rotate(gray, _ROT[a]))
            if score > best_score * 1.2:
                best, best_score = a, score
        if best:
            return cv2.rotate(gray, _ROT[best]), best
    return gray, 0


def estimate_skew(gray: np.ndarray, max_angle: float = 5.0, step: float = 0.2) -> float:
    """Find the small rotation that makes text rows most sharply separated."""
    scale = 800 / max(gray.shape)
    small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else gray
    _, bw = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    if bw.mean() < 1:
        return 0.0
    h, w = bw.shape
    center = (w / 2, h / 2)
    best_angle, best_score = 0.0, -1.0
    for angle in np.arange(-max_angle, max_angle + step / 2, step):
        m = cv2.getRotationMatrix2D(center, angle, 1.0)
        rotated = cv2.warpAffine(bw, m, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)
        profile = rotated.sum(axis=1).astype(np.float64)
        score = float(np.sum(np.diff(profile) ** 2))
        if score > best_score:
            best_score, best_angle = score, float(angle)
    return best_angle


def deskew(gray: np.ndarray) -> Tuple[np.ndarray, float]:
    angle = estimate_skew(gray)
    if abs(angle) < 0.15:
        return gray, 0.0
    h, w = gray.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    out = cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_LINEAR, borderValue=255)
    return out, angle


def rotate_small(img: np.ndarray, angle: float) -> np.ndarray:
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    border = 255 if img.ndim == 2 else (255, 255, 255)
    return cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_LINEAR, borderValue=border)


def blank_codes(gray: np.ndarray) -> Tuple[np.ndarray, int]:
    """White out QR codes: their modules OCR as random letters."""
    try:
        found, pts = cv2.QRCodeDetector().detectMulti(gray)
    except Exception:
        return gray, 0
    if not found or pts is None:
        return gray, 0
    out = gray.copy()
    for quad in pts:
        x, y, w, h = cv2.boundingRect(quad.astype(np.int32))
        pad = int(0.08 * max(w, h))
        out[max(0, y - pad):y + h + pad, max(0, x - pad):x + w + pad] = 255
    return out, len(pts)


def find_dark_bars(gray: np.ndarray) -> List[Tuple[int, int, int, int]]:
    """Wide dark bands with light text: table header bars, page footers, banners."""
    h, w = gray.shape
    dark = (gray < 100).astype(np.uint8) * 255
    closed = cv2.morphologyEx(dark, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(closed)
    bars = []
    for i in range(1, n):
        x, y, bw, bh, _ = stats[i]
        if bw < 0.15 * w or not (0.006 * h <= bh <= 0.12 * h):
            continue
        fill = dark[y:y + bh, x:x + bw].mean() / 255
        if fill > 0.55:
            bars.append((x, y, x + bw, y + bh))
    return bars


def invert_bars(gray: np.ndarray, bars) -> np.ndarray:
    """Turn white-on-dark text into dark-on-white so OCR can read it."""
    if not bars:
        return gray
    out = gray.copy()
    for x0, y0, x1, y1 in bars:
        out[y0:y1, x0:x1] = 255 - out[y0:y1, x0:x1]
    return out


def assign_colors(words: List[Word], gray: np.ndarray, rgb: Optional[np.ndarray], bars) -> None:
    """Record the ink colour of words printed in colour (e.g. green credits)."""
    if rgb is None:
        return
    for w in words:
        if any(x0 <= w.cx <= x1 and y0 <= w.cy <= y1 for x0, y0, x1, y1 in bars):
            continue
        x0, y0, x1, y1 = int(w.x0), int(w.top), int(w.x1) + 1, int(w.bottom) + 1
        crop = gray[y0:y1, x0:x1]
        if crop.size == 0:
            continue
        ink = crop < 170
        if ink.sum() < 8:
            continue
        mean = rgb[y0:y1, x0:x1][ink].mean(axis=0)
        if mean.max() - mean.min() >= 45:
            w.color = "{:02X}{:02X}{:02X}".format(*(int(v) for v in mean))


def enhance(gray: np.ndarray) -> np.ndarray:
    """Boost contrast on faded scans; leave clean scans alone."""
    if gray.std() < 50:
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        gray = clahe.apply(gray)
    return gray


# --------------------------------------------------------------------------- grids
@dataclass
class Grid:
    xs: List[int]
    ys: List[int]
    groups: List[Tuple[int, int, int, int]]   # (r0, c0, r1, c1) per logical cell
    bbox: Tuple[int, int, int, int]


def _line_masks(gray: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    bw = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                               cv2.THRESH_BINARY_INV, 25, 15)
    h, w = bw.shape
    # kernels must be longer than the tallest/widest letter, shorter than a cell border
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(w // 30, 40), 1))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(h // 40, 40)))
    horiz = cv2.morphologyEx(bw, cv2.MORPH_OPEN, hk)
    vert = cv2.morphologyEx(bw, cv2.MORPH_OPEN, vk)
    # bridge small breaks in faint lines
    horiz = cv2.dilate(horiz, cv2.getStructuringElement(cv2.MORPH_RECT, (9, 3)))
    vert = cv2.dilate(vert, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 9)))
    return horiz, vert


def _segment_positions(mask: np.ndarray, axis: int, tol: int) -> List[int]:
    """Centre positions of line segments along an axis (0 = horizontal lines -> y)."""
    n, _, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
    vals = []
    for i in range(1, n):
        vals.append(int(cents[i][1] if axis == 0 else cents[i][0]))
    vals.sort()
    clustered: List[List[int]] = []
    for v in vals:
        if clustered and v - clustered[-1][-1] <= tol:
            clustered[-1].append(v)
        else:
            clustered.append([v])
    return [int(statistics.mean(c)) for c in clustered]


def detect_grids(gray: np.ndarray) -> Tuple[List[Grid], np.ndarray, np.ndarray]:
    horiz, vert = _line_masks(gray)
    h, w = gray.shape
    both = cv2.bitwise_or(horiz, vert)
    contours, _ = cv2.findContours(both, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    grids: List[Grid] = []
    for cnt in contours:
        x, y, cw, ch = cv2.boundingRect(cnt)
        if cw < w * 0.15 or ch < h * 0.02:
            continue
        pad = 4
        x0, y0 = max(x - pad, 0), max(y - pad, 0)
        x1, y1 = min(x + cw + pad, w), min(y + ch + pad, h)
        hm = horiz[y0:y1, x0:x1]
        vm = vert[y0:y1, x0:x1]
        ys = [y0 + v for v in _segment_positions(hm, 0, tol=8)]
        xs = [x0 + v for v in _segment_positions(vm, 1, tol=8)]
        if len(xs) < 3 or len(ys) < 3:
            continue  # needs at least 2 columns and 2 rows of cells
        groups = _merge_groups(xs, ys, horiz, vert)
        grids.append(Grid(xs, ys, groups, (x0, y0, x1, y1)))
    grids.sort(key=lambda g: g.bbox[1])
    return grids, horiz, vert


def _merge_groups(xs, ys, horiz, vert):
    """Union cells whose shared border has no drawn line (merged cells)."""
    n_rows, n_cols = len(ys) - 1, len(xs) - 1
    parent = list(range(n_rows * n_cols))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        parent[find(a)] = find(b)

    def coverage(mask, x0, x1, y0, y1):
        region = mask[max(y0, 0):max(y1, 1), max(x0, 0):max(x1, 1)]
        if region.size == 0:
            return 0.0
        if x1 - x0 <= y1 - y0:   # vertical border: fraction of rows with ink
            return float((region.max(axis=1) > 0).mean())
        return float((region.max(axis=0) > 0).mean())

    for r in range(n_rows):
        for c in range(n_cols):
            ya, yb = ys[r], ys[r + 1]
            xa, xb = xs[c], xs[c + 1]
            my = max(int((yb - ya) * 0.2), 2)
            mx = max(int((xb - xa) * 0.2), 2)
            if c + 1 < n_cols:
                xc = xs[c + 1]
                if coverage(vert, xc - 4, xc + 5, ya + my, yb - my) < 0.5:
                    union(r * n_cols + c, r * n_cols + c + 1)
            if r + 1 < n_rows:
                yc = ys[r + 1]
                if coverage(horiz, xa + mx, xb - mx, yc - 4, yc + 5) < 0.5:
                    union(r * n_cols + c, (r + 1) * n_cols + c)

    members = {}
    for r in range(n_rows):
        for c in range(n_cols):
            members.setdefault(find(r * n_cols + c), []).append((r, c))
    groups = []
    for cells in members.values():
        rs = [a for a, _ in cells]
        cs = [b for _, b in cells]
        groups.append((min(rs), min(cs), max(rs), max(cs)))
    return groups


# --------------------------------------------------------------------------- OCR
def ocr_words(gray: np.ndarray, lang: str = "eng", psm: int = 11) -> List[Word]:
    data = pytesseract.image_to_data(
        gray, lang=lang, config=f"--oem 1 --psm {psm} -c preserve_interword_spaces=1",
        output_type=pytesseract.Output.DICT,
    )
    words: List[Word] = []
    for i, text in enumerate(data["text"]):
        text = (text or "").strip()
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1
        if not text or conf < 0:
            continue
        # drop isolated punctuation noise with very low confidence
        if conf < 30 and len(text) <= 2 and not any(ch.isalnum() for ch in text):
            continue
        # divider lines and box edges read as | [ ] characters
        if set(text) <= set("|[]{}_¦!"):
            continue
        # QR codes, logos and photo texture produce short, very unsure fragments;
        # anything real here is re-read by recover_missed_words
        if conf < 35 and len(text) <= 3 and not text.replace(",", "").replace(".", "").isdigit():
            continue
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        words.append(Word(text, x, y, x + w, y + h, conf))
    return words


def ocr_cell(gray: np.ndarray, box: Tuple[int, int, int, int], lang: str) -> Tuple[str, float]:
    """OCR one table cell on its own: local threshold handles shaded headers."""
    x0, y0, x1, y1 = box
    inset = max(4, int(min(x1 - x0, y1 - y0) * 0.06))
    crop = gray[y0 + inset:y1 - inset, x0 + inset:x1 - inset]
    if crop.size == 0 or crop.shape[0] < 6 or crop.shape[1] < 6:
        return "", 0.0
    _, bw = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    ink = (bw == 0).mean()
    if ink < 0.004 or ink > 0.6:
        return "", 0.0   # blank cell, or a solid fill
    if bw.shape[0] < 40:
        f = 40 / bw.shape[0]
        bw = cv2.resize(bw, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
    bw = cv2.copyMakeBorder(bw, 12, 12, 12, 12, cv2.BORDER_CONSTANT, value=255)
    try:
        data = pytesseract.image_to_data(bw, lang=lang, config="--oem 1 --psm 6",
                                         output_type=pytesseract.Output.DICT)
    except Exception:
        return "", 0.0
    toks, confs = [], []
    for t, c in zip(data["text"], data["conf"]):
        t = (t or "").strip()
        try:
            c = float(c)
        except (TypeError, ValueError):
            c = -1
        if t and c >= 0:
            toks.append(t)
            confs.append(c)
    if not toks:
        return "", 0.0
    return strip_border_noise(" ".join(toks)), min(confs)


def strip_border_noise(text: str) -> str:
    """Grid cells are bounded by lines; edge glyphs like | [ ] are line debris."""
    t = text.strip()
    t = re.sub(r"^[|\[\]{}()_~—–:;!']+\s*(?=\S)", lambda m: "(" if m.group(0).strip() == "(" else "", t)
    t = re.sub(r"\s*[|\[\]{}_~—–;!']+$", "", t)
    return t.strip(" |")


def recover_missed_words(gray: np.ndarray, words: List[Word], lang: str = "eng",
                         max_regions: int = 60) -> List[Word]:
    """Find ink that no recognised word covers and OCR those spots on their own."""
    if not words:
        return []
    med_h = statistics.median(w.height for w in words)
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    covered = np.zeros_like(bw)
    pad = max(2, int(med_h * 0.3))
    for w in words:
        cv2.rectangle(covered, (int(w.x0) - pad, int(w.top) - pad),
                      (int(w.x1) + pad, int(w.bottom) + pad), 255, -1)
    left = cv2.bitwise_and(bw, cv2.bitwise_not(covered))
    left = cv2.morphologyEx(left, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))  # drop speckle
    # join letters into word-sized blobs
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, int(med_h * 0.6)), max(1, int(med_h * 0.2))))
    blobs = cv2.dilate(left, k)
    n, _, stats, _ = cv2.connectedComponentsWithStats(blobs)
    found: List[Word] = []
    candidates = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if not (0.6 * med_h <= h <= 2.5 * med_h) or w < 0.8 * med_h:
            continue
        ink = left[y:y + h, x:x + w].mean() / 255
        if ink < 0.08:
            continue
        candidates.append((x, y, w, h))
    for x, y, w, h in candidates[:max_regions]:
        text, conf = ocr_cell(gray, (x - 6, y - 6, x + w + 6, y + h + 6), lang)
        if text and conf >= 50 and any(ch.isalnum() for ch in text):
            found.append(Word(text, x, y, x + w, y + h, conf))
    return found


_RULE_TOKEN = re.compile(r"^[eEwWaAoOcsS\-_=~.·•]{1,4}$")


def drop_rule_noise(words: List[Word]) -> List[Word]:
    """Dashed or dotted rules ('------ End ------') OCR as rows of 'ee', 'wee'..."""
    if not words:
        return words
    med_h = statistics.median(w.height for w in words)
    rows = {}
    for i, w in enumerate(words):
        rows.setdefault(int(w.cy // max(med_h, 1)), []).append(i)
    drop = set()
    for idx in rows.values():
        junk = [i for i in idx if _RULE_TOKEN.match(words[i].text)]
        if len(junk) >= 6 and len(junk) >= 0.5 * len(idx):
            drop.update(junk)
    return [w for i, w in enumerate(words) if i not in drop]


def _grid_to_table(grid: Grid, words: List[Word], img_w: int, img_h: int,
                   gray: Optional[np.ndarray] = None, lang: str = "eng",
                   retry_below: float = 80.0) -> Tuple[Table, set]:
    xs, ys = grid.xs, grid.ys
    n_rows, n_cols = len(ys) - 1, len(xs) - 1
    cell_words = {}
    used = set()
    group_of = {}
    for g in grid.groups:
        r0, c0, r1, c1 = g
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                group_of[(r, c)] = g
    for idx, w in enumerate(words):
        if not (xs[0] <= w.cx <= xs[-1] and ys[0] <= w.cy <= ys[-1]):
            continue
        c = max(0, min(n_cols - 1, int(np.searchsorted(xs, w.cx) - 1)))
        r = max(0, min(n_rows - 1, int(np.searchsorted(ys, w.cy) - 1)))
        g = group_of.get((r, c), (r, c, r, c))
        cell_words.setdefault((g[0], g[1]), []).append(w)
        used.add(idx)

    rows = [[""] * n_cols for _ in range(n_rows)]
    conf = [[100.0] * n_cols for _ in range(n_rows)]
    colors = [[None] * n_cols for _ in range(n_rows)]
    line_counts = []
    split_cells_seen = 0
    for (r, c), ws in cell_words.items():
        lines = group_lines(ws)
        line_counts.append(len(lines))
        # a wide gap inside a "cell" means the box is a card holding its own columns
        if any(len(split_cells(ln, gap_factor=2.5)) > 1 for ln in lines):
            split_cells_seen += 1
        rows[r][c] = strip_border_noise(" ".join(" ".join(w.text for w in ln) for ln in lines))
        conf[r][c] = min(w.conf for w in ws)
        cols = [w.color for w in ws if w.color]
        if len(cols) * 2 >= len(ws):
            colors[r][c] = max(set(cols), key=cols.count)
    # second pass on empty or doubtful cells
    if gray is not None:
        for (r0, c0, r1, c1) in grid.groups:
            current, cur_conf = rows[r0][c0], conf[r0][c0]
            if current and cur_conf >= retry_below:
                continue
            box = (xs[c0], ys[r0], xs[c1 + 1], ys[r1 + 1])
            text, c = ocr_cell(gray, box, lang)
            if text and (not current or c > cur_conf):
                rows[r0][c0], conf[r0][c0] = text, c

    merges = [g for g in grid.groups if g[0] != g[2] or g[1] != g[3]]
    x0, y0, x1, y1 = grid.bbox
    density = statistics.mean(line_counts) if line_counts else 0.0
    table = Table(rows=rows, source="ocr-grid", merges=merges, confidence=conf,
                  colors=colors if any(c for r in colors for c in r) else None,
                  bbox=(x0 / img_w, y0 / img_h, x1 / img_w, y1 / img_h))
    table.density = density
    table.inner_columns = split_cells_seen / max(len(cell_words), 1)
    return table, used


def _background(rgb: Optional[np.ndarray], gray: np.ndarray, box) -> Optional[str]:
    """Colour of the paper just around a piece of text (header bands, shaded rows)."""
    if rgb is None:
        return None
    h, w = gray.shape
    x0, y0, x1, y1 = (int(v) for v in box)
    pad = max(3, int((y1 - y0) * 0.3))
    X0, Y0, X1, Y1 = max(0, x0 - pad), max(0, y0 - pad), min(w, x1 + pad), min(h, y1 + pad)
    region = rgb[Y0:Y1, X0:X1].reshape(-1, 3)
    if region.size == 0:
        return None
    mask = np.ones((Y1 - Y0, X1 - X0), bool)
    mask[max(0, y0 - Y0):max(0, y1 - Y0), max(0, x0 - X0):max(0, x1 - X0)] = False
    ring = region[mask.reshape(-1)]
    if len(ring) < 10:
        return None
    med = np.median(ring, axis=0)
    color = "{:02X}{:02X}{:02X}".format(*(int(v) for v in med))
    return None if is_whiteish(color, 246) else color


def find_bands(rgb: Optional[np.ndarray]) -> List[Tuple[int, int, int, int, str]]:
    """Large, evenly coloured rectangles: header bars, footer bands, shaded strips."""
    if rgb is None:
        return []
    h, w = rgb.shape[:2]
    scale = 600 / max(h, w)
    small = cv2.resize(rgb, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    sh, sw = small.shape[:2]
    nonwhite = (small.min(axis=2) < 246).astype(np.uint8) * 255
    k = max(3, int(min(sh, sw) * 0.012))
    solid = cv2.morphologyEx(nonwhite, cv2.MORPH_OPEN, np.ones((k, k), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(solid)
    bands = []
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if bw < 0.2 * sw or area < 0.004 * sw * sh or area < 0.85 * bw * bh:
            continue
        if bw * bh > 0.5 * sw * sh:
            continue                     # tinted paper of a scan, not a band
        pixels = small[y:y + bh, x:x + bw].reshape(-1, 3)
        med = np.median(pixels, axis=0)
        # mostly one colour (text inside is allowed): a band, not a photo
        close = (np.abs(pixels.astype(int) - med).sum(axis=1) < 40).mean()
        if close < 0.6:
            continue
        color = "{:02X}{:02X}{:02X}".format(*(int(v) for v in med))
        bands.append((int(x / scale), int(y / scale), int((x + bw) / scale), int((y + bh) / scale), color))
    return bands


def find_pictures(rgb: Optional[np.ndarray], words: List[Word], bands) -> list:
    """Areas that are pictures (screenshots, photos, logos, QR codes), not text.

    They are copied into the spreadsheet as images; reading them with OCR
    would only produce scattered fragments.
    """
    if rgb is None:
        return []
    h, w = rgb.shape[:2]
    scale = 500 / max(h, w)
    small = cv2.resize(rgb, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    sh, sw = small.shape[:2]
    ink = (small.min(axis=2) < 230).astype(np.uint8) * 255
    k = max(3, int(sw * 0.02))
    merged = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, np.ones((k, k), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(merged)
    good = [wd for wd in words if wd.conf >= 70 and len(wd.text) >= 2]
    pictures = []
    for i in range(1, n):
        x, y, bw, bh, _ = stats[i]
        if bw < 0.03 * sw or bh < 0.012 * sh or bw * bh < 0.0012 * sw * sh:
            continue
        small_area = bw * bh < 0.008 * sw * sh
        X0, Y0, X1, Y1 = int(x / scale), int(y / scale), int((x + bw) / scale), int((y + bh) / scale)
        if any(abs(X0 - b[0]) < 0.02 * w and abs(X1 - b[2]) < 0.02 * w and abs(Y0 - b[1]) < 0.02 * h
               for b in bands):
            continue                      # a plain coloured band, already reproduced as a fill
        area = (X1 - X0) * (Y1 - Y0)
        text_area = sum(wd.height * (wd.x1 - wd.x0) for wd in good
                        if X0 <= wd.cx <= X1 and Y0 <= wd.cy <= Y1)
        region = small[y:y + bh, x:x + bw].reshape(-1, 3).astype(int)
        colourful = (region.max(axis=1) - region.min(axis=1) > 40).mean()
        varied = region.std(axis=0).mean()
        density = (ink[y:y + bh, x:x + bw] > 0).mean()   # pictures are mostly inked; text pages are not
        text_share = text_area / max(area, 1)
        # QR codes, photos: densely inked and varied. Screenshots, coloured graphics:
        # clearly colourful. Text areas and cards are neither.
        dense_picture = density >= 0.5 and varied > 35
        colour_picture = colourful >= 0.1 and density >= 0.25
        if small_area:
            # logos and badges: a solid coloured shape with at most a few words in it
            n_words = sum(1 for wd in words if X0 <= wd.cx <= X1 and Y0 <= wd.cy <= Y1)
            if density >= 0.6 and colourful >= 0.3 and n_words <= 6:
                pictures.append((X0, Y0, X1, Y1))
            continue
        if text_share < 0.10 and (dense_picture or colour_picture):
            pictures.append((X0, Y0, X1, Y1))
    # keep only outermost pictures
    outer = [p for p in pictures
             if not any(q != p and q[0] <= p[0] and q[1] <= p[1] and q[2] >= p[2] and q[3] >= p[3]
                        for q in pictures)]
    return outer


def picture_png(rgb: np.ndarray, box, max_side: int = 1400) -> bytes:
    x0, y0, x1, y1 = box
    crop = rgb[y0:y1, x0:x1]
    s = max_side / max(crop.shape[:2])
    if s < 1:
        crop = cv2.resize(crop, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    ok, png = cv2.imencode(".png", cv2.cvtColor(crop, cv2.COLOR_RGB2BGR))
    return png.tobytes() if ok else b""


def find_rules(horiz: np.ndarray, rgb: Optional[np.ndarray], grid_boxes, bands) -> list:
    """Horizontal separator lines that are not part of a ruled table or a band."""
    h, w = horiz.shape
    n, _, stats, _ = cv2.connectedComponentsWithStats(horiz)
    rules = []
    for i in range(1, n):
        x, y, bw, bh, _ = stats[i]
        if bw < 0.15 * w or bh > 0.01 * h:
            continue
        cy = y + bh / 2
        if any(gx0 <= x + bw / 2 <= gx1 and gy0 - 5 <= cy <= gy1 + 5 for gx0, gy0, gx1, gy1 in grid_boxes):
            continue
        if any(bx0 <= x + bw / 2 <= bx1 and by0 - 3 <= cy <= by1 + 3 for bx0, by0, bx1, by1, _ in bands):
            continue
        color = "7F7F7F"
        if rgb is not None:
            line = rgb[y:y + bh, x:x + bw].reshape(-1, 3)
            dark = line[line.min(axis=1) < 235]
            if len(dark):
                color = "{:02X}{:02X}{:02X}".format(*(int(v) for v in np.median(dark, axis=0)))
        rules.append((x, cy, x + bw, color))
    return rules


def build_segments(accepted, rest: List[Word], rgb, gray) -> List[Segment]:
    """Exact-copy segments in image pixels: grid cells plus free text."""
    segs: List[Segment] = []
    for grid, rows, conf, colors, words in accepted:
        xs, ys = grid.xs, grid.ys
        for r0, c0, r1, c1 in grid.groups:
            box = (xs[c0], ys[r0], xs[c1 + 1], ys[r1 + 1])
            inside = [w for w in words if box[0] <= w.cx <= box[2] and box[1] <= w.cy <= box[3]]
            text = rows[r0][c0]
            size_px = ocr_size(inside) if inside else None
            align = None
            if inside:
                left_gap = min(w.x0 for w in inside) - box[0]
                right_gap = box[2] - max(w.x1 for w in inside)
                if right_gap < left_gap * 0.5:
                    align = "right"
                elif abs(left_gap - right_gap) < 0.1 * (box[2] - box[0]) and left_gap > 8:
                    align = "center"
            seg = Segment(text, box[0], box[1], box[2], box[3], size=size_px or 0.0,
                          color=colors[r0][c0] if colors else None,
                          conf=conf[r0][c0] if text else 100.0, boxed=True, align=align)
            seg.fill = _background(rgb, gray, (box[0] + 4, box[1] + 4, box[2] - 4, box[3] - 4)) \
                if not inside else _background(rgb, gray, (min(w.x0 for w in inside), min(w.top for w in inside),
                                                         max(w.x1 for w in inside), max(w.bottom for w in inside)))
            segs.append(seg)
    free = words_to_segments(rest, gap_factor=1.2, size_from_height=True)
    for seg in free:
        seg.fill = _background(rgb, gray, (seg.x0, seg.top, seg.x1, seg.bottom))
    segs.extend(free)
    for seg in segs:
        if seg.fill and seg.color is None and luminance(seg.fill) < 110:
            seg.color = "FFFFFF"
    return segs


def extract_scanned_image(gray: Optional[np.ndarray], lang: str = "eng", step=None,
                          rgb: Optional[np.ndarray] = None, exact: bool = False) -> Tuple[list, dict]:
    """Run the full OCR pipeline on an image. Returns (blocks, info).

    Pass rgb to enable colour-aware reading (recommended); gray alone works too.
    """
    step = step or (lambda frac, msg: None)
    info = {}
    if rgb is not None:
        gray = ink_gray(rgb)
    step(0.05, "checking orientation")
    gray, rot = fix_orientation(gray)
    if rot:
        info["rotated"] = rot
        if rgb is not None:
            rgb = cv2.rotate(rgb, _ROT[rot])
    angle = estimate_skew(gray)
    if abs(angle) >= 0.15:
        gray = rotate_small(gray, angle)
        if rgb is not None:
            rgb = rotate_small(rgb, angle)
        info["deskewed"] = round(angle, 2)
    gray = enhance(gray)
    gray, codes = blank_codes(gray)
    if codes:
        info["qr_codes"] = codes
    bars = find_dark_bars(gray)
    gray = invert_bars(gray, bars)

    step(0.25, "finding tables")
    grids, horiz, vert = detect_grids(gray)
    # erase ruling lines before OCR (they turn into |, _ and l characters)
    clean = gray.copy()
    lines = cv2.bitwise_or(horiz, vert)
    for g in grids:
        x0, y0, x1, y1 = g.bbox
        region = lines[y0:y1, x0:x1]
        clean[y0:y1, x0:x1][region > 0] = 255

    step(0.35, "reading text")
    words = ocr_words(clean, lang=lang, psm=11)
    step(0.7, "double-checking unclear text")
    words += recover_missed_words(clean, words, lang=lang)
    words = drop_rule_noise(words)
    assign_colors(words, gray, rgb, bars)
    if words:
        info["mean_confidence"] = round(statistics.mean(w.conf for w in words), 1)
    h, w = gray.shape

    blocks = []
    used_all = set()
    accepted = []
    if grids:
        step(0.8, "checking table cells")
    for g in grids:
        table, used = _grid_to_table(g, words, w, h, gray=clean, lang=lang)
        raw = ([list(r) for r in table.rows], [list(c) for c in table.confidence],
               [list(c) for c in table.colors] if table.colors else None)
        table.normalise()
        # a box around a heading or logo is not a table, and neither are
        # rounded "cards" holding whole paragraphs: give their words back
        if (not used or len(table.rows) < 2 or table.n_cols < 2 or table.density > 1.8
                or table.inner_columns > 0.3):
            continue
        used_all |= used
        blocks.append((g.bbox[1] / h, table))
        accepted.append((g, raw[0], raw[1], raw[2], [words[i] for i in used]))

    rest = [wd for i, wd in enumerate(words) if i not in used_all]
    if exact:
        bands = find_bands(rgb)
        pictures = find_pictures(rgb, words, bands)

        def outside(x, y):
            return not any(px0 <= x <= px1 and py0 <= y <= py1 for px0, py0, px1, py1 in pictures)
        rest = [wd for wd in rest if outside(wd.cx, wd.cy)]
        info["segments"] = [sg for sg in build_segments(accepted, rest, rgb, gray)
                            if outside((sg.x0 + sg.x1) / 2, (sg.top + sg.bottom) / 2)]
        info["bands"] = [b for b in bands if outside((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)]
        info["rules"] = [r for r in find_rules(horiz, rgb, [g.bbox for g, *_ in accepted], bands)
                         if outside((r[0] + r[2]) / 2, r[1])]
        info["pictures"] = [(b, picture_png(rgb, b)) for b in pictures]
        info["image_size"] = (w, h)
        return [], info
    for b in build_blocks(rest, w, h, gap_factor=1.2):
        if isinstance(b, Table):
            b.source = "ocr-stream"
            cells = [c for row, crow in zip(b.rows, b.confidence or []) for v, c in zip(row, crow) if v]
            if cells and statistics.mean(cells) < 62:
                # mostly unreadable (photos, screenshots, logos): keep as text, not a table
                b = TextBlock(["    ".join(v for v in row if v) for row in b.rows], b.bbox)
        blocks.append((b.bbox[1], b))
    blocks.sort(key=lambda x: x[0])
    info["grids"] = len(grids)
    return [b for _, b in blocks], info


def extract_scanned_page(page: pymupdf.Page, lang: str = "eng",
                         clip: Optional[pymupdf.Rect] = None, step=None,
                         exact: bool = False) -> Tuple[list, dict]:
    dpi = choose_dpi(page, clip)
    rgb = render_rgb(page, dpi, clip)
    blocks, info = extract_scanned_image(None, lang=lang, step=step, rgb=rgb, exact=exact)
    if exact:
        # pixels -> points, shifted by the clip position on mixed pages
        k = 72.0 / dpi
        dx = clip.x0 if clip is not None else 0.0
        dy = clip.y0 if clip is not None else 0.0
        for seg in info["segments"]:
            seg.x0, seg.x1 = seg.x0 * k + dx, seg.x1 * k + dx
            seg.top, seg.bottom = seg.top * k + dy, seg.bottom * k + dy
            if seg.size:
                seg.size = max(6.0, min(28.0, round(seg.size * k * 2) / 2))
            else:
                seg.size = 9.0
        info["bands"] = [(x0 * k + dx, y0 * k + dy, x1 * k + dx, y1 * k + dy, c)
                         for x0, y0, x1, y1, c in info.get("bands", [])]
        info["rules"] = [(x0 * k + dx, y * k + dy, x1 * k + dx, c)
                         for x0, y, x1, c in info.get("rules", [])]
        info["pictures"] = [((b[0] * k + dx, b[1] * k + dy, b[2] * k + dx, b[3] * k + dy), png)
                            for b, png in info.get("pictures", []) if png]
        iw, ih = info["image_size"]
        info["page_size"] = (iw * k, ih * k)
    info["dpi"] = dpi
    return blocks, info
