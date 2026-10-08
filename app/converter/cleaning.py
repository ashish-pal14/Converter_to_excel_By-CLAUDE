"""Turn extracted strings into real Excel values (numbers, dates, percentages).

Without this step users get spreadsheets where every number is text and
SUM() returns 0.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, Optional, Tuple

CURRENCY = "$€£¥₹₩₽₺₫฿₱₦₴₪"
CURRENCY_CODES = r"(?:USD|EUR|GBP|INR|JPY|CNY|AUD|CAD|CHF|SGD|AED|SAR|ZAR|[Rr][Ss]\s*\.?|Rs\s*\.?)"

_num_us = re.compile(r"^\d{1,3}(,\d{3})+(\.\d+)?$|^\d+(\.\d+)?$|^\.\d+$")
_num_eu = re.compile(r"^\d{1,3}(\.\d{3})+(,\d+)?$|^\d+,\d+$")
_num_space = re.compile(r"^\d{1,3}( \d{3})+([.,]\d+)?$")
_ocr_fix = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "|": "1", "S": "5", "B": "8"})

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
MONTHS.update({"sept": 9})


def _to_float(core: str, decimal: str) -> Optional[float]:
    if decimal == "," or (decimal == "auto" and _num_eu.match(core) and not _num_us.match(core)):
        if _num_eu.match(core) or re.match(r"^\d+$", core):
            return float(core.replace(".", "").replace(",", "."))
        return None
    if _num_us.match(core):
        return float(core.replace(",", ""))
    if _num_space.match(core):
        tail = core.replace(" ", "")
        return float(tail.replace(",", ".")) if "," in tail else float(tail)
    return None


def parse_number(s: str, decimal: str = "auto", ocr: bool = False) -> Optional[Tuple[float, str]]:
    """Return (value, excel_number_format) or None if s is not a number."""
    t = s.strip()
    if ocr:
        t = t.rstrip(".,;:'`")      # OCR often reads specks after a number as punctuation
    if not t or len(t) > 40:
        return None
    neg = False
    pct = False
    fmt_currency = ""

    if t.startswith("(") and t.endswith(")"):
        neg, t = True, t[1:-1].strip()
    if t.endswith(("CR", "Cr")):          # bank statements: credit marker
        t = t[:-2].strip()
    elif t.endswith(("DR", "Dr")):
        neg, t = True, t[:-2].strip()
    if t.endswith("-") and len(t) > 1:
        neg, t = True, t[:-1].strip()
    if t.startswith("-") or t.startswith("−"):
        neg, t = (not neg), t[1:].strip()
    elif t.startswith("+"):
        t = t[1:].strip()
    if t.endswith("%"):
        pct, t = True, t[:-1].strip()

    m = re.match(rf"^([{CURRENCY}]|{CURRENCY_CODES})\s*(.*)$", t)
    if m:
        fmt_currency, t = m.group(1), m.group(2).strip()
    m = re.match(rf"^(.*?)\s*([{CURRENCY}]|{CURRENCY_CODES})$", t)
    if m and not fmt_currency:
        t, fmt_currency = m.group(1).strip(), m.group(2)
    if t.startswith("-"):
        neg, t = (not neg), t[1:].strip()

    if not t or not any(ch.isdigit() for ch in t):
        return None
    # leading zeros = an identifier, keep as text (00123, 0042)
    if re.match(r"^0\d", t) and "." not in t and "," not in t:
        return None
    # long digit strings = account / card / phone numbers: Excel would round them
    if len(re.sub(r"\D", "", t)) > 15:
        return None

    value = _to_float(t, decimal)
    if value is None and ocr:
        fixed = t.translate(_ocr_fix)
        digit_share = sum(ch.isdigit() for ch in t) / max(len(t), 1)
        if fixed != t and digit_share >= 0.6:
            value = _to_float(fixed, decimal)
    if value is None:
        return None

    if neg:
        value = -value
    decimals = 0
    m = re.search(r"[.,](\d+)$", t)
    if m and not (decimal != "," and _num_us.match(t) and "," in t and "." not in t):
        decimals = min(len(m.group(1)), 6)
    if decimal == "auto" and re.match(r"^\d{1,3}(,\d{3})+$", t):
        decimals = 0

    base = "#,##0" + ("." + "0" * decimals if decimals else "")
    if pct:
        return value / 100.0, ("0" + ("." + "0" * decimals if decimals else "") + "%")
    if fmt_currency:
        sym = fmt_currency if fmt_currency in CURRENCY else ""
        if sym:
            return value, f'"{sym}"{base};-"{sym}"{base}'
    return value, base


_date_patterns = [
    (re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$"), "YMD"),
    (re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}|\d{2})$"), "AMBIG"),
    (re.compile(r"^(\d{1,2})[\s\-/.]+([A-Za-z]{3,9})[\s\-/.,']+(\d{4}|\d{2})$"), "D_MON_Y"),
    (re.compile(r"^([A-Za-z]{3,9})[\s\-/.]+(\d{1,2})(?:st|nd|rd|th)?,?[\s\-/.]+(\d{4})$"), "MON_D_Y"),
]


def _year(y: str) -> int:
    v = int(y)
    if len(y) == 2:
        v += 2000 if v < 70 else 1900
    return v


def parse_date(s: str, order: str = "DMY") -> Optional[dt.date]:
    t = s.strip().rstrip(".")
    if len(t) < 6 or len(t) > 20:
        return None
    for rx, kind in _date_patterns:
        m = rx.match(t)
        if not m:
            continue
        try:
            if kind == "YMD":
                return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if kind == "AMBIG":
                a, b, y = int(m.group(1)), int(m.group(2)), _year(m.group(3))
                if a > 12:
                    return dt.date(y, b, a)
                if b > 12:
                    return dt.date(y, a, b)
                return dt.date(y, b, a) if order == "DMY" else dt.date(y, a, b)
            if kind == "D_MON_Y":
                mon = MONTHS.get(m.group(2)[:4].lower()) or MONTHS.get(m.group(2)[:3].lower())
                if mon:
                    return dt.date(_year(m.group(3)), mon, int(m.group(1)))
            if kind == "MON_D_Y":
                mon = MONTHS.get(m.group(1)[:4].lower()) or MONTHS.get(m.group(1)[:3].lower())
                if mon:
                    return dt.date(int(m.group(3)), mon, int(m.group(2)))
        except ValueError:
            return None
    return None


_time_colon = re.compile(
    r"(?P<h>\d{1,2})\s*[:.]\s*(?P<m>\d{2})(?:\s*[:.]\s*(?P<s>\d{2}))?\s*(?P<ampm>[AaPp]\.?\s?[Mm]\.?)?$")
_time_space = re.compile(r"(?P<h>\d{1,2})\s+(?P<m>\d{2})\s*(?P<ampm>[AaPp]\.?\s?[Mm]\.?)$")


def parse_datetime(s: str, order: str = "DMY") -> Optional[dt.datetime]:
    """'06 Jul 26 03:04 pm', '06/07/2026 15:04', '2026-07-06 15:04:10'.

    OCR sometimes drops the colon ('03 04 pm'); that form is accepted only
    with am/pm so a plain '26 12' is never mistaken for a time.
    """
    t = s.strip()
    if len(t) > 32:
        return None
    for rx in (_time_colon, _time_space):
        for m in rx.finditer(t):
            if m.end() != len(t):
                continue
            date_part = t[:m.start()].strip(" ,T:")
            d = parse_date(date_part, order)
            if d is None:
                continue
            h, mi = int(m.group("h")), int(m.group("m"))
            sec = int(m.groupdict().get("s") or 0)
            ampm = (m.group("ampm") or "").lower().replace(".", "").replace(" ", "")
            if ampm:
                if not 1 <= h <= 12:
                    continue
                h = (h % 12) + (12 if ampm == "pm" else 0)
            if h > 23 or mi > 59 or sec > 59:
                continue
            return dt.datetime(d.year, d.month, d.day, h, mi, sec)
    return None


def clean_text(s: str) -> str:
    s = s.replace("\u00a0", " ").replace("\u200b", "")
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


def convert_value(s: Any, date_order: str = "DMY", decimal: str = "auto",
                  ocr: bool = False) -> Tuple[Any, Optional[str]]:
    """Return (value, number_format) for one cell."""
    if s is None:
        return "", None
    if not isinstance(s, str):
        return s, None
    t = clean_text(s)
    if not t:
        return "", None
    d = parse_date(t, date_order)
    if d is not None:
        return d, "yyyy-mm-dd"
    dtm = parse_datetime(t, date_order)
    if dtm is not None:
        return dtm, "yyyy-mm-dd hh:mm"
    n = parse_number(t, decimal=decimal, ocr=ocr)
    if n is not None:
        value, fmt = n
        if fmt == "#,##0" and abs(value) < 10000 and "," not in t and "." not in t:
            fmt = "0"   # plain integers like years or quantities: no separators
        if fmt == "0" and float(value).is_integer():
            value = int(value)
        return value, fmt
    return t, None


def looks_like_header(row) -> bool:
    cells = [c for c in row if str(c).strip()]
    if not cells:
        return False
    numeric = sum(1 for c in cells if parse_number(str(c)) is not None or parse_date(str(c)))
    return numeric == 0
