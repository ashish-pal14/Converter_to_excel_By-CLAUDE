"""Settings, read from environment variables (see .env.example)."""
from __future__ import annotations

import os
import subprocess
from functools import lru_cache


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


class Settings:
    data_dir: str = os.getenv("DATA_DIR", os.path.join(os.getcwd(), "data"))
    max_file_mb: int = _int("MAX_FILE_MB", 50)
    max_pages: int = _int("MAX_PAGES", 300)
    workers: int = _int("WORKERS", max(1, (os.cpu_count() or 2) - 1))
    job_ttl_minutes: int = _int("JOB_TTL_MINUTES", 60)
    api_key: str = os.getenv("API_KEY", "")          # empty = no key required
    default_ocr_lang: str = os.getenv("OCR_LANG", "eng")
    cors_origins: list = [o for o in os.getenv("CORS_ORIGINS", "").split(",") if o]


settings = Settings()


@lru_cache(maxsize=1)
def ocr_languages() -> list:
    """Languages installed for Tesseract (e.g. eng, deu, fra, ara, hin)."""
    try:
        out = subprocess.run(["tesseract", "--list-langs"], capture_output=True,
                             text=True, timeout=10).stdout
        langs = [ln.strip() for ln in out.splitlines()[1:] if ln.strip()]
        return [lang for lang in langs if lang != "osd"] or ["eng"]
    except Exception:
        return ["eng"]
