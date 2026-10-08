# PDF to Excel

A self-hosted web app that converts PDFs into spreadsheets. By default it makes an **exact copy**: each page becomes a sheet that looks like the page. It handles digital PDFs, scanned PDFs, mixed documents, photos of tables, password-protected and damaged files. Tables come out as real Excel numbers, dates and percentages, so formulas work straight away.

## Run it

### With Docker (recommended)

```bash
cp .env.example .env          # adjust limits if needed
docker compose up --build
```

Open http://localhost:8000.

To read scans in other languages, list them in `docker-compose.yml`:

```yaml
args:
  OCR_LANGS: "eng deu fra ara hin chi_sim"
```

Language codes are Tesseract's: `eng`, `deu`, `fra`, `spa`, `ita`, `por`, `nld`, `ara`, `hin`, `ben`, `tam`, `tel`, `urd`, `rus`, `tur`, `chi_sim`, `chi_tra`, `jpn`, `kor` and [many more](https://tesseract-ocr.github.io/tessdoc/Data-Files-in-different-versions.html). Installed languages appear automatically in the web page's language list.

### Without Docker

Install Tesseract first:

| System | Command |
|---|---|
| Ubuntu / Debian | `sudo apt install tesseract-ocr tesseract-ocr-eng` |
| macOS | `brew install tesseract` (add `tesseract-lang` for all languages) |
| Windows | Installer from https://github.com/UB-Mannheim/tesseract/wiki, then add it to `PATH` |

Then:

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

### Command line (no server)

```bash
python cli.py statement.pdf
python cli.py scans/*.pdf --out converted/ --layout exact_single
python cli.py statement.pdf --layout tables          # Transactions list + tables
python cli.py locked.pdf --password secret --format csv
python cli.py invoice.pdf --pages 1-2 --ocr-lang eng+deu --force-ocr
```

## Exact copy (default)

Each page becomes a sheet that mirrors the page:

- **Positions.** Every line is a row and every piece of text sits in the column matching its position on the page. Row heights and column widths come from the PDF's own measurements, and gridlines are hidden so the sheet reads like the page.
- **Formatting.** Font size, bold, text colour (for example green or teal credits), background bands (dark header bars, footers, shaded boxes), separator lines, and ruled-table borders and merged cells are all kept.
- **Pictures.** Logos, QR codes, photos and screenshots are copied in as pictures at their position, instead of being read as text.
- **Values.** Numbers and dates are real Excel values formatted to look as printed: `Rs. 1,000` shows as `Rs. 1,000` but adds up, `06 Jul 26` stays `06 Jul 26`, and `(1,234.00)` stays in brackets. Tick *Keep numbers and dates as text* (API: `convert_types=false`) for plain text instead.
- **Pages.** One sheet per page (`exact`), or all pages on one sheet with page breaks between them (`exact_single`).
- **Printing.** Sheets are set to A4 and fit to the page width.

The table-based modes are still available for analysis work: `tables` (one sheet per table, plus a Transactions list for statements), `pages` and `single`.

## What it handles

| Input | How |
|---|---|
| Digital PDF, ruled tables | pdfplumber line detection, merged cells kept as merged Excel cells |
| Digital PDF, borderless tables (bank statements, reports) | Columns found from vertical whitespace; wrapped text rejoined |
| Scanned PDF / photo / TIFF | Auto-rotate, deskew, contrast fix, OpenCV grid detection, Tesseract OCR |
| Mixed (digital page with a pasted-in scan) | Text read directly, image regions OCR'd |
| Tables across pages | Joined into one sheet, repeated header rows removed |
| Password-protected | Password field; never written to disk |
| Damaged PDFs | Repaired by MuPDF on open |
| Pages without tables | Text is still exported line by line |
| Text drawn as shapes ("Print to PDF") | Detected and read with OCR |
| Bank / card statements | Standard **Transactions** sheet with debit/credit detection and balance checks |

### Bank and credit-card statements (`tables` layout)

With the *Tables only* layout, when a file contains a transaction list, the first sheet is **Transactions**, in one standard layout whatever the bank:

`Date | Time | Description | (other columns) | Debit | Credit | Balance | Page`

