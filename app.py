from flask import Flask, render_template, request, flash, redirect, url_for, Response
from flask_wtf.csrf import CSRFProtect
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph
from io import BytesIO
from datetime import date, timedelta
from email.message import EmailMessage
import smtplib
import uuid
import math
from supabase import create_client
import os
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", os.urandom(32))
csrf = CSRFProtect(app)

# ======================================================
# CONFIG
# ======================================================
COMPANY_NAME     = os.getenv("COMPANY_NAME")
COMPANY_ADDRESS  = os.getenv("COMPANY_ADDRESS")
COMPANY_EMAIL    = os.getenv("COMPANY_EMAIL")
COMPANY_WHATSAPP = os.getenv("COMPANY_WHATSAPP")
COMPANY_GSTIN    = os.getenv("COMPANY_GSTIN", "")
SIGN_NAME        = os.getenv("SIGN_NAME")
SIGN_MOBILE      = os.getenv("SIGN_MOBILE")
OWNER_EMAIL      = os.getenv("OWNER_EMAIL")
SMTP_EMAIL       = os.getenv("SMTP_EMAIL")
SMTP_PASSWORD    = os.getenv("SMTP_PASSWORD")
SUPABASE_URL     = os.getenv("SUPABASE_URL")
SUPABASE_KEY     = os.getenv("SUPABASE_KEY")

# Bank / Payment (optional)
BANK_NAME        = os.getenv("BANK_NAME", "")
BANK_ACCOUNT     = os.getenv("BANK_ACCOUNT", "")
BANK_IFSC        = os.getenv("BANK_IFSC", "")
UPI_ID           = os.getenv("UPI_ID", "")
PAYMENT_TERMS    = os.getenv("PAYMENT_TERMS", "Due on Receipt")

_required = {
    "COMPANY_NAME": COMPANY_NAME, "OWNER_EMAIL": OWNER_EMAIL,
    "SMTP_EMAIL": SMTP_EMAIL, "SMTP_PASSWORD": SMTP_PASSWORD,
    "SUPABASE_URL": SUPABASE_URL, "SUPABASE_KEY": SUPABASE_KEY,
}
_missing = [k for k, v in _required.items() if not v]
if _missing:
    raise EnvironmentError(f"Missing required env vars: {', '.join(_missing)}")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
LOGO_PATH = os.path.join("static", "logo.png")

# In-memory PDF store (invoice_no -> bytes)
_pdf_store = {}

# ======================================================
# FONT — Arial for ₹ Unicode on Windows
# ======================================================
_F   = "Helvetica"
_FB  = "Helvetica-Bold"
_CUR = "Rs."

try:
    _ar, _arb = r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"
    if os.path.exists(_ar) and os.path.exists(_arb):
        pdfmetrics.registerFont(TTFont("Arial",      _ar))
        pdfmetrics.registerFont(TTFont("Arial-Bold", _arb))
        _F, _FB, _CUR = "Arial", "Arial-Bold", "\u20b9"
except Exception:
    pass

# ======================================================
# UTILS
# ======================================================
def money(v):
    return Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def rupee(v):
    return f"{_CUR} {float(v):,.2f}"


def num_to_words_indian(n):
    """Convert a number to words in Indian numbering system (Lakhs, Crores)."""
    if n == 0:
        return "Zero"

    ones = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven",
            "Eight", "Nine", "Ten", "Eleven", "Twelve", "Thirteen",
            "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
    tens = ["", "", "Twenty", "Thirty", "Forty", "Fifty",
            "Sixty", "Seventy", "Eighty", "Ninety"]

    def _two_digits(num):
        if num < 20:
            return ones[num]
        t, o = divmod(num, 10)
        return (tens[t] + " " + ones[o]).strip()

    def _three_digits(num):
        h, rest = divmod(num, 100)
        parts = []
        if h:
            parts.append(ones[h] + " Hundred")
        if rest:
            parts.append(_two_digits(rest))
        return " and ".join(parts) if h and rest else " ".join(parts)

    n = abs(float(n))
    rupees = int(n)
    paise = round((n - rupees) * 100)

    if rupees == 0 and paise == 0:
        return "Zero"

    parts = []
    if rupees > 0:
        # Indian system: Crore, Lakh, Thousand, Hundred
        crore, rupees_rem = divmod(rupees, 10000000)
        lakh, rupees_rem = divmod(rupees_rem, 100000)
        thousand, rupees_rem = divmod(rupees_rem, 1000)
        rest = rupees_rem

        if crore:
            parts.append(_three_digits(crore) + " Crore")
        if lakh:
            parts.append(_two_digits(lakh) + " Lakh")
        if thousand:
            parts.append(_two_digits(thousand) + " Thousand")
        if rest:
            parts.append(_three_digits(rest))

    result = "Rupees " + " ".join(parts)
    if paise:
        result += " and " + _two_digits(paise) + " Paise"
    result += " Only"
    return result


# ======================================================
# PDF — INDUSTRY-STANDARD PREMIUM LAYOUT
# ======================================================

# ── Geometry ─────────────────────────────────────────
PW, PH   = A4                      # 595.27 × 841.89 pt
ML, MR   = 40, 40
CW       = PW - ML - MR            # ≈ 515 pt

# ── Header ───────────────────────────────────────────
HDR_H    = 100

# ── Palette ──────────────────────────────────────────
C_NAVY   = colors.HexColor("#0a1f3d")
C_NAVY2  = colors.HexColor("#0d2649")
C_ACCENT = colors.HexColor("#2563eb")
C_GREEN  = colors.HexColor("#15803d")
C_ROWALT = colors.HexColor("#f5f8fd")
C_BORD   = colors.HexColor("#dce5f0")
C_DARK   = colors.HexColor("#0f172a")
C_MID    = colors.HexColor("#475569")
C_LIGHT  = colors.HexColor("#94a3b8")
C_WHITE  = colors.white
C_WARM   = colors.HexColor("#f8fafc")
C_RED    = colors.HexColor("#dc2626")

# ── Table column positions (with S.No.) ──────────────
COL_SNO   = ML + 4
COL_DESC  = ML + 36
COL_HSN   = ML + 260
COL_QTY   = ML + 330
COL_RATE  = ML + 405
COL_AMT   = ML + CW - 6

ROW_H    = 26

# Minimum y before we need a page break
BOTTOM_RESERVE = 320

# Paragraph style for wrapping descriptions
DESC_STYLE = ParagraphStyle(
    "desc", fontName=_F, fontSize=9, leading=12,
    textColor=C_DARK,
)


# ─────────────────────────────────────────────────────
def _header(c, invoice_no=None, pg=1, total_pg=1):
    """Professional header with logo, company info, and optional GSTIN."""

    # Light background strip
    c.setFillColor(colors.HexColor("#eef3fb"))
    c.rect(0, PH - HDR_H, PW, HDR_H, fill=1, stroke=0)

    # Bottom accent stripe — removed per user request

    # Logo
    if os.path.exists(LOGO_PATH):
        lw, lh = 140, 56
        ly = PH - HDR_H + (HDR_H - lh) / 2
        c.drawImage(ImageReader(LOGO_PATH), ML, ly, lw, lh, mask="auto")

    # Company name
    c.setFillColor(C_NAVY)
    c.setFont(_FB, 13)
    c.drawRightString(PW - MR, PH - 28, COMPANY_NAME or "")

    # Sub-details
    c.setFont(_F, 8)
    c.setFillColor(C_MID)
    sub = [x for x in [
        COMPANY_ADDRESS,
        COMPANY_EMAIL,
        f"WhatsApp: {COMPANY_WHATSAPP}" if COMPANY_WHATSAPP else None,
        f"GSTIN: {COMPANY_GSTIN}" if COMPANY_GSTIN else None,
    ] if x]
    for i, line in enumerate(sub):
        c.drawRightString(PW - MR, PH - 44 - i * 13, line)


def _tbl_header(c, y):
    """Table header with S.No., Description, HSN/SAC, Qty, Rate, Amount."""
    c.setFillColor(C_NAVY)
    c.rect(ML, y - 8, CW, ROW_H + 6, fill=1, stroke=0)
    c.setFillColor(C_WHITE)
    c.setFont(_FB, 7.5)
    c.drawString(COL_SNO,  y + 5, "S.NO")
    c.drawString(COL_DESC, y + 5, "DESCRIPTION")
    c.drawRightString(COL_QTY,  y + 5, "QTY")
    c.drawRightString(COL_RATE, y + 5, f"RATE ({_CUR})")
    c.drawRightString(COL_AMT,  y + 5, f"AMOUNT ({_CUR})")


