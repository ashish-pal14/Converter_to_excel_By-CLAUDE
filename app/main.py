"""HTTP API and web UI.

    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import ocr_languages, settings
from .converter import ConversionError, ConvertRequest, Options, convert
from .jobs import JobManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = os.path.join(os.path.dirname(__file__), "static")

SIGNATURES = {
    b"%PDF": ".pdf",
    b"\x89PNG": ".png",
    b"\xff\xd8\xff": ".jpg",
    b"II*\x00": ".tif",
    b"MM\x00*": ".tif",
    b"BM": ".bmp",
    b"GIF8": ".gif",
}

manager: Optional[JobManager] = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global manager
    manager = JobManager(settings.data_dir, settings.workers)
    yield
    manager.shutdown()


app = FastAPI(title="PDF to Excel", version="1.0.0", lifespan=lifespan)
if settings.cors_origins:
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                       allow_methods=["*"], allow_headers=["*"])


def require_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    if settings.api_key and x_api_key != settings.api_key:
        raise HTTPException(401, "Missing or invalid API key (send it in the X-API-Key header).")


# --------------------------------------------------------------------------- helpers
def parse_pages(spec: Optional[str]) -> Optional[List[int]]:
    """'1-3, 5, 8-' style page selections. Returns None for all pages."""
    if not spec or not spec.strip():
        return None
    pages: List[int] = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d*))?", part)
        if not m:
            raise HTTPException(422, f"Page range '{part}' is not valid. Use a format like 1-3, 5.")
        a = int(m.group(1))
        b = m.group(2)
        if b is None:
            pages.append(a)
        else:
            end = int(b) if b else 10_000
            pages.extend(range(a, min(end, a + 10_000) + 1))
    return sorted(set(p for p in pages if p >= 1)) or None


def detect_type(head: bytes) -> Optional[str]:
    if b"%PDF" in head[:1024]:          # some PDFs have junk before the header
        return ".pdf"
    for sig, ext in SIGNATURES.items():
        if head.startswith(sig):
            return ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return None


async def save_upload(upload: UploadFile, folder: str) -> str:
    """Stream the upload to disk, enforcing the size limit and checking the type."""
    limit = settings.max_file_mb * 1024 * 1024
    head = await upload.read(2048)
    ext = detect_type(head)
    if ext is None:
        raise HTTPException(415, "This file type is not supported. Upload a PDF or an image (PNG, JPG, TIFF).")
    path = os.path.join(folder, "input" + ext)
    size = len(head)
    with open(path, "wb") as f:
        f.write(head)
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                f.close()
                os.remove(path)
                raise HTTPException(413, f"The file is larger than {settings.max_file_mb} MB.")
            f.write(chunk)
    if size == 0:
        raise HTTPException(400, "The uploaded file is empty.")
    return path


def job_options(output: str, layout: str, date_order: str, decimal: str, force_ocr: bool,
                ocr_lang: str, pages: Optional[str], convert_types: bool, include_text: bool) -> dict:
    if output not in ("xlsx", "csv", "json"):
        raise HTTPException(422, "Output must be xlsx, csv or json.")
    if layout not in ("exact", "exact_single", "tables", "pages", "single"):
        raise HTTPException(422, "Layout must be exact, exact_single, tables, pages or single.")
    if date_order not in ("DMY", "MDY"):
        raise HTTPException(422, "Date order must be DMY or MDY.")
    if decimal not in ("auto", ".", ","):
        raise HTTPException(422, "Decimal separator must be auto, '.' or ','.")
    langs = ocr_languages()
    for lang in ocr_lang.split("+"):
        if lang not in langs:
            raise HTTPException(422, f"OCR language '{lang}' is not installed. Available: {', '.join(langs)}.")
    return dict(output=output, layout=layout, date_order=date_order, decimal=decimal,
                force_ocr=force_ocr, ocr_lang=ocr_lang, pages=parse_pages(pages),
                convert_types=convert_types, include_text=include_text)


# --------------------------------------------------------------------------- API
@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/config")
def config():
    return {
        "max_file_mb": settings.max_file_mb,
        "max_pages": settings.max_pages,
        "ocr_languages": ocr_languages(),
        "job_ttl_minutes": settings.job_ttl_minutes,
        "api_key_required": bool(settings.api_key),
    }


@app.post("/api/jobs", dependencies=[Depends(require_key)], status_code=202)
async def create_job(
    file: UploadFile = File(...),
    output: str = Form("xlsx"),
    layout: str = Form("exact"),
    date_order: str = Form("DMY"),
    decimal: str = Form("auto"),
    force_ocr: bool = Form(False),
    ocr_lang: str = Form(settings.default_ocr_lang),
    pages: Optional[str] = Form(None),
    password: Optional[str] = Form(None),
    convert_types: bool = Form(True),
    include_text: bool = Form(True),
):
    opts = job_options(output, layout, date_order, decimal, force_ocr, ocr_lang, pages,
                       convert_types, include_text)
    name = os.path.basename(file.filename or "document.pdf")
    job = manager.create(name, opts)
    try:
        path = await save_upload(file, manager.folder(job.id))
    except HTTPException:
        manager.delete(job.id)
        raise
    manager.submit(job, path, password or None)
    return job.public()


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_key)])
def job_status(job_id: str):
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "This conversion was not found. It may have expired.")
    return job.public()


@app.get("/api/jobs/{job_id}/preview", dependencies=[Depends(require_key)])
def job_preview(job_id: str):
    job = manager.get(job_id)
    if not job or job.id not in manager.previews:
        raise HTTPException(404, "No preview is available for this conversion.")
    return {"sheets": manager.previews[job.id], "summary": job.summary}


@app.get("/api/jobs/{job_id}/download", dependencies=[Depends(require_key)])
def job_download(job_id: str):
    job = manager.get(job_id)
    if not job or job.status != "done":
        raise HTTPException(404, "The converted file is not available.")
    path = manager.output_path(job)
    if not path or not os.path.exists(path):
        raise HTTPException(410, "The converted file has expired. Convert the PDF again.")
    return FileResponse(path, media_type=job.media_type, filename=job.output_name)


@app.delete("/api/jobs/{job_id}", dependencies=[Depends(require_key)], status_code=204)
def job_delete(job_id: str):
    manager.delete(job_id)
    return Response(status_code=204)


@app.post("/api/convert", dependencies=[Depends(require_key)])
async def convert_now(
    file: UploadFile = File(...),
    output: str = Form("xlsx"),
    layout: str = Form("exact"),
    date_order: str = Form("DMY"),
    decimal: str = Form("auto"),
    force_ocr: bool = Form(False),
    ocr_lang: str = Form(settings.default_ocr_lang),
    pages: Optional[str] = Form(None),
    password: Optional[str] = Form(None),
    convert_types: bool = Form(True),
):
    """One-shot conversion for scripts: upload a file, get the spreadsheet back."""
    opts = job_options(output, layout, date_order, decimal, force_ocr, ocr_lang, pages, convert_types, True)
    with tempfile.TemporaryDirectory() as tmp:
        path = await save_upload(file, tmp)
        name = os.path.basename(file.filename or "document.pdf")
        req = ConvertRequest(
            path=path, filename=name, output=opts["output"], password=password or None,
            force_ocr=opts["force_ocr"], ocr_lang=opts["ocr_lang"], pages=opts["pages"],
            max_pages=settings.max_pages,
            options=Options(layout=opts["layout"], date_order=opts["date_order"],
                            decimal=opts["decimal"], convert_types=opts["convert_types"]))
        try:
            result = await run_in_threadpool(convert, req)
        except ConversionError as e:
            raise HTTPException(422, str(e))
    base = os.path.splitext(name)[0] or "converted"
    return Response(result.data, media_type=result.media_type, headers={
        "Content-Disposition": f'attachment; filename="{base}.{result.extension}"'})


@app.exception_handler(HTTPException)
async def http_error(_, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


# --------------------------------------------------------------------------- UI
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))
