from flask import Flask, render_template, request
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from io import BytesIO
from datetime import date
from email.message import EmailMessage
import smtplib
import uuid
from supabase import create_client
import os
from decimal import Decimal, ROUND_HALF_UP
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ======================================================
# COMPANY DETAILS (FROM .env)
# ======================================================
COMPANY_NAME = os.getenv("COMPANY_NAME")
COMPANY_ADDRESS = os.getenv("COMPANY_ADDRESS")
COMPANY_EMAIL = os.getenv("COMPANY_EMAIL")
COMPANY_WHATSAPP = os.getenv("COMPANY_WHATSAPP")

# Digital Signature
SIGN_NAME = os.getenv("SIGN_NAME")
SIGN_MOBILE = os.getenv("SIGN_MOBILE")

# Email
OWNER_EMAIL = os.getenv("OWNER_EMAIL")
SMTP_EMAIL = os.getenv("SMTP_EMAIL")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

# Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Logo
LOGO_PATH = os.path.join("static", "logo.png")

# ======================================================
# UTILS
# ======================================================
def money(value):
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

# ======================================================
# CREATE PDF (MULTI-ITEM)
# ======================================================
def create_invoice_pdf(data, invoice_no, items, subtotal, gst, total):
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    # Logo
    if os.path.exists(LOGO_PATH):
        c.drawImage(ImageReader(LOGO_PATH), 30, height - 90, 120, 45, mask="auto")

    # Company details
    c.setFont("Helvetica-Bold", 14)
    c.drawString(180, height - 60, COMPANY_NAME)

    c.setFont("Helvetica", 9)
    c.drawString(180, height - 75, COMPANY_ADDRESS)
    c.drawString(180, height - 88, f"Email: {COMPANY_EMAIL}")
    c.drawString(180, height - 101, f"WhatsApp: {COMPANY_WHATSAPP}")

    # Invoice info
    c.setFont("Helvetica-Bold", 16)
    c.drawString(30, height - 130, "INVOICE")

    c.setFont("Helvetica", 10)
    c.drawString(400, height - 130, f"Invoice No: {invoice_no}")
    c.drawString(400, height - 145, f"Date: {date.today()}")

    # Client
    c.setFont("Helvetica-Bold", 11)
    c.drawString(30, height - 180, "Bill To:")
    c.setFont("Helvetica", 10)
    c.drawString(30, height - 195, data["client_name"] or "N/A")
    c.drawString(30, height - 210, data["client_email"] or "N/A")

    # Table Header
    y = height - 250
    c.setFont("Helvetica-Bold", 10)
    c.drawString(30, y, "Description")
    c.drawString(320, y, "Qty")
    c.drawString(370, y, "Rate (₹)")
    c.drawString(460, y, "Amount (₹)")
    c.line(30, y - 5, 550, y - 5)

    # Items
    c.setFont("Helvetica", 10)
    y -= 25
    for item in items:
        c.drawString(30, y, item["desc"])
        c.drawString(320, y, str(item["qty"]))
        c.drawString(370, y, str(item["rate"]))
        c.drawString(460, y, str(item["total"]))
        y -= 18

    # Totals
    y -= 20
    c.drawString(350, y, "Subtotal:")
    c.drawString(460, y, str(subtotal))

    y -= 18
    c.drawString(350, y, "GST (18%):")
    c.drawString(460, y, str(gst))

    y -= 18
    c.setFont("Helvetica-Bold", 11)
    c.drawString(350, y, "Total:")
    c.drawString(460, y, str(total))

    # Signature
    c.setFont("Helvetica", 9)
    c.line(350, 120, 550, 120)
    c.drawString(350, 105, "Digitally Signed By")
    c.setFont("Helvetica-Bold", 10)
    c.drawString(350, 90, SIGN_NAME)
    c.setFont("Helvetica", 9)
    c.drawString(350, 75, f"Mobile: {SIGN_MOBILE}")
    c.drawString(350, 60, f"For {COMPANY_NAME}")

    c.setFont("Helvetica", 8)
    c.drawString(30, 60, "This is a system-generated invoice. No physical signature required.")

    c.save()
    buffer.seek(0)
    return buffer

# ======================================================
# EMAIL
# ======================================================
def send_invoice_email(to_email, pdf_buffer, invoice_no):
    msg = EmailMessage()
    msg["From"] = f"{COMPANY_NAME} <{SMTP_EMAIL}>"
    msg["To"] = to_email
    msg["Subject"] = f"Invoice {invoice_no} | {COMPANY_NAME}"
    msg.set_content(f"Please find attached invoice {invoice_no}.")
    msg.add_attachment(pdf_buffer.read(), maintype="application", subtype="pdf", filename=f"{invoice_no}.pdf")

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(SMTP_EMAIL, SMTP_PASSWORD)
        server.send_message(msg)

# ======================================================
# ROUTE
# ======================================================
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":

        items = []
        subtotal = Decimal("0.00")

        descriptions = request.form.getlist("desc[]")
        quantities = request.form.getlist("qty[]")
        rates = request.form.getlist("rate[]")

        for d, q, r in zip(descriptions, quantities, rates):
            line_total = money(Decimal(q) * Decimal(r))
            subtotal += line_total
            items.append({
                "desc": d,
                "qty": q,
                "rate": r,
                "total": line_total
            })

        gst = money(subtotal * Decimal("0.18"))
        total = money(subtotal + gst)

        invoice_no = f"COG-{uuid.uuid4().hex[:6].upper()}"

        data = {
            "client_name": request.form.get("name"),
            "client_email": request.form.get("email")
        }

        pdf = create_invoice_pdf(data, invoice_no, items, subtotal, gst, total)

        supabase.table("invoices").insert({
            "invoice_no": invoice_no,
            "client_name": data["client_name"],
            "client_email": data["client_email"],
            "service": "Multiple Items",
            "amount": float(subtotal),
            "gst": float(gst),
            "total": float(total)
        }).execute()

        pdf.seek(0)
        send_invoice_email(OWNER_EMAIL, pdf, invoice_no)

        if data["client_email"]:
            pdf.seek(0)
            send_invoice_email(data["client_email"], pdf, invoice_no)

        return render_template("success.html", invoice_no=invoice_no)

    return render_template("index.html")

# ======================================================
# START
# ======================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)