"""Generate a test set covering the kinds of PDF the converter must handle.

    python -m tests.make_samples            -> writes into samples/
"""
from __future__ import annotations

import os
import random
import sys

import cv2
import numpy as np
import pymupdf
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")
random.seed(7)

REGIONS = ["North", "South", "East", "West", "Central"]
PRODUCTS = ["Widgets", "Gadgets", "Sprockets", "Gizmos", "Doohickeys", "Flanges"]


def ruled_report(path: str, rows: int = 70) -> None:
    """Digital PDF, ruled table with a merged header, spanning 2+ pages."""
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(path, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm)
    data = [["Region", "Product", "Sales", "", "Growth"],
            ["", "", "Q1", "Q2", ""]]
    for i in range(rows):
        q1 = random.randint(1000, 99999) + random.randint(0, 99) / 100
        q2 = random.randint(1000, 99999) + random.randint(0, 99) / 100
        g = (q2 - q1) / q1 * 100
        data.append([REGIONS[i % 5], PRODUCTS[i % 6], f"{q1:,.2f}", f"{q2:,.2f}", f"{g:.1f}%"])
    t = Table(data, repeatRows=2, colWidths=[30 * mm, 35 * mm, 32 * mm, 32 * mm, 25 * mm])
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.6, colors.black),
        ("SPAN", (2, 0), (3, 0)),     # "Sales" over Q1 and Q2
        ("SPAN", (0, 0), (0, 1)),
        ("SPAN", (1, 0), (1, 1)),
        ("SPAN", (4, 0), (4, 1)),
        ("BACKGROUND", (0, 0), (-1, 1), colors.HexColor("#DDDDDD")),
        ("ALIGN", (2, 2), (-1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
    ]))
    doc.build([Paragraph("Quarterly sales report", styles["Title"]),
               Paragraph("Figures in USD. Prepared by the finance team.", styles["Normal"]),
               Spacer(1, 8 * mm), t])


def bank_statement(path: str) -> None:
    """Digital PDF with an unruled (whitespace-aligned) table and wrapped text."""
    c = canvas.Canvas(path, pagesize=A4)
    W, H = A4
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, H - 50, "Acme Bank - Account Statement")
    c.setFont("Helvetica", 9)
    c.drawString(40, H - 66, "Account: 000123456789012345   Period: 01/03/2024 - 31/03/2024")
    cols = [40, 110, 330, 410, 490]
    y = H - 100
    c.setFont("Helvetica-Bold", 9)
    for x, h in zip(cols, ["Date", "Description", "Debit", "Credit", "Balance"]):
        c.drawString(x, y, h)
    c.setFont("Helvetica", 9)
    bal = 5000.00
    y -= 16
    descs = ["Card payment SUPERMARKET", "Salary ACME CORP", "Transfer to savings",
             "Direct debit ELECTRICITY COMPANY LTD monthly", "ATM withdrawal", "Refund ONLINE STORE"]
    for d in range(1, 26):
        desc = descs[d % len(descs)]
        amt = round(random.uniform(10, 900), 2)
        credit = desc.startswith(("Salary", "Refund"))
        bal = bal + amt if credit else bal - amt
        c.drawString(cols[0], y, f"{d:02d}/03/2024")
        if len(desc) > 32:  # wrap long descriptions onto a second line
            c.drawString(cols[1], y, desc[:30])
            c.drawString(cols[1], y - 10, desc[30:].strip())
        else:
            c.drawString(cols[1], y, desc)
        amount = f"{amt:,.2f}"
        if credit:
            c.drawRightString(cols[3] + 50, y, amount)
        else:
            c.drawRightString(cols[2] + 50, y, amount)
        bal_s = f"{bal:,.2f}" if bal >= 0 else f"({-bal:,.2f})"
        c.drawRightString(cols[4] + 60, y, bal_s)
        y -= 26 if len(desc) > 32 else 16
    c.drawString(40, 40, "Page 1 of 1")
    c.save()