followed by live `SUM` totals for debits and credits.

- **Debit vs. credit** is taken from, in order: separate Debit/Credit (Withdrawal/Deposit) columns, `Cr`/`Dr` markers, ink colour (many card statements show credits only in green or teal), then words in the description such as *Repayment*, *Refund*, *Reversal*, *Cashback* or *Salary*.
- **Multi-page lists** are joined into one list. Repeated page headers and footers are removed, keeping the first copy.
- **Dates with times** (`06 Jul 26 03:04 pm`, even with the time wrapped onto the next line) become a real date plus a time.
- **Running balance check.** When the statement has a balance column, every row is checked against the one before it, and rows that don't add up are highlighted. The result appears on the *About this file* sheet, for example "Running balance matches on 24 of 24 rows".
- **Nothing is silently dropped.** A dated row whose amount can't be read stays in the list, highlighted with a comment, and is left out of the totals so the difference is visible.

Compare the debit and credit totals with the summary printed on the statement. If they match, the conversion is complete.

### PDFs with no text layer

"Microsoft Print to PDF" and some report generators draw every letter as a vector shape. These PDFs look normal but contain no text, so they are rendered and read with OCR, the same way as scans. White-on-dark header bars are inverted before reading, coloured text is read in a colour-aware way, and QR codes are blanked so they don't turn into random letters.

### Data cleaning

Excel gets typed values, not text:

- `1,234.56`, `1.234,56`, `1 234 567`, `$1,000`, `€99.90`
- negatives as `(1,234.56)`, `-42`, `42-`, `150.00 DR`
- `12.5%` becomes 0.125 with a percent format
- dates such as `03/04/2024` (day-first or month-first, set per conversion), `2024-01-31`, `5 Jan 2024`, `March 7, 2023`

Values that look like identifiers stay as text, so they aren't corrupted. This covers leading-zero codes (`00123`) and digit strings over 15 characters (account and card numbers), which Excel would otherwise round.

### Quality signals

Cells read by OCR with low confidence are highlighted yellow in Excel, with a comment, and in the web preview. The last sheet, *About this file*, lists each page's type, its legibility score, and any corrections applied (rotation, straightening).

## API

All endpoints accept an `X-API-Key` header when `API_KEY` is set.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/jobs` | Upload a file, start a background conversion; returns a job |
| `GET` | `/api/jobs/{id}` | Status, progress (0–1), message, summary |
| `GET` | `/api/jobs/{id}/preview` | First rows of each sheet, for display |
| `GET` | `/api/jobs/{id}/download` | The converted file |
| `DELETE` | `/api/jobs/{id}` | Delete the job and its files now |
| `POST` | `/api/convert` | One-shot: upload and get the file back in the response |
| `GET` | `/api/config` | Limits and installed OCR languages |
| `GET` | `/docs` | Interactive API documentation |

Form fields for `POST /api/jobs` and `POST /api/convert`:

| Field | Values | Default |
|---|---|---|
| `file` | PDF, PNG, JPG, TIFF, BMP, WEBP, GIF | required |
| `output` | `xlsx`, `csv` (zip of one CSV per sheet), `json` | `xlsx` |
| `layout` | `exact` (copy of each page), `exact_single` (all pages on one sheet), `tables` (sheet per table + Transactions), `pages`, `single` | `exact` |
| `convert_types` | `false` keeps numbers and dates as text | `true` |
| `date_order` | `DMY`, `MDY` | `DMY` |
| `decimal` | `auto`, `.`, `,` | `auto` |
| `pages` | e.g. `1-3,7` or `5-` | all |
| `password` | text | none |
| `ocr_lang` | e.g. `eng`, `eng+fra` | `OCR_LANG` |
| `force_ocr` | `true` to treat every page as a scan | `false` |

```bash
curl -F file=@statement.pdf -F date_order=MDY -o statement.xlsx http://localhost:8000/api/convert
```

## Settings

Set these in `.env`:

| Variable | Default | Meaning |
|---|---|---|
| `MAX_FILE_MB` | 50 | Upload size limit |
| `MAX_PAGES` | 300 | Page limit per conversion |
| `WORKERS` | CPU cores − 1 | Conversions running in parallel |
| `JOB_TTL_MINUTES` | 60 | Uploads and results are deleted after this |
| `OCR_LANG` | `eng` | Default OCR language |
| `API_KEY` | empty | Require this key on API calls |
| `CORS_ORIGINS` | empty | Other websites allowed to call the API |

## How it works

```
upload ─► API (FastAPI) ─► job queue (thread pool) ─► per-page classifier
                                                         │
                         ┌───────────────────────────────┴──────────────────┐
                    digital page                                       scanned page
              pdfplumber ruled tables                    render at native DPI ► rotate ► deskew
              + whitespace layout engine                 grid detection ► erase lines ► OCR
              + OCR of embedded images                   cell re-read ► missed-word recovery
                         └───────────────────────────────┬──────────────────┘
                                   stitch tables across pages
                                   type conversion (numbers, dates, %)
                                   write xlsx / csv / json ─► preview + download
