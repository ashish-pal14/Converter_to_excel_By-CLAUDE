"""End-to-end conversion: file in, spreadsheet bytes out."""
from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import pdfplumber
import pymupdf

from .classifier import classify_page
from .digital import LATTICE_SETTINGS, extract_digital_page, words_in_rect
from .exact import build_exact_sheets, exact_preview, grid_rows, write_exact_xlsx
from .segments import digital_fills, digital_rules, digital_segments
from .models import PageResult, Table, TextBlock
from .scanned import extract_scanned_page
from .furniture import remove_furniture
from .stitching import stitch
from .writer import Options, build_sheets, preview, write_csv_zip, write_json, write_xlsx

log = logging.getLogger(__name__)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif"}
ProgressFn = Callable[[float, str], None]


class ConversionError(Exception):
    """A problem the user can fix (wrong password, unreadable file...)."""


@dataclass
class ConvertRequest:
    path: str
    filename: str = ""
    output: str = "xlsx"                # xlsx | csv | json
    password: Optional[str] = None
    force_ocr: bool = False
    ocr_lang: str = "eng"
    pages: Optional[List[int]] = None   # 1-based page numbers, None = all
    max_pages: int = 500
    options: Options = field(default_factory=Options)


@dataclass
class ConvertResult:
    data: bytes
    extension: str
    media_type: str
    pages: List[PageResult]
    preview: list
    summary: dict


def _open_document(req: ConvertRequest) -> pymupdf.Document:
    ext = os.path.splitext(req.filename or req.path)[1].lower()
    try:
        if ext in IMAGE_EXTS:
            img = pymupdf.open(req.path)
            pdf_bytes = img.convert_to_pdf()
            img.close()
            return pymupdf.open("pdf", pdf_bytes)
        doc = pymupdf.open(req.path, filetype="pdf")
    except Exception as e:  # noqa: BLE001
        raise ConversionError(f"This file could not be opened as a PDF or image ({e}).") from e
    if doc.needs_pass:
        if not req.password:
            raise ConversionError("This PDF is password-protected. Enter the password and try again.")
        if not doc.authenticate(req.password):
            raise ConversionError("The password is incorrect.")
    if doc.page_count == 0:
        raise ConversionError("This PDF has no pages.")
    return doc


def _clean_copy(doc: pymupdf.Document) -> bytes:
    """Decrypted, repaired copy for pdfplumber (MuPDF repairs damage on open)."""
    return doc.tobytes(garbage=1, deflate=True, encryption=pymupdf.PDF_ENCRYPT_NONE)


def _map_blocks(blocks, clip: pymupdf.Rect, page_rect: pymupdf.Rect):
    """Map block bboxes from clip-relative to page-relative coordinates."""
    W, H = page_rect.width, page_rect.height
    for b in blocks:
        x0, y0, x1, y1 = b.bbox
        b.bbox = ((clip.x0 + x0 * clip.width) / W, (clip.y0 + y0 * clip.height) / H,
                  (clip.x0 + x1 * clip.width) / W, (clip.y0 + y1 * clip.height) / H)
    return blocks


def _image_regions_without_text(mu_page, pl_page) -> List[pymupdf.Rect]:
    """Large embedded images on a digital page that carry no text layer."""
    regions = []
    area = abs(mu_page.rect)
    try:
        infos = mu_page.get_image_info()
    except Exception:
        return regions
    for info in infos:
        r = pymupdf.Rect(info["bbox"]) & mu_page.rect
        if r.is_empty or abs(r) < 0.15 * area:
            continue
        if words_in_rect(pl_page, (r.x0, r.y0, r.x1, r.y1)) < 15:
            regions.append(r)
    return regions


def _embedded_pictures(mu_page, pl_page, skip) -> list:
    """Images in a digital PDF (logos, photos, QR codes), copied as pictures."""
    out = []
    area = abs(mu_page.rect)
    try:
        infos = mu_page.get_image_info()
    except Exception:
        return out
    for info in infos:
        r = pymupdf.Rect(info["bbox"]) & mu_page.rect
        if r.is_empty or abs(r) < 0.002 * area or any(abs(r & s) > 0.5 * abs(r) for s in skip):
            continue
        if words_in_rect(pl_page, (r.x0, r.y0, r.x1, r.y1)) >= 15:
            continue                                  # a background behind real text
        try:
            png = mu_page.get_pixmap(dpi=150, clip=r, alpha=False).tobytes("png")
        except Exception:
            continue
        out.append(((r.x0, r.y0, r.x1, r.y1), png))
    return out


