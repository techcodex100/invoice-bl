import io, os, random, string, json
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import Response, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from PIL import Image
from datetime import datetime
from PyPDF2 import PdfReader

app = FastAPI(title="Invoice → Bill of Lading Generator")

# -------------------------------------------------------
# CORS
# -------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------------------------------------
# PDF GENERATION
# -------------------------------------------------------
def generate_bl_pdf(data: dict, template_path="image.jpeg") -> bytes:
    """Overlay extracted data onto Bill of Lading template."""
    buffer = io.BytesIO()
    bg_path = template_path if os.path.isabs(template_path) else os.path.join(os.path.dirname(__file__), template_path)
    try:
        if os.path.exists(bg_path):
            bg = Image.open(bg_path)
            w, h = bg.size
            c = canvas.Canvas(buffer, pagesize=(w, h))
            c.drawImage(ImageReader(bg), 0, 0, width=w, height=h)
            bg.close()
        else:
            from reportlab.lib.pagesizes import A4
            w, h = A4
            c = canvas.Canvas(buffer, pagesize=A4)
    except Exception:
        from reportlab.lib.pagesizes import A4
        w, h = A4
        c = canvas.Canvas(buffer, pagesize=A4)

    c.setFont("Helvetica", 9)

    def draw_wrapped(text, x, y, max_width):
        if not text: return
        words, lines, line = text.split(), [], ""
        for word in words:
            test = f"{line}{word} "
            if c.stringWidth(test, "Helvetica", 9) < max_width:
                line = test
            else:
                lines.append(line.strip())
                line = f"{word} "
        lines.append(line.strip())
        for i, l in enumerate(lines):
            c.drawString(x, y - (i * 11), l)

    # SHIPPER / CONSIGNEE / NOTIFY PARTY
    c.setFont("Helvetica-Bold", 10)
    c.drawString(70, h - 72, "EXPORTER / SHIPPER:")
    c.setFont("Helvetica", 9)
    draw_wrapped(data.get("exporter", ""), 70, h - 90, 350)

    c.setFont("Helvetica-Bold", 10)
    c.drawString(70, h - 200, "CONSIGNEE:")
    c.setFont("Helvetica", 9)
    draw_wrapped(data.get("consignee", ""), 70, h - 220, 350)

    c.setFont("Helvetica-Bold", 10)
    c.drawString(70, h - 300, "NOTIFY PARTY:")
    c.setFont("Helvetica", 9)
    draw_wrapped(data.get("notify_party", ""), 70, h - 320, 350)

    # PORTS & PRE-CARRIAGE
    c.setFont("Helvetica-Bold", 10)
    c.drawString(460, h - 460, "PORT OF LOADING:")
    c.setFont("Helvetica", 9)
    draw_wrapped(data.get("port_of_loading", ""), 460, h - 480, 350)

    c.setFont("Helvetica-Bold", 10)
    c.drawString(70, h - 510, "PORT OF DISCHARGE:")
    c.setFont("Helvetica", 9)
    draw_wrapped(data.get("port_of_discharge", ""), 70, h - 530, 350)

    c.setFont("Helvetica-Bold", 10)
    c.drawString(70, h - 460, "PLACE OF ACCEPTANCE:")
    c.setFont("Helvetica", 9)
    draw_wrapped(data.get("pre_carriage_by", ""), 160, h - 430, 300)

    # VESSEL / B/L NO.
    c.setFont("Helvetica-Bold", 10)
    c.drawString(70, h - 410, "VESSEL/VOYAGE:")
    c.setFont("Helvetica", 9)
    draw_wrapped(data.get("vessel_voyage", ""), 200, h - 410, 400)

    c.setFont("Helvetica-Bold", 10)
    c.drawString(450, h - 390, f"B/L NO.: {data.get('invoice_no', '')}")

    # GOODS
    left_box_x = 100
    desc_box_x = 330
    right_box_x = max(w - 200, 520)
    y_start = h - 590
    for i, good in enumerate(data.get("goods", [])):
        row_y = y_start - (i * 115)
        c.setFont("Helvetica", 9)
        sr_text = good.get('sr_marks') or ''
        if data.get('container_no') and data.get('seal_no'):
            sr_text = f"{sr_text}\nContainer & Seal nos.: {data.get('container_no')} / {data.get('seal_no')}".strip()
        if sr_text:
            draw_wrapped(sr_text, left_box_x, row_y, 200)
        draw_wrapped(good.get("description", ""), desc_box_x, row_y, 520)
        draw_wrapped(str(good.get("units_mt", "")), right_box_x, row_y, 80)
        draw_wrapped(str(good.get("rate", "")), right_box_x + 100, row_y, 80)

    # FOOTER
    c.setFont("Helvetica-Bold", 10)
    c.drawString(70, 110, "DELIVERY AGENT:")
    c.setFont("Helvetica", 9)
    draw_wrapped(data.get("delivery_agent", ""), 200, 110, 420)
    place = (data.get("port_of_loading") or data.get("place_of_receipt") or "").strip()
    today = datetime.now().strftime("%d-%m-%Y")
    c.setFont("Helvetica-Bold", 10)
    c.drawString(w - 250, 96, "PLACE & DATE:")
    c.setFont("Helvetica", 9)
    c.drawRightString(w - 70, 96, f"{place}  {today}")

    c.save()
    return buffer.getvalue()