```

| File | Role |
|---|---|
| `app/converter/classifier.py` | Digital / scanned / empty, per page |
| `app/converter/digital.py` | Ruled tables with merged cells; text-layer words |
| `app/converter/layout.py` | Words → lines → cells → columns; table vs. text blocks |
| `app/converter/scanned.py` | Image cleanup, grid detection, OCR, cell assignment |
| `app/converter/stitching.py` | Multi-page tables |
| `app/converter/furniture.py` | Repeated page headers/footers |
| `app/converter/statement.py` | Transactions sheet: column roles, debit/credit, balance check |
| `app/converter/cleaning.py` | Text → typed values |
| `app/converter/writer.py` | Excel/CSV/JSON output for the table modes |
| `app/converter/exact.py` | Exact copy: rows, columns, formats, fills, lines, pictures |
| `app/converter/segments.py` | Positioned text, fills and lines for the exact copy |
| `app/converter/pipeline.py` | Orchestration, error handling, progress |
| `app/jobs.py` | Background jobs, progress, automatic deletion |
| `app/main.py` | HTTP API and upload validation |
| `app/static/` | Web interface |

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

The suite generates its own sample documents (`tests/make_samples.py`). These include digital ruled and borderless tables, a skewed noisy scan, a sideways scan, a photo of a table, a mixed page, a password-protected file, and a credit-card statement drawn entirely as vector shapes with teal credits and a dark header bar. Each scanned result is compared cell by cell with the digital original of the same page. At the time of writing, scanned accuracy was 98.9–100% on these samples.

Before going live, add 50–100 of your own real documents to the test set. Accuracy depends on what your users actually upload.

## Scaling and production notes

- **Throughput.** A digital page takes well under a second. A scanned page takes about 3–8 seconds on one CPU core. Raise `WORKERS` and give the container more cores. `OMP_THREAD_LIMIT=1` (set in the Dockerfile) stops Tesseract from oversubscribing cores when jobs run in parallel.
- **More than one server.** Jobs currently live in memory on one instance. To scale horizontally:
  1. Move job state to Redis.
  2. Store files in S3/MinIO.
  3. Run `JobManager._run` as a Celery or RQ task.

  The HTTP API doesn't need to change.
- **Harder scans.** Handwriting, very poor photos, or complex layouts may need a stronger engine. `extract_scanned_image()` in `scanned.py` is the single place to swap in PaddleOCR, or a cloud service such as AWS Textract, Azure Document Intelligence or Google Document AI, behind the same interface.
- **Security.** Run behind HTTPS (nginx, Caddy, or a cloud load balancer). Keep `JOB_TTL_MINUTES` short for sensitive documents, and set `API_KEY` if the API is reachable from the internet. Uploaded PDFs are deleted as soon as conversion finishes; results are deleted after the TTL.

## Known limits

- Handwriting is not read reliably by Tesseract.
- Tables drawn as images inside vector graphics, or as text inside charts, are not extracted as tables.
- Borderless tables with very uneven column alignment can split or merge columns. The *One sheet per page* layout keeps everything visible in that case.
- Very low-resolution scans (below ~150 DPI) lose accuracy. The highlighted cells show where to check.
