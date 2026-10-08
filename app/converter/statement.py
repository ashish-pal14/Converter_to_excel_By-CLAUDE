"""Turn statement tables into one clean Transactions list.

Banks lay statements out in many ways: one Amount column with Cr/Dr markers,
separate Debit and Credit columns, credits shown only in green, a running
balance or none. This module recognises the columns and produces a single
standard layout:

    Date | Time | Description | <other columns> | Debit | Credit | Balance | Page

Debit vs. credit is decided from, in order of trust:
  1. separate Debit / Credit (Withdrawal / Deposit) columns
  2. a Dr/Cr marker in the amount or in a type column
  3. ink colour (green/teal/blue = credit, red = debit) when the statement uses it
  4. keywords in the description (repayment, refund, reversal, salary, ...)

When a running balance exists, each row is checked against the previous one;
rows that don't add up are flagged, which catches most OCR misreads.
"""
from __future__ import annotations

import colorsys
import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .cleaning import convert_value, looks_like_header, parse_number
from .models import PageResult, Table

ROLE_WORDS = {
    "date": ["txn date", "transaction date", "tran date", "trans date", "posting date",
             "post date", "value date", "date"],
    "description": ["transaction details", "description", "narration", "particulars",
                    "details", "remarks", "merchant", "transaction"],
    "debit": ["withdrawal", "withdrawals", "debit", "debits", "paid out", "money out", "dr amount",
              "amount dr", "spent", "outflow"],
    "credit": ["deposit", "deposits", "credit", "credits", "paid in", "money in", "cr amount",
               "amount cr", "received", "inflow"],
    "balance": ["closing balance", "running balance", "available balance", "balance"],
    "type": ["dr/cr", "cr/dr", "dr / cr", "type", "txn type"],
    "amount": ["amount", "amt", "value", "inr", "rs"],
}
CREDIT_WORDS = re.compile(
    r"\b(repayment|refund|reversal|reversed|cashback|cash back|payment received|thank you|"
    r"credited|credit int|interest credit|salary|deposit|neft cr|imps cr|upi cr|by transfer|"
    r"by clearing|received from)\b", re.I)
DRCR = re.compile(r"\b(cr|dr)\.?$", re.I)


@dataclass
class Transactions:
    header: List[str]
    rows: List[List[object]] = field(default_factory=list)
    formats: List[List[Optional[str]]] = field(default_factory=list)
    flags: List[List[int]] = field(default_factory=list)
    comments: Dict[Tuple[int, int], str] = field(default_factory=dict)
    debit_col: int = -1
    credit_col: int = -1
    method: str = ""
    checks: List[str] = field(default_factory=list)
    unreadable: int = 0


# --------------------------------------------------------------------------- detection
def _header_rows(t: Table) -> int:
    n = 0
    while n < min(3, len(t.rows) - 1) and looks_like_header(t.rows[n]):
        n += 1
    return n


def _role_for(text: str) -> Optional[str]:
    h = " ".join(text.lower().replace("(", " ").replace(")", " ").split())
    if not h:
        return None
    for role in ("date", "balance", "debit", "credit", "type", "description", "amount"):
        for w in ROLE_WORDS[role]:
            if h == w or re.search(rf"\b{re.escape(w)}\b", h):
                return role
    return None


def _is_date(v) -> bool:
    return isinstance(v, (dt.date, dt.datetime))


def _column_stats(t: Table, start: int, date_order: str, ocr: bool):
    n_cols = t.n_cols
    data = t.rows[start:]
    date_hits, num_hits, filled = [0] * n_cols, [0] * n_cols, [0] * n_cols
    for row in data:
        for j in range(n_cols):
            cell = row[j] if j < len(row) else ""
            if not str(cell).strip():
                continue
            filled[j] += 1
            v, _ = convert_value(cell, date_order, ocr=ocr)
            if _is_date(v):
                date_hits[j] += 1
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                num_hits[j] += 1
            elif _amount(cell, ocr) is not None:
                num_hits[j] += 1
    return date_hits, num_hits, filled


def _amount(cell: str, ocr: bool) -> Optional[float]:
    t = str(cell).strip()
    if not t:
        return None
    t = DRCR.sub("", t).strip()
    p = parse_number(t, ocr=ocr)
    return None if p is None else p[0]