# -------------------------------------------------------
# UTILITY: Read embedded JSON from PDF
# -------------------------------------------------------
def extract_json_from_pdf_bytes(pdf_bytes: bytes) -> dict:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        metadata = reader.metadata or {}
        json_raw = None
        for k, v in metadata.items():
            if "custom_json" in k.lower():
                json_raw = v
                break
        if not json_raw:
            raise HTTPException(404, "No embedded JSON metadata found in PDF")
        return json.loads(json_raw)
    except Exception as e:
        raise HTTPException(500, f"Error reading PDF: {str(e)}")

# -------------------------------------------------------
# API ROUTES
# -------------------------------------------------------
@app.post("/generate-bl-from-json/")
async def generate_bl_from_json(invoice_pdf: UploadFile = File(...)):
    if not invoice_pdf.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted")
    pdf_bytes = await invoice_pdf.read()
    data = extract_json_from_pdf_bytes(pdf_bytes)

    # Fill missing randomized fields if needed
    if not data.get("vessel_voyage"):
        vessels = ["MSC LORETO", "CMA CGM NEVADA", "APL TOKYO", "MAERSK OHIO", "ONE HAMBURG", "WAN HAI 528", "EVER GIVEN"]
        voyage_code = f"V.{random.randint(100,999)}{random.choice(list('ABCDE'))}"
        data["vessel_voyage"] = f"{random.choice(vessels)} {voyage_code}"
    if not data.get("container_no"):
        data["container_no"] = ''.join(random.choices(string.ascii_uppercase, k=4)) + ''.join(random.choices(string.digits, k=7))
    if not data.get("seal_no"):
        data["seal_no"] = ''.join(random.choices(string.digits, k=6))
    if not data.get("delivery_agent"):
        agents = [
            "SEA LINE LOGISTICS PTE. LTD., Singapore",
            "GULF STAR SHIPPING LLC, Dubai",
            "PACIFIC FREIGHT SERVICES, Singapore",
            "BLUE OCEAN LINES, Mumbai",
            "NORTH HARBOUR AGENCIES, Singapore"
        ]
        data["delivery_agent"] = random.choice(agents)

    bl_pdf = generate_bl_pdf(data, "image.jpeg")
    return Response(
        content=bl_pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=BL_{data.get('invoice_no','Unknown')}.pdf"}
    )

@app.post("/extract-json-from-pdf/")
async def extract_json_from_pdf(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted")
    pdf_bytes = await file.read()
    data = extract_json_from_pdf_bytes(pdf_bytes)
    return JSONResponse(content={"status": "success", "data": data})

@app.get("/")
def home():
    return {"message": "Upload your invoice PDF with embedded JSON to /generate-bl-from-json or /extract-json-from-pdf"}