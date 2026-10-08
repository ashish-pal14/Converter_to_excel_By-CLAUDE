FROM python:3.12-slim

# Tesseract OCR + language packs. Add more with --build-arg, e.g.
#   docker build --build-arg OCR_LANGS="eng deu fra ara hin" .
ARG OCR_LANGS="eng"
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr libglib2.0-0 \
      $(for l in $OCR_LANGS; do echo tesseract-ocr-$(echo $l | tr '_' '-'); done) \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    DATA_DIR=/data OMP_THREAD_LIMIT=1

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY cli.py .

RUN useradd --create-home --uid 1000 appuser && mkdir -p /data && chown appuser /data
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/api/health')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