def _footer(c, pg=1, total_pg=1):
    """Footer with accent line, legal note, company info, and page number."""
    # Top accent line — removed per user request

    c.setFont(_F, 7)
    c.setFillColor(C_LIGHT)
    c.drawCentredString(PW / 2, 38,
        "This is a computer-generated invoice and does not require a physical signature.")
    parts = [p for p in [COMPANY_NAME, COMPANY_EMAIL, COMPANY_ADDRESS] if p]
    c.drawCentredString(PW / 2, 26, "  ·  ".join(parts))

    # Page number
    if total_pg > 1:
        c.setFont(_F, 7)
        c.setFillColor(C_LIGHT)
        c.drawRightString(PW - MR, 14, f"Page {pg} of {total_pg}")


def _draw_bank_details(c, y):
    """Payment information box with bank details and UPI."""
    if not BANK_NAME and not UPI_ID:
        return y

    box_x = ML
    box_w = 260

    c.setFont(_FB, 7.5)
    c.setFillColor(C_LIGHT)
    c.drawString(box_x, y, "PAYMENT INFORMATION")
    y -= 6

    # Light background box
    c.setFillColor(C_WARM)
    box_h = 14 * sum(1 for v in [BANK_NAME, BANK_ACCOUNT, BANK_IFSC, UPI_ID] if v) + 16
    c.rect(box_x, y - box_h, box_w, box_h, fill=1, stroke=0)
    c.setStrokeColor(C_BORD)
    c.setLineWidth(0.5)
    c.rect(box_x, y - box_h, box_w, box_h, fill=0, stroke=1)

    # Left accent
    c.setFillColor(C_ACCENT)
    c.rect(box_x, y - box_h, 3, box_h, fill=1, stroke=0)

    pad_x = box_x + 12
    inner_y = y - 14

    def _bank_line(label, value):
        nonlocal inner_y
        if not value:
            return
        c.setFont(_F, 7.5)
        c.setFillColor(C_MID)
        c.drawString(pad_x, inner_y, f"{label}:")
        c.setFont(_FB, 8)
        c.setFillColor(C_DARK)
        c.drawString(pad_x + 85, inner_y, value)
        inner_y -= 14

    _bank_line("Bank Name", BANK_NAME)
    _bank_line("Account No", BANK_ACCOUNT)
    _bank_line("IFSC Code", BANK_IFSC)
    _bank_line("UPI ID", UPI_ID)

    return y - box_h - 12


def _draw_notes(c, y, notes):
    """Terms & Conditions / Notes section."""
    if not notes:
        return y

    c.setFont(_FB, 7.5)
    c.setFillColor(C_LIGHT)
    c.drawString(ML, y, "TERMS & CONDITIONS")
    y -= 14

    c.setFont(_F, 8)
    c.setFillColor(C_MID)
    for line in notes.split("\n"):
        line = line.strip()
        if line:
            c.drawString(ML + 8, y, f"• {line}")
            y -= 13
    return y - 6