def _outline(c, x, y, text, size=9, color=(0.1, 0.1, 0.1), bold=False, right=False):
    """Draw text as filled vector outlines (no font, no text layer), the way
    'Microsoft Print to PDF' and some report generators do."""
    from matplotlib.font_manager import FontProperties
    from matplotlib.textpath import TextPath
    tp = TextPath((0, 0), text, size=size,
                  prop=FontProperties(family="DejaVu Sans", weight="bold" if bold else "normal"))
    if right:
        x -= tp.get_extents().width
    verts, codes = tp.vertices, tp.codes
    p = c.beginPath()
    i, cur = 0, (0.0, 0.0)
    while i < len(codes):
        code = codes[i]
        vx, vy = verts[i]
        if code == 1:
            p.moveTo(x + vx, y + vy); cur = (vx, vy); i += 1
        elif code == 2:
            p.lineTo(x + vx, y + vy); cur = (vx, vy); i += 1
        elif code == 3:   # quadratic -> cubic
            (qx, qy), (ex, ey) = verts[i], verts[i + 1]
            c1 = (cur[0] + 2 / 3 * (qx - cur[0]), cur[1] + 2 / 3 * (qy - cur[1]))
            c2 = (ex + 2 / 3 * (qx - ex), ey + 2 / 3 * (qy - ey))
            p.curveTo(x + c1[0], y + c1[1], x + c2[0], y + c2[1], x + ex, y + ey)
            cur = (ex, ey); i += 2
        elif code == 4:
            (ax, ay), (bx, by), (ex, ey) = verts[i], verts[i + 1], verts[i + 2]
            p.curveTo(x + ax, y + ay, x + bx, y + by, x + ex, y + ey)
            cur = (ex, ey); i += 3
        else:
            p.close(); i += 1
    c.setFillColorRGB(*color)
    c.drawPath(p, stroke=0, fill=1, fillMode=0)


def card_statement_vector(path: str) -> list:
    """Credit-card statement like many Indian card issuers send: text drawn as
    shapes, white-on-dark table header, credits shown only in teal, the time
    printed under the date, repeated page header/footer. Returns the truth."""
    c = canvas.Canvas(path, pagesize=A4)
    W, H = A4
    teal, ink = (0.17, 0.71, 0.66), (0.12, 0.12, 0.12)
    merchants = ["SWIGGY", "ZOMATO GURGAON", "RELIANCE JIO INFOCOMM", "Airtel",
                 "AMAZON PAY INDIA", "DMART", "UBER INDIA", "Repayment - Thank You", "REFUND"]
    truth, day = [], 1
    for page in range(1, 4):
        c.setFillColorRGB(0.93, 0.95, 0.98)
        c.rect(0, H - 60, W, 60, stroke=0, fill=1)
        _outline(c, 30, H - 30, "Issued by", 8, (0.4, 0.4, 0.4))
        _outline(c, W - 30, H - 25, "SAMPLE BANK CREDIT CARD STATEMENT", 10, ink, True, right=True)
        _outline(c, W - 30, H - 45, "07 JUL 2026 - 06 AUG 2026", 10, ink, True, right=True)
        _outline(c, 30, H - 100, "Card Transactions - 1234", 16, ink)
        y0 = H - 140
        c.setFillColorRGB(0.25, 0.25, 0.25)
        c.rect(30, y0 - 6, W - 60, 20, stroke=0, fill=1)
        _outline(c, 36, y0, "Date", 9, (1, 1, 1), True)
        _outline(c, 130, y0, "Transaction details", 9, (1, 1, 1), True)
        _outline(c, W - 36, y0, "Amount (INR)", 9, (1, 1, 1), True, right=True)
        y = y0 - 34
        for k in range(12 if page < 3 else 5):
            name = merchants[(page * 7 + k * 3) % len(merchants)]
            credit = name.startswith(("Repayment", "REFUND"))
            amount = round(random.uniform(20, 2500), 2)
            hh, mm = random.randint(1, 12), random.randint(0, 59)
            ampm = random.choice(["am", "pm"])
            _outline(c, 36, y, f"{day:02d} Jul 26 {hh:02d}:", 9, ink)
            _outline(c, 36, y - 11, f"{mm:02d} {ampm}", 9, ink)
            _outline(c, 130, y, name, 9, ink)
            _outline(c, W - 36, y, f"Rs. {amount:,.2f}", 9, teal if credit else ink, right=True)
            c.setStrokeColorRGB(0.85, 0.85, 0.85)
            c.line(30, y - 20, W - 30, y - 20)
            truth.append((f"2026-07-{day:02d}", name, amount, credit))
            day = min(day + 1, 28)
            y -= 50
        c.setFillColorRGB(0, 0, 0)
        c.rect(0, 0, W, 50, stroke=0, fill=1)
        _outline(c, 30, 28, "support@samplebank.example", 9, (1, 1, 1))
        _outline(c, W / 2, 20, f"Page {page} of 3", 9, (1, 1, 1))
        c.showPage()
    c.save()
    return truth


