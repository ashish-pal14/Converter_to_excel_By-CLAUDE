"""Background conversion jobs.

Jobs run in a thread pool. OCR work happens in Tesseract subprocesses and
OpenCV (both release the GIL), so threads use all CPU cores well. Each job
lives in its own folder and is deleted automatically after JOB_TTL_MINUTES.

To scale beyond one machine, replace JobManager.submit() with a Celery/RQ
task that calls run_job(); the API does not need to change.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Dict, Optional

from .config import settings
from .converter import ConversionError, ConvertRequest, Options, convert

log = logging.getLogger(__name__)


@dataclass
class Job:
    id: str
    filename: str
    status: str = "queued"          # queued | running | done | failed
    progress: float = 0.0
    message: str = "Waiting to start"
    error: Optional[str] = None
    created: float = field(default_factory=time.time)
    finished: Optional[float] = None
    output_name: Optional[str] = None
    media_type: Optional[str] = None
    summary: Optional[dict] = None
    settings: dict = field(default_factory=dict)

    def public(self) -> dict:
        d = asdict(self)
        d.pop("settings", None)
        d["expires_in_minutes"] = max(
            0, int((self.created + settings.job_ttl_minutes * 60 - time.time()) / 60))
        return d


class JobManager:
    def __init__(self, root: str, workers: int):
        self.root = os.path.join(root, "jobs")
        os.makedirs(self.root, exist_ok=True)
        self.jobs: Dict[str, Job] = {}
        self.previews: Dict[str, list] = {}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="convert")
        self._stop = threading.Event()
        threading.Thread(target=self._janitor, daemon=True).start()

    # ----------------------------------------------------------------- paths
    def folder(self, job_id: str) -> str:
        return os.path.join(self.root, job_id)

    def input_path(self, job_id: str, ext: str) -> str:
        return os.path.join(self.folder(job_id), "input" + ext)

    # ----------------------------------------------------------------- lifecycle
    def create(self, filename: str, opts: dict) -> Job:
        job = Job(id=uuid.uuid4().hex, filename=filename, settings=opts)
        os.makedirs(self.folder(job.id), exist_ok=True)
        with self.lock:
            self.jobs[job.id] = job
        return job

    def submit(self, job: Job, input_path: str, password: Optional[str]) -> None:
        # the password is only held in memory for the duration of the job
        self.pool.submit(self._run, job, input_path, password)

    def get(self, job_id: str) -> Optional[Job]:
        with self.lock:
            return self.jobs.get(job_id)

    def delete(self, job_id: str) -> None:
        with self.lock:
            self.jobs.pop(job_id, None)
            self.previews.pop(job_id, None)
        shutil.rmtree(self.folder(job_id), ignore_errors=True)

    def output_path(self, job: Job) -> Optional[str]:
        if not job.output_name:
            return None
        return os.path.join(self.folder(job.id), job.output_name)

    # ----------------------------------------------------------------- worker
    def _run(self, job: Job, input_path: str, password: Optional[str]) -> None:
        job.status, job.message = "running", "Starting"

        def progress(p: float, msg: str) -> None:
            job.progress, job.message = round(p, 3), msg

        s = job.settings
        try:
            req = ConvertRequest(
                path=input_path,
                filename=job.filename,
                output=s.get("output", "xlsx"),
                password=password,
                force_ocr=s.get("force_ocr", False),
                ocr_lang=s.get("ocr_lang", settings.default_ocr_lang),
                pages=s.get("pages"),
                max_pages=settings.max_pages,
                options=Options(
                    layout=s.get("layout", "tables"),
                    date_order=s.get("date_order", "DMY"),
                    decimal=s.get("decimal", "auto"),
                    convert_types=s.get("convert_types", True),
                    include_text=s.get("include_text", True),
                ),
            )
            result = convert(req, progress)
            base = os.path.splitext(job.filename)[0] or "converted"
            job.output_name = f"{base}.{result.extension}"
            with open(self.output_path(job), "wb") as f:
                f.write(result.data)
            job.media_type = result.media_type
            job.summary = result.summary
            with self.lock:
                self.previews[job.id] = result.preview
            job.status, job.progress, job.message = "done", 1.0, "Ready to download"
        except ConversionError as e:
            job.status, job.error, job.message = "failed", str(e), "Conversion failed"
        except Exception as e:  # noqa: BLE001
            log.exception("job %s crashed", job.id)
            job.status, job.message = "failed", "Conversion failed"
            job.error = f"Unexpected error while converting this file ({type(e).__name__})."
        finally:
            job.finished = time.time()
            try:
                os.remove(input_path)       # keep the uploaded PDF no longer than needed
            except OSError:
                pass
            with open(os.path.join(self.folder(job.id), "job.json"), "w") as f:
                json.dump(job.public(), f)

    # ----------------------------------------------------------------- cleanup
    def _janitor(self) -> None:
        while not self._stop.wait(60):
            self.cleanup()

    def cleanup(self) -> None:
        cutoff = time.time() - settings.job_ttl_minutes * 60
        with self.lock:
            expired = [j.id for j in self.jobs.values() if j.created < cutoff and j.status != "running"]
        for job_id in expired:
            self.delete(job_id)
        # folders left over from a previous run of the server
        for name in os.listdir(self.root):
            path = os.path.join(self.root, name)
            if name not in self.jobs and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)

    def shutdown(self) -> None:
        self._stop.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