# ─────────────────────────────────────────────────────
def create_invoice_pdf(data, invoice_no, items, subtotal,
                       discount_pct=Decimal("0"), tax_pct=Decimal("0"),
                       notes=""):
    discount_pct = money(discount_pct)
    tax_pct      = money(tax_pct)
    discount_amt = money(subtotal * discount_pct / Decimal("100"))
    taxable      = subtotal - discount_amt
    tax_amt      = money(taxable * tax_pct / Decimal("100"))
    total        = taxable + tax_amt
    has_discount = discount_pct > 0
    has_tax      = tax_pct > 0

    # First pass: calculate total pages needed
    test_y = PH - HDR_H - 180  # starting y after header + meta + bill-to
    pages_needed = 1
    for idx in range(len(items)):
        if test_y < BOTTOM_RESERVE + ROW_H:
            pages_needed += 1
            test_y = PH - HDR_H - 50
        test_y -= ROW_H
    total_pages = pages_needed

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    current_page = 1

    # ── PAGE 1 ────────────────────────────────────────
    _header(c, invoice_no, current_page, total_pages)

    # ── INVOICE title + meta ──────────────────────────
    y = PH - HDR_H - 40

    c.setFillColor(C_NAVY)
    c.setFont(_FB, 28)
    c.drawString(ML, y, "INVOICE")
    
    # Right meta block
    meta_x = PW - MR

    c.setFont(_FB, 7.5)
    c.setFillColor(C_LIGHT)
    c.drawRightString(meta_x, y + 6, "INVOICE NUMBER")
    c.setFont(_FB, 12)
    c.setFillColor(C_ACCENT)
    c.drawRightString(meta_x, y - 10, invoice_no)

    c.setFont(_F, 8)
    c.setFillColor(C_MID)
    c.drawRightString(meta_x, y - 26, f"Date: {date.today().strftime('%d %B %Y')}")

    # Due date / payment terms
    if PAYMENT_TERMS:
        c.drawRightString(meta_x, y - 39, f"Terms: {PAYMENT_TERMS}")

    # ── BILL TO ───────────────────────────────────────
    y -= 68

    # Accent bar
    c.setFillColor(C_ACCENT)
    c.rect(ML, y - 34, 3, 50, fill=1, stroke=0)

    pad = ML + 14
    c.setFont(_FB, 7.5)
    c.setFillColor(C_LIGHT)
    c.drawString(pad, y, "BILL TO")

    y -= 16
    c.setFont(_FB, 12)
    c.setFillColor(C_DARK)
    c.drawString(pad, y, data["client_name"] or "\u2014")

    if data.get("client_email"):
        y -= 15
        c.setFont(_F, 8.5)
        c.setFillColor(C_MID)
        c.drawString(pad, y, data["client_email"])

    # ── Divider ───────────────────────────────────────
    y -= 22
    c.setStrokeColor(C_BORD)
    c.setLineWidth(0.8)
    c.line(ML, y, PW - MR, y)

    # ── Table ─────────────────────────────────────────
    y -= 24
    _tbl_header(c, y)
    y -= ROW_H

    for idx, item in enumerate(items):
        if y < BOTTOM_RESERVE + ROW_H:
            _footer(c, current_page, total_pages)
            c.showPage()
            current_page += 1
            _header(c, invoice_no, current_page, total_pages)
            y = PH - HDR_H - 30
            _tbl_header(c, y)
            y -= ROW_H

        # Alternating row bg
        if idx % 2 == 0:
            c.setFillColor(C_ROWALT)
            c.rect(ML, y - 7, CW, ROW_H, fill=1, stroke=0)

        # Row border
        c.setStrokeColor(C_BORD)
        c.setLineWidth(0.3)
        c.line(ML, y - 7, ML + CW, y - 7)

        # Vertical separators
        for sx in (COL_DESC - 4, COL_QTY + 14, COL_RATE + 14):
            c.line(sx, y - 7, sx, y + ROW_H - 7)

        # S.No.
        c.setFont(_F, 8.5)
        c.setFillColor(C_MID)
        c.drawString(COL_SNO, y + 5, str(idx + 1))

        # Description (truncate for now, fits single row)
        desc = item["desc"]
        if len(desc) > 48:
            desc = desc[:46] + "…"
        c.setFont(_F, 9)
        c.setFillColor(C_DARK)
        c.drawString(COL_DESC, y + 5, desc)

        # Qty
        c.setFillColor(C_MID)
        c.drawRightString(COL_QTY, y + 5, str(item["qty"]))

        # Rate
        c.drawRightString(COL_RATE, y + 5, f"{float(item['rate']):,.2f}")

        # Amount
        c.setFont(_FB, 9)
        c.setFillColor(C_DARK)
        c.drawRightString(COL_AMT, y + 5, f"{float(item['total']):,.2f}")

        y -= ROW_H

    # Closing table line (double)
    c.setStrokeColor(C_NAVY)
    c.setLineWidth(1.5)
    c.line(ML, y + ROW_H - 7, ML + CW, y + ROW_H - 7)
    c.setLineWidth(0.5)
    c.line(ML, y + ROW_H - 10, ML + CW, y + ROW_H - 10)

    # Safety page break
    if y < BOTTOM_RESERVE:
        _footer(c, current_page, total_pages)
        c.showPage()
        current_page += 1
        _header(c, invoice_no, current_page, total_pages)
        y = PH - HDR_H - 50

    # ── TOTALS ────────────────────────────────────────
    TBOX_W = 235
    TBOX_X = PW - MR - TBOX_W
    y -= 18

    def _total_row(label, value, is_green=False, is_bold=False):
        nonlocal y
        c.setFont(_FB if is_bold else _F, 9)
        c.setFillColor(C_MID)
        c.drawString(TBOX_X, y, label)
        c.setFont(_FB, 9.5)
        c.setFillColor(C_GREEN if is_green else C_DARK)
        c.drawRightString(PW - MR, y, value)
        y -= 20

    if has_discount or has_tax:
        _total_row("Subtotal", rupee(subtotal))

    if has_discount:
        _total_row(f"Discount ({float(discount_pct):g}%)",
                   f"\u2212 {rupee(discount_amt)}", is_green=True)

    if has_tax:
        # Show as GST
        half = money(tax_amt / Decimal("2"))
        _total_row(f"CGST ({float(tax_pct / Decimal('2')):g}%)", rupee(half))
        _total_row(f"SGST ({float(tax_pct / Decimal('2')):g}%)", rupee(half))

    if has_discount or has_tax:
        # Divider above total
        y += 6
        c.setStrokeColor(C_BORD)
        c.setLineWidth(0.8)
        c.line(TBOX_X, y, PW - MR, y)
        y -= 16

    # ── Grand Total Box ──────────────────────────────
    TBOX_H = 42
    box_top = y
    box_bottom = box_top - TBOX_H
    c.setFillColor(C_NAVY)
    c.rect(TBOX_X, box_bottom, TBOX_W, TBOX_H, fill=1, stroke=0)
    # Left accent
    c.setFillColor(C_ACCENT)
    c.rect(TBOX_X, box_bottom, 5, TBOX_H, fill=1, stroke=0)
    # "INVOICE TOTAL" label — upper portion of box
    c.setFillColor(C_WHITE)
    c.setFont(_FB, 7.5)
    c.drawString(TBOX_X + 16, box_bottom + TBOX_H - 14, "INVOICE TOTAL")
    # Amount — lower portion of box
    c.setFont(_FB, 15)
    c.drawRightString(PW - MR - 12, box_bottom + 10, rupee(total))
    y = box_bottom - 16

    # ── Amount in Words ──────────────────────────────
    c.setFont(_FB, 7.5)
    c.setFillColor(C_LIGHT)
    c.drawString(ML, y, "AMOUNT IN WORDS")
    y -= 14
    c.setFont(_F, 8.5)
    c.setFillColor(C_DARK)
    words = num_to_words_indian(float(total))
    # Split long text into two lines if needed
    if len(words) > 75:
        mid = words.rfind(" ", 0, 75)
        c.drawString(ML, y, words[:mid])
        y -= 13
        c.drawString(ML, y, words[mid + 1:])
    else:
        c.drawString(ML, y, words)

    y -= 20

    # ── Divider ───────────────────────────────────────
    c.setStrokeColor(C_BORD)
    c.setLineWidth(0.5)
    c.line(ML, y, PW - MR, y)
    y -= 16

    # ── Bank Details (left) + Signature (right) ──────
    # Draw them side-by-side only if bank details exist,
    # otherwise just draw signature full-width right-aligned
    has_bank = bool(BANK_NAME or UPI_ID)
    sig_x = PW - MR - 195 if has_bank else PW - MR - 195

    if has_bank:
        _draw_bank_details(c, y)

    # Signature block (right side)
    c.setFont(_FB, 7.5)
    c.setFillColor(C_LIGHT)
    c.drawString(sig_x, y, "AUTHORISED SIGNATORY")

    c.setFont(_FB, 11)
    c.setFillColor(C_DARK)
    c.drawString(sig_x, y - 16, SIGN_NAME or "")

    c.setFont(_F, 8)
    c.setFillColor(C_MID)
    sig_parts = [p for p in [
        f"Mob: {SIGN_MOBILE}" if SIGN_MOBILE else None,
        f"For {COMPANY_NAME}" if COMPANY_NAME else None,
    ] if p]
    c.drawString(sig_x, y - 30, "  ·  ".join(sig_parts))

    # Signature line
    c.setStrokeColor(C_BORD)
    c.setLineWidth(0.6)
    c.line(sig_x, y - 38, PW - MR, y - 38)

    c.setFont(_F, 7.5)
    c.setFillColor(C_LIGHT)
    c.drawString(sig_x, y - 50, date.today().strftime("%d/%m/%Y"))

    y -= 60

    # ── Notes / Terms ─────────────────────────────────
    if notes:
        _draw_notes(c, y, notes)

    _footer(c, current_page, total_pages)
    c.save()
    buf.seek(0)
    return buf