def text_only(path: str) -> None:
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(path, pagesize=A4)
    para = ("This document contains only running text and no tables. The converter should "
            "still produce a spreadsheet with the text placed line by line. ") * 4
    doc.build([Paragraph("Meeting notes", styles["Title"]), Paragraph(para, styles["Normal"]),
               Spacer(1, 6 * mm), Paragraph(para, styles["Normal"])])


def page_to_image(pdf_path: str, page: int = 0, dpi: int = 200) -> np.ndarray:
    d = pymupdf.open(pdf_path)
    pix = d[page].get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()
    d.close()
    return img


def degrade(img: np.ndarray, angle: float = 1.2, noise: int = 12) -> np.ndarray:
    """Make a clean render look like a mediocre office scan."""
    h, w = img.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    img = cv2.warpAffine(img, m, (w, h), borderValue=255)
    img = cv2.GaussianBlur(img, (3, 3), 0)
    rng = np.random.default_rng(1)
    img = np.clip(img.astype(np.int16) + rng.normal(0, noise, img.shape), 0, 255).astype(np.uint8)
    img = (img * 0.85 + 30).astype(np.uint8)  # greyish paper, lower contrast
    return img


def image_pdf(images, path: str, dpi: int = 200) -> None:
    """Wrap images into an image-only PDF (no text layer), like a scanner does."""
    doc = pymupdf.open()
    for img in images:
        ok, png = cv2.imencode(".png", img)
        h, w = img.shape[:2]
        page = doc.new_page(width=w * 72 / dpi, height=h * 72 / dpi)
        page.insert_image(page.rect, stream=png.tobytes())
    doc.save(path)


def mixed(path: str, table_img: np.ndarray) -> None:
    """Digital text page with a scanned table pasted in as an image."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 60), "Supplier price list (scanned attachment below)", fontsize=14)
    page.insert_text((50, 80), "The table below was pasted in from a paper copy.", fontsize=10)
    crop = table_img[int(table_img.shape[0] * 0.17): int(table_img.shape[0] * 0.55), :]
    ok, png = cv2.imencode(".png", crop)
    h, w = crop.shape
    rect = pymupdf.Rect(30, 100, 565, 100 + 535 * h / w)
    page.insert_image(rect, stream=png.tobytes())
    doc.save(path)


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    p = lambda n: os.path.join(OUT, n)  # noqa: E731
    ruled_report(p("digital_ruled_multipage.pdf"))
    bank_statement(p("digital_borderless_statement.pdf"))
    text_only(p("digital_text_only.pdf"))
    import json
    truth = card_statement_vector(p("card_statement_vector_text.pdf"))
    with open(p("card_statement_vector_text.json"), "w") as f:
        json.dump(truth, f)

    ruled_img = page_to_image(p("digital_ruled_multipage.pdf"), 0)
    stmt_img = page_to_image(p("digital_borderless_statement.pdf"), 0)
    image_pdf([degrade(ruled_img, 1.2)], p("scanned_ruled_skewed.pdf"))
    image_pdf([degrade(stmt_img, -0.8)], p("scanned_borderless_statement.pdf"))
    image_pdf([cv2.rotate(degrade(stmt_img, 0.0, 6), cv2.ROTATE_90_CLOCKWISE)],
              p("scanned_rotated_90.pdf"))
    mixed(p("mixed_digital_with_scan.pdf"), ruled_img)
    cv2.imwrite(p("photo_of_table.png"), degrade(ruled_img, 0.5, 6))

    d = pymupdf.open(p("digital_borderless_statement.pdf"))
    d.save(p("password_protected.pdf"), encryption=pymupdf.PDF_ENCRYPT_AES_256,
           user_pw="secret", owner_pw="owner")
    d.close()
    print("samples written to", OUT)
    for f in sorted(os.listdir(OUT)):
        print("  ", f)


if __name__ == "__main__":
    sys.exit(main())