def _exact_page(n: int, mu_page, pl_page, req: ConvertRequest, step=None) -> PageResult:
    """Collect positioned text for an exact copy of the page."""
    cls = classify_page(mu_page, force_ocr=req.force_ocr)
    result = PageResult(number=n, kind=cls.kind, width=mu_page.rect.width, height=mu_page.rect.height)
    if cls.kind == "empty":
        result.notes.append(cls.reason)
        return result
    if cls.kind == "digital":
        result.segments = digital_segments(pl_page, LATTICE_SETTINGS)
        result.fills = digital_fills(pl_page)
        boxes = [t.bbox for t in pl_page.find_tables(table_settings=LATTICE_SETTINGS)]
        result.rules = digital_rules(pl_page, boxes)
        ocr_regions = _image_regions_without_text(mu_page, pl_page)
        for region in ocr_regions:
            _, info = extract_scanned_page(mu_page, lang=req.ocr_lang, clip=region, step=step, exact=True)
            result.segments.extend(info.get("segments", []))
            result.fills.extend(info.get("bands", []))
            result.rules.extend(info.get("rules", []))
            result.pictures.extend(info.get("pictures", []))
            result.notes.append("OCR used on an embedded image")
            if info.get("mean_confidence") is not None:
                result.mean_confidence = info["mean_confidence"]
        result.pictures.extend(_embedded_pictures(mu_page, pl_page, ocr_regions))
    else:
        result.notes.append(cls.reason)
        _, info = extract_scanned_page(mu_page, lang=req.ocr_lang, step=step, exact=True)
        result.segments = info.get("segments", [])
        result.fills = info.get("bands", [])
        result.rules = info.get("rules", [])
        result.pictures = info.get("pictures", [])
        if info.get("page_size"):
            result.width, result.height = info["page_size"]
        result.mean_confidence = info.get("mean_confidence")
        if info.get("rotated"):
            result.notes.append(f"rotated {info['rotated']}°")
        if info.get("deskewed"):
            result.notes.append(f"straightened {info['deskewed']}°")
        if result.mean_confidence is not None and result.mean_confidence < 60:
            result.notes.append("poor scan quality: check the output carefully")
    if not result.segments:
        result.notes.append("no readable content")
    return result


def _process_page(n: int, mu_page, pl_page, req: ConvertRequest, step=None) -> PageResult:
    cls = classify_page(mu_page, force_ocr=req.force_ocr)
    result = PageResult(number=n, kind=cls.kind)
    if cls.kind == "empty":
        result.notes.append(cls.reason)
        return result

    if cls.kind == "digital":
        result.blocks = extract_digital_page(pl_page)
        # mixed page: a scanned table pasted into a digital document
        for region in _image_regions_without_text(mu_page, pl_page):
            blocks, info = extract_scanned_page(mu_page, lang=req.ocr_lang, clip=region, step=step)
            if blocks:
                result.blocks.extend(_map_blocks(blocks, region, mu_page.rect))
                result.notes.append("OCR used on an embedded image")
                if info.get("mean_confidence") is not None:
                    result.mean_confidence = info["mean_confidence"]
        result.blocks.sort(key=lambda b: b.bbox[1])
    else:
        result.notes.append(cls.reason)
        blocks, info = extract_scanned_page(mu_page, lang=req.ocr_lang, step=step)
        result.blocks = blocks
        result.mean_confidence = info.get("mean_confidence")
        if info.get("rotated"):
            result.notes.append(f"rotated {info['rotated']}°")
        if info.get("deskewed"):
            result.notes.append(f"straightened {info['deskewed']}°")
        if result.mean_confidence is not None and result.mean_confidence < 60:
            result.notes.append("poor scan quality: check the output carefully")

    for b in result.blocks:
        if isinstance(b, Table):
            b.page = n
            b.normalise()
    result.blocks = [b for b in result.blocks
                     if not (isinstance(b, Table) and (not b.rows or b.n_cols < 1))]
    if not result.blocks:
        result.notes.append("no readable content")
    return result