def _map_columns(t: Table, hdr: int, date_order: str):
    """Return {role: column} or None if this is not a transaction table."""
    ocr = t.source.startswith("ocr")
    data_rows = len(t.rows) - hdr
    if data_rows < 2 or t.n_cols < 2:
        return None
    date_hits, num_hits, filled = _column_stats(t, hdr, date_order, ocr)
    roles: Dict[str, int] = {}

    if hdr:
        header = [" ".join(str(t.rows[r][j]) for r in range(hdr) if j < len(t.rows[r]))
                  for j in range(t.n_cols)]
        for j, h in enumerate(header):
            role = _role_for(h)
            if role and role not in roles:
                roles[role] = j

    # dates: trust the data more than the header
    date_cols = [j for j in range(t.n_cols) if date_hits[j] >= 0.6 * max(filled[j], 1) and filled[j]]
    if "date" not in roles or roles["date"] not in date_cols:
        if not date_cols:
            return None
        roles["date"] = date_cols[0]
    date_rows = date_hits[roles["date"]]
    if date_rows < 0.6 * data_rows:
        return None
    if date_rows < 3:
        return None   # a summary box with a due date is not a transaction list

    numeric = [j for j in range(t.n_cols)
               if j != roles["date"] and filled[j] and num_hits[j] >= 0.6 * filled[j]
               and j not in date_cols]
    for role in ("debit", "credit", "balance", "amount"):
        if role in roles and roles[role] not in numeric:
            roles.pop(role)
    have_amount = any(r in roles for r in ("debit", "credit", "amount"))
    if not have_amount:
        free = [j for j in numeric if j not in roles.values()]
        if not free:
            return None
        if len(free) == 1:
            roles["amount"] = free[0]
        elif len(free) >= 3:
            roles["debit"], roles["credit"], roles["balance"] = free[-3], free[-2], free[-1]
        else:
            a, b = free[0], free[1]
            both = sum(1 for row in t.rows[hdr:] if str(row[a]).strip() and str(row[b]).strip())
            if both <= 0.1 * data_rows:      # never both filled: debit / credit pair
                roles["debit"], roles["credit"] = a, b
            else:
                roles["amount"], roles["balance"] = a, b
    if "balance" not in roles:
        free = [j for j in numeric if j not in roles.values()]
        if free and ("debit" in roles or "amount" in roles):
            roles["balance"] = free[-1]

    if "description" not in roles:
        text_cols = [j for j in range(t.n_cols) if j not in roles.values() and filled[j]]
        if text_cols:
            roles["description"] = max(
                text_cols, key=lambda j: sum(len(str(r[j])) for r in t.rows[hdr:] if j < len(r)))
    return roles


# --------------------------------------------------------------------------- direction
def _hue_kind(hex_color: Optional[str]) -> Optional[str]:
    if not hex_color:
        return None
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    if s < 0.25:
        return None
    deg = h * 360
    if deg < 25 or deg > 330:
        return "debit"            # red / crimson
    if 70 <= deg <= 250:
        return "credit"           # green, teal, blue
    return None


def _direction(amount_text: str, type_text: str, color: Optional[str], desc: str,
               color_means: bool) -> Tuple[str, str]:
    m = DRCR.search(amount_text.strip()) or DRCR.search(type_text.strip())
    if m:
        return ("credit" if m.group(1).lower() == "cr" else "debit"), "Cr/Dr marker"
    tt = type_text.strip().lower()
    if tt in ("c", "credit", "cr"):
        return "credit", "type column"
    if tt in ("d", "debit", "dr"):
        return "debit", "type column"
    if color_means:
        kind = _hue_kind(color)
        if kind:
            return kind, "text colour"
    if CREDIT_WORDS.search(desc):
        return "credit", "description"
    return "debit", "default"


