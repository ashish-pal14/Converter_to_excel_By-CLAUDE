"""Convert PDFs from the command line, without the web server.

Examples
    python cli.py statement.pdf
    python cli.py scans/*.pdf --out converted/ --layout pages
    python cli.py locked.pdf --password secret --format csv
    python cli.py invoice.pdf --pages 1-2 --ocr-lang eng+deu --force-ocr
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from app.converter import ConversionError, ConvertRequest, Options, convert
from app.main import parse_pages


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Convert PDFs (digital or scanned) to Excel, CSV or JSON.")
    ap.add_argument("files", nargs="+", help="PDF or image files")
    ap.add_argument("--out", default=".", help="output folder (default: current folder)")
    ap.add_argument("--format", choices=["xlsx", "csv", "json"], default="xlsx")
    ap.add_argument("--layout", choices=["exact", "exact_single", "tables", "pages", "single"],
                    default="exact",
                    help="exact: copy of each page (default); exact_single: all pages on one sheet; "
                         "tables: one sheet per table plus Transactions for statements")
    ap.add_argument("--as-text", action="store_true",
                    help="keep numbers and dates as text instead of real Excel values")
    ap.add_argument("--date-order", choices=["DMY", "MDY"], default="DMY")
    ap.add_argument("--decimal", choices=["auto", ".", ","], default="auto")
    ap.add_argument("--pages", help="page selection, e.g. 1-3,7")
    ap.add_argument("--password")
    ap.add_argument("--ocr-lang", default="eng", help="Tesseract language(s), e.g. eng or eng+fra")
    ap.add_argument("--force-ocr", action="store_true", help="read every page as a scan")
    ap.add_argument("--no-text", action="store_true", help="leave out text outside tables")
    args = ap.parse_args(argv)

    os.makedirs(args.out, exist_ok=True)
    failures = 0
    for path in args.files:
        name = os.path.basename(path)
        start = time.time()

        def progress(p, msg, name=name):
            sys.stderr.write(f"\r{name}: {int(p * 100):3d}% {msg[:60]:<60}")
            sys.stderr.flush()

        try:
            req = ConvertRequest(
                path=path, filename=name, output=args.format, password=args.password,
                force_ocr=args.force_ocr, ocr_lang=args.ocr_lang,
                pages=parse_pages(args.pages) if args.pages else None, max_pages=10_000,
                options=Options(layout=args.layout, date_order=args.date_order,
                                decimal=args.decimal, include_text=not args.no_text,
                                convert_types=not args.as_text))
            result = convert(req, progress)
        except ConversionError as e:
            sys.stderr.write(f"\r{name}: failed - {e}\n")
            failures += 1
            continue
        out = os.path.join(args.out, os.path.splitext(name)[0] + "." + result.extension)
        with open(out, "wb") as f:
            f.write(result.data)
        s = result.summary
        what = "copied" if s.get("mode") == "exact" else f"{s['tables']} tables"
        sys.stderr.write(f"\r{name}: {s['pages']} pages {what}, {s['scanned_pages']} scanned "
                         f"-> {out} ({time.time() - start:.1f}s)\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