def _finish_exact(req: ConvertRequest, pages: List[PageResult], report) -> ConvertResult:
    import csv
    import json
    import zipfile

    report(0.95, "Laying out the pages")
    sheets = build_exact_sheets(pages, single=req.options.layout == "exact_single")
    opts = req.options
    if req.output == "csv":
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for sh in sheets:
                out = io.StringIO()
                csv.writer(out).writerows(grid_rows(sh))
                zf.writestr(sh.name + ".csv", "\ufeff" + out.getvalue())
        data, ext, mt = buf.getvalue(), "zip", "application/zip"
    elif req.output == "json":
        doc = {"source": req.filename, "pages": [{"page": p.number, "type": p.kind, "notes": p.notes}
                                                 for p in pages],
               "sheets": [{"name": sh.name, "rows": grid_rows(sh)} for sh in sheets]}
        data, ext, mt = json.dumps(doc, ensure_ascii=False, indent=2).encode(), "json", "application/json"
    else:
        data = write_exact_xlsx(sheets, opts.convert_types, opts.date_order)
        ext, mt = "xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    summary = {
        "mode": "exact",
        "pages": len(pages),
        "digital_pages": sum(p.kind == "digital" for p in pages),
        "scanned_pages": sum(p.kind == "scanned" for p in pages),
        "empty_pages": sum(p.kind in ("empty", "error") for p in pages),
        "tables": 0,
        "transactions": None,
        "page_details": [{"page": p.number, "type": p.kind, "tables": 0,
                          "confidence": p.mean_confidence, "notes": p.notes} for p in pages],
    }
    report(1.0, "Done")
    return ConvertResult(data, ext, mt, pages, exact_preview(sheets), summary)


def convert(req: ConvertRequest, progress: Optional[ProgressFn] = None) -> ConvertResult:
    report = progress or (lambda p, m: None)
    report(0.02, "Opening file")
    doc = _open_document(req)
    try:
        plumber = pdfplumber.open(io.BytesIO(_clean_copy(doc)))
    except Exception as e:  # noqa: BLE001
        raise ConversionError(f"This PDF is damaged and could not be repaired ({e}).") from e

    numbers = req.pages or list(range(1, doc.page_count + 1))
    numbers = [n for n in numbers if 1 <= n <= doc.page_count]
    if not numbers:
        raise ConversionError("None of the selected pages exist in this PDF.")
    if len(numbers) > req.max_pages:
        raise ConversionError(
            f"This PDF has {len(numbers)} pages; the limit is {req.max_pages}. "
            "Select a page range and try again.")

    pages: List[PageResult] = []
    exact = req.options.layout in ("exact", "exact_single")
    try:
        for i, n in enumerate(numbers):
            base, share = 0.05 + 0.85 * i / len(numbers), 0.85 / len(numbers)
            label = f"Page {n} of {doc.page_count}"
            report(base, f"Reading {label.lower()}")

            def step(frac, msg, base=base, share=share, label=label):
                report(base + share * frac, f"{label}: {msg}")
            try:
                handler = _exact_page if exact else _process_page
                pages.append(handler(n, doc[n - 1], plumber.pages[n - 1], req, step))
            except Exception as e:  # noqa: BLE001 - one bad page must not kill the job
                log.exception("page %s failed", n)
                pages.append(PageResult(number=n, kind="error",
                                        notes=[f"this page could not be read: {e}"]))
    finally:
        plumber.close()
        doc.close()

    if exact:
        return _finish_exact(req, pages, report)

    report(0.92, "Joining tables across pages")
    remove_furniture(pages)
    stitch(pages)

    report(0.95, "Writing spreadsheet")
    meta = {"filename": req.filename or os.path.basename(req.path)}
    sheets = build_sheets(pages, req.options, meta)
    if req.output == "csv":
        data, ext, mt = write_csv_zip(sheets), "zip", "application/zip"
    elif req.output == "json":
        data, ext, mt = write_json(sheets, pages, meta), "json", "application/json"
    else:
        data, ext = write_xlsx(sheets, pages, meta), "xlsx"
        mt = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    summary = {
        "pages": len(pages),
        "digital_pages": sum(p.kind == "digital" for p in pages),
        "scanned_pages": sum(p.kind == "scanned" for p in pages),
        "empty_pages": sum(p.kind in ("empty", "error") for p in pages),
        "tables": sum(len(p.tables) for p in pages),
        "transactions": meta.get("transactions"),
        "page_details": [{"page": p.number, "type": p.kind, "tables": len(p.tables),
                          "confidence": p.mean_confidence, "notes": p.notes} for p in pages],
    }
    report(1.0, "Done")
    return ConvertResult(data, ext, mt, pages, preview(sheets), summary)