# --------------------------------------------------------------------------- build
def build_transactions(pages: List[PageResult], date_order: str = "DMY") -> Optional[Transactions]:
    candidates = []
    for p in pages:
        for t in p.tables:
            hdr = _header_rows(t)
            roles = _map_columns(t, hdr, date_order)
            if roles:
                candidates.append((t, hdr, roles))
    if not candidates:
        return None

    has_time = False
    has_balance = any("balance" in r for _, _, r in candidates)
    extra_names: List[str] = []
    prepared = []
    for t, hdr, roles in candidates:
        used = set(roles.values())
        extras = [j for j in range(t.n_cols) if j not in used]
        names = []
        for j in extras:
            name = " ".join(str(t.rows[r][j]) for r in range(hdr)).strip() or f"Column {j + 1}"
            names.append(name)
            if name not in extra_names:
                extra_names.append(name)
        prepared.append((t, hdr, roles, list(zip(extras, names))))

    # does this statement mark direction by colour? (some amounts coloured, others not)
    amount_colors = []
    for t, hdr, roles, _ in prepared:
        col = roles.get("amount")
        if col is None or not t.colors:
            continue
        for i in range(hdr, len(t.rows)):
            if str(t.rows[i][col]).strip():
                amount_colors.append(_hue_kind(t.colors[i][col] if i < len(t.colors) else None))
    coloured = sum(1 for k in amount_colors if k)
    color_means = 0 < coloured < len(amount_colors)

    records = []
    for t, hdr, roles, extras in prepared:
        ocr = t.source.startswith("ocr")
        for i in range(hdr, len(t.rows)):
            row = t.rows[i]
            get = lambda role: str(row[roles[role]]).strip() if role in roles else ""  # noqa: E731
            dval, _ = convert_value(get("date"), date_order, ocr=ocr)
            if not _is_date(dval):
                continue                       # totals, sub-headings, notes
            time_text = ""
            if isinstance(dval, dt.datetime):
                has_time = True
                time_text = dval.strftime("%H:%M")
                dval = dval.date()
            desc = get("description")
            debit = credit = None
            how = ""
            unreadable = None     # amount text we could not turn into a number
            if "debit" in roles or "credit" in roles:
                debit = _amount(get("debit"), ocr) if "debit" in roles else None
                credit = _amount(get("credit"), ocr) if "credit" in roles else None
                how = "separate columns"
                if debit is None and get("debit"):
                    debit, unreadable = get("debit"), "debit"
                if credit is None and get("credit"):
                    credit, unreadable = get("credit"), "credit"
            elif "amount" in roles:
                raw = get("amount")
                amt = _amount(raw, ocr)
                if amt is None:
                    if not raw:
                        continue
                    # keep the row: dropping it would silently change the totals
                    records.append(dict(date=dval, time=time_text, desc=desc,
                                        extras={name: str(row[j]).strip() for j, name in extras},
                                        debit=raw, credit=None, balance=None, page=t.page_of_row(i),
                                        conf=0.0, how="", unreadable="debit"))
                    continue
                color = None
                if t.colors and i < len(t.colors):
                    color = t.colors[i][roles["amount"]]
                kind, how = _direction(raw, get("type"), color, desc, color_means)
                if kind == "credit":
                    credit = abs(amt)
                else:
                    debit = abs(amt)
            if debit is None and credit is None:
                continue
            balance = _amount(get("balance"), ocr) if "balance" in roles else None
            conf = 100.0
            if t.confidence and i < len(t.confidence):
                conf = min(t.confidence[i][j] for j in roles.values() if j < len(t.confidence[i]))
            extra_vals = {name: str(row[j]).strip() for j, name in extras}
            page = t.page_of_row(i)
            records.append(dict(date=dval, time=time_text, desc=desc, extras=extra_vals,
                                debit=debit, credit=credit, balance=balance, page=page,
                                conf=conf, how=how, unreadable=unreadable))
    if not records:
        return None

    header = ["Date"] + (["Time"] if has_time else []) + ["Description"] + extra_names + \
             ["Debit", "Credit"] + (["Balance"] if has_balance else []) + ["Page"]
    tx = Transactions(header=header)
    tx.debit_col = header.index("Debit")
    tx.credit_col = header.index("Credit")
    hows = {r["how"] for r in records if r["how"]}
    tx.method = ", ".join(sorted(hows))
    for rec in records:
        vals = [rec["date"]] + ([rec["time"]] if has_time else []) + [rec["desc"]] + \
               [rec["extras"].get(n, "") for n in extra_names] + \
               [rec["debit"] if rec["debit"] is not None else "",
                rec["credit"] if rec["credit"] is not None else ""] + \
               ([rec["balance"] if rec["balance"] is not None else ""] if has_balance else []) + \
               [rec["page"]]
        fmts = ["yyyy-mm-dd"] + (["@"] if has_time else []) + [None] * (1 + len(extra_names)) + \
               ["#,##0.00", "#,##0.00"] + (["#,##0.00"] if has_balance else []) + ["0"]
        flags = [1 if rec["conf"] < 60 and k in (tx.debit_col, tx.credit_col) and v != "" else 0
                 for k, v in enumerate(vals)]
        if rec.get("unreadable"):
            col = tx.debit_col if rec["unreadable"] == "debit" else tx.credit_col
            flags[col] = 1
            tx.comments[(len(tx.rows), col)] = (
                "This amount could not be read as a number, so it is not in the totals. "
                "Check it against the PDF and type the correct value.")
            tx.unreadable += 1
        tx.rows.append(vals)
        tx.formats.append(fmts)
        tx.flags.append(flags)

    if tx.unreadable:
        tx.checks.append(f"{tx.unreadable} amount(s) could not be read and are highlighted")
    if has_balance:
        _check_balances(tx, header.index("Balance"))
    return tx


def _check_balances(tx: Transactions, bcol: int) -> None:
    """Flag rows whose balance does not follow from the previous row."""
    ok_bank = ok_card = 0
    pairs = []
    prev = None
    for i, row in enumerate(tx.rows):
        bal = row[bcol]
        if bal == "" or bal is None:
            continue
        if prev is not None:
            d = row[tx.debit_col] if isinstance(row[tx.debit_col], (int, float)) else 0
            c = row[tx.credit_col] if isinstance(row[tx.credit_col], (int, float)) else 0
            bank = abs(prev - d + c - bal) < 0.011
            card = abs(prev + d - c - bal) < 0.011
            ok_bank += bank
            ok_card += card
            pairs.append((i, bank, card))
        prev = bal
    if not pairs:
        return
    use_bank = ok_bank >= ok_card
    bad = [i for i, bank, card in pairs if not (bank if use_bank else card)]
    good = len(pairs) - len(bad)
    tx.checks.append(f"Running balance matches on {good} of {len(pairs)} rows")
    for i in bad:
        for col in (tx.debit_col, tx.credit_col, bcol):
            if tx.rows[i][col] != "":
                tx.flags[i][col] = 1
        tx.comments[(i, bcol)] = ("The balance doesn't follow from the previous row. "
                                  "Check this row's amount and balance against the PDF.")