# ======================================================
# EMAIL
# ======================================================
def send_invoice_email(to_email, pdf_buffer, invoice_no):
    msg = EmailMessage()
    msg["From"]    = f"{COMPANY_NAME} <{SMTP_EMAIL}>"
    msg["To"]      = to_email
    msg["Subject"] = f"Invoice {invoice_no} from {COMPANY_NAME}"
    msg.set_content(
        f"Hi,\n\nPlease find your invoice {invoice_no} from {COMPANY_NAME} attached.\n\n"
        f"For queries contact us at {COMPANY_EMAIL}.\n\nThank you."
    )
    msg.add_attachment(
        pdf_buffer.read(),
        maintype="application", subtype="pdf",
        filename=f"{invoice_no}.pdf",
    )
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=10) as srv:
        srv.starttls()
        srv.login(SMTP_EMAIL, SMTP_PASSWORD)
        srv.send_message(msg)


# ======================================================
# ROUTE
# ======================================================
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        try:
            descs = request.form.getlist("desc[]")
            qtys  = request.form.getlist("qty[]")
            rates = request.form.getlist("rate[]")

            if not descs or len(descs) != len(qtys) or len(descs) != len(rates):
                flash("Invalid submission: item fields are mismatched.")
                return redirect(url_for("index"))

            items    = []
            subtotal = Decimal("0.00")

            for d, q, r in zip(descs, qtys, rates):
                d = d.strip()
                if not d:
                    flash("Item description cannot be empty.")
                    return redirect(url_for("index"))
                try:
                    qty  = Decimal(q)
                    rate = Decimal(r)
                except InvalidOperation:
                    flash("Quantity and rate must be valid numbers.")
                    return redirect(url_for("index"))
                if qty <= 0:
                    flash("Quantity must be greater than 0.")
                    return redirect(url_for("index"))
                if rate < 0:
                    flash("Rate cannot be negative.")
                    return redirect(url_for("index"))

                line_total = money(qty * rate)
                subtotal  += line_total
                items.append({"desc": d, "qty": q, "rate": rate, "total": line_total})

            # Discount (optional)
            try:
                discount_pct = Decimal(request.form.get("discount", "0") or "0")
                discount_pct = max(Decimal("0"), min(Decimal("100"), discount_pct))
            except InvalidOperation:
                discount_pct = Decimal("0")

            # Tax / GST (optional)
            try:
                tax_pct = Decimal(request.form.get("tax", "0") or "0")
                tax_pct = max(Decimal("0"), min(Decimal("100"), tax_pct))
            except InvalidOperation:
                tax_pct = Decimal("0")

            # Notes
            notes = request.form.get("notes", "").strip()

            discount_amt = money(subtotal * discount_pct / Decimal("100"))
            taxable      = subtotal - discount_amt
            tax_amt      = money(taxable * tax_pct / Decimal("100"))
            total        = taxable + tax_amt

            invoice_no = f"COG-{uuid.uuid4().hex[:6].upper()}"
            data = {
                "client_name":  request.form.get("name",  "").strip(),
                "client_email": request.form.get("email", "").strip(),
            }

            # Generate PDF
            pdf = create_invoice_pdf(data, invoice_no, items, subtotal,
                                     discount_pct, tax_pct, notes)

            # Store PDF in memory for download (no local file)
            _pdf_store[invoice_no] = pdf.getvalue()

            # Supabase — optional, never blocks delivery
            try:
                label = (items[0]["desc"] if len(items) == 1
                         else f"Multiple Items ({len(items)})")
                supabase.table("invoices").insert({
                    "invoice_no":   invoice_no,
                    "client_name":  data["client_name"],
                    "client_email": data["client_email"],
                    "service":      label,
                    "amount":       float(total),
                }).execute()
            except Exception as db_err:
                app.logger.warning(f"Supabase insert skipped: {db_err}")

            send_invoice_email(OWNER_EMAIL, pdf, invoice_no)
            if data["client_email"]:
                pdf.seek(0)
                send_invoice_email(data["client_email"], pdf, invoice_no)

            return render_template("success.html",
                                   invoice_no=invoice_no)

        except Exception as e:
            app.logger.error(f"Invoice generation failed: {e}")
            flash("Something went wrong while generating the invoice. Please try again.")
            return redirect(url_for("index"))

    return render_template("index.html")


@app.route("/download/<invoice_no>")
def download_pdf(invoice_no):
    pdf_bytes = _pdf_store.pop(invoice_no, None)
    if pdf_bytes is None:
        flash("PDF not found or already downloaded. Please generate a new invoice.")
        return redirect(url_for("index"))
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={invoice_no}.pdf"},
    )


# ======================================================
# START
# ======================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
