import io, re, random, string, os
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import Response, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from PIL import Image
import pdfplumber
from pdf2image import convert_from_bytes
import pytesseract

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
# PDF TEXT EXTRACTION
# -------------------------------------------------------
def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extract text from a PDF using pdfplumber; fallback to OCR if scanned."""
    text = ""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                text += t + "\n"
    if not text.strip():  # OCR fallback
        for page_img in convert_from_bytes(pdf_bytes):
            text += pytesseract.image_to_string(page_img) + "\n"
    return text.strip()

# -------------------------------------------------------
# DATA EXTRACTION (Regex)
# -------------------------------------------------------
def extract_invoice_data(text: str) -> dict:
    """Extract structured invoice data using robust regex; fallback randoms for missing fields."""
    raw = text or ""
    FLAGS = re.IGNORECASE | re.DOTALL

    # Use the raw text for matching
    def find(pattern: str, source: str = None):
        s = raw if source is None else source
        m = re.search(pattern, s, FLAGS)
        try:
            return (m.group(1) or "").strip() if m else ""
        except Exception:
            return ""

    def extract_block(label_regex: str, next_labels_regex: str, source: str = None) -> str:
        s = raw if source is None else source
        pattern = rf'{label_regex}\s*[:\-]?\s*(.*?)(?={next_labels_regex}|$)'
        m = re.search(pattern, s, FLAGS)
        block = (m.group(1) or "").strip() if m else ""
        # restore line breaks where multiple spaces may exist
        block = re.sub(r'\n\s*', '\n', block)
        return block

    # Build a union of label starters for lookahead boundaries
    label_alts = [
        r'Invoice\s*No\.?', r'I\.?E\.?\s*Code', r"Buyer'?s\s*Order\s*No\.",
        r'Consignee', r'Notify\s*Party', r'Country', r'Pre-?Carriage', r'Vessel\s*\/?\s*Voyage',
        r'Place\s*of\s*Receipt', r'Port\s*of\s*Loading', r'Port\s*of\s*Discharge', r'Final\s*Destination',
        r'Terms\s*of\s*Payment', r'Amount\s*Chargeable', r'Total', r'BIN\s*NO', r'Drawback', r'Benefit', r'Shipment'
    ]
    boundary = r'(?=' + r'|'.join(label_alts) + r')'

    # 🧾 BASIC DETAILS
    invoice_no = find(r'Invoice\s*No\.?\s*[:\-]?\s*([A-Z0-9\-\/]+)')
    ie_code = find(r'I\.?E\.?\s*Code\s*No\.?\s*[:\-]?\s*([A-Z0-9]+)')
    po_no = find(r'Buyer.?s\s*Order\s*No\.?\s*[:\-]?\s*([A-Z0-9\-\/]+)')
    terms = find(r'Terms\s*of\s*Payment\s*[:\-]?\s*(.+?)(?:Country|Final|Port|$)')
    drawback_no = find(r'Drawback\s*Sr\.?\s*No\.?\s*[:\-]?\s*([A-Z0-9\-\/]+)')
    benefit_scheme = find(r'Benefit[s]?\s*under\s*ME[I|E]S\s*scheme\s*[:\-]?\s*([A-Za-z ]+)')
    total = find(r'(?:Amount\s*Chargeable|Total)\s*[:\-]?\s*(?:USD\s*)?([\d,]+(?:\.\d{2})?)')
    currency = "USD" if re.search(r'USD', raw, re.IGNORECASE) else "NOT FOUND"

    # 🏢 EXPORTER / CONSIGNEE / NOTIFY PARTY (multiline blocks)
    # Shipper is same as Exporter
    exporter_block = extract_block(r'Exporter|Shipper', boundary)
    consignee_block = extract_block(r'Consignee', boundary)
    notify_block = extract_block(r'Notify\s*Party', boundary)

    # Fallbacks: if blocks are empty, try broader spans between common labels
    if not exporter_block:
        m = re.search(r'(?:Exporter|Shipper)\s*:\s*([\s\S]{10,800}?)(?=Consignee|Notify\s*Party|Invoice\s*No\.?|Country|Pre-?Carriage|$)', raw, FLAGS)
        exporter_block = (m.group(1).strip() if m else "")
    if not consignee_block:
        m = re.search(r'Consignee\s*:\s*([\s\S]{10,800}?)(?=Notify\s*Party|Country|Pre-?Carriage|Invoice\s*No\.?|$)', raw, FLAGS)
        consignee_block = (m.group(1).strip() if m else "")
        # Additional fallback for consignee
        if not consignee_block:
            m = re.search(r'Consignee\s*[:\-]?\s*([\s\S]{10,500}?)(?=Notify|Country|Port|Vessel|$)', raw, FLAGS)
            consignee_block = (m.group(1).strip() if m else "")

    # Clean IE/IEC lines and heading labels out of shipper/exporter block
    if exporter_block:
        exporter_block = re.sub(r'(?mi)^.*\bI\.?E\.?C\.?\b.*$\n?', '', exporter_block)
        exporter_block = re.sub(r'(?mi)^.*\bI\.?E\.?\s*Code.*$\n?', '', exporter_block)
        # Remove Invoice No / Buyer's Order No lines if accidentally captured
        exporter_block = re.sub(r'(?mi)^.*\bInvoice\s*No\.?\b.*$\n?', '', exporter_block)
        exporter_block = re.sub(r"(?mi)^.*Buyer'?s\s*Order\s*No\.?\b.*$\n?", '', exporter_block)
        # Remove leading 'Exporter:'/'Shipper:' labels if present
        exporter_block = re.sub(r'(?mi)^(?:Exporter|Shipper)\s*:?\s*', '', exporter_block)
        # Collapse excessive blank lines
        exporter_block = re.sub(r'\n{3,}', '\n\n', exporter_block)
        exporter_block = exporter_block.strip()

    # Use full exporter block (name + address) for Bill of Lading
    exporter_name = exporter_block

    # Notify Party: mirror Consignee if missing or marked same as consignee
    if (not notify_block or re.search(r'same as consignee', notify_block, re.IGNORECASE)):
        notify_block = consignee_block
    
    # Use notify party data for consignee if consignee is empty
    if not consignee_block or consignee_block.strip() == "":
        consignee_block = notify_block

    # 🚢 SHIPMENT DETAILS
    pre_carriage = find(r'(?:Pre|Pre\-)?\s*Carriage\s*By\s*[:\-]?\s*([A-Za-z \-]+)') or find(r'Pre\-?Carriage\s*[:\-]?\s*([A-Za-z \-]+)')
    vessel_voyage = find(r'Vessel\s*\/?\s*Voyage\s*[:\-]?\s*([A-Za-z0-9 .\-\/]+)')
    # Use block extraction for all four header fields with robust lookahead boundaries
    por_block = extract_block(r'Place\s*of\s*receipt|Place\s*of\s*Acceptance', boundary)
    pl_block = extract_block(r'Port\s*of\s*Loading|Port\s*of\s*Shipment', boundary)
    pd_block = extract_block(r'Port\s*of\s*Discharge', boundary)
    podl_block = extract_block(r'Place\s*of\s*Delivery|Final\s*Destination', boundary)
    # Specific regex patterns for known ports (use capturing groups so finder returns value)
    pol_direct = (
        find(r'(Nhava\s*Sheva|Nava\s*Sheva|Nahava\s*Seva|JNPT)') or
        find(r'Port\s*of\s*Loading\s*[:\-]?\s*([^\n]+)') or 
        find(r'Port\s*of\s*Shipment\s*[:\-]?\s*([^\n]+)') or
        find(r'Loading\s*Port\s*[:\-]?\s*([^\n]+)') or
        find(r'POL\s*[:\-]?\s*([^\n]+)')
    )
    pod_direct = (
        find(r'(Singapore)') or
        find(r'Port\s*of\s*Discharge\s*[:\-]?\s*([^\n]+)') or 
        find(r'Port\s*of\s*Delivery\s*[:\-]?\s*([^\n]+)') or
        find(r'Discharge\s*Port\s*[:\-]?\s*([^\n]+)') or
        find(r'POD\s*[:\-]?\s*([^\n]+)')
    )
    def first_line(val: str) -> str:
        if not val:
            return ''
        for line in val.split('\n'):
            s = line.strip(' :\t')
            if s:
                return s
        return ''
    place_receipt = (first_line(por_block) or find(r'(?:Place|Place\s*of)\s*of?\s*Receipt\s*[:\-]?\s*([^\n]+)') or find(r'Place\s*of\s*Acceptance\s*[:\-]?\s*([^\n]+)'))
    port_loading = (pol_direct or first_line(pl_block))
    port_discharge = (pod_direct or first_line(pd_block))
    final_destination = (first_line(podl_block) or find(r'Final\s*Destination\s*[:\-]?\s*([^\n]+)') or find(r'Place\s*of\s*Delivery\s*[:\-]?\s*([^\n]+)'))
    country_origin = find(r'Country\s*of\s*Origin\s*[:\-]?\s*([A-Za-z ,]+)')
    country_destination = find(r'Country\s*of\s*Final\s*Destination\s*[:\-]?\s*([A-Za-z ,]+)')
    # Container & Seal (fallback from raw text)
    cont_seal_match = re.search(r'Container\s*&\s*Seal\s*nos?\.?\s*[:\-]?\s*([A-Z0-9\/\-]+)\s*[\/\-\| ]\s*([A-Z0-9]+)', raw, FLAGS)
    if cont_seal_match:
        container_no = cont_seal_match.group(1).strip()
        seal_no = cont_seal_match.group(2).strip()

    # 📦 GOODS EXTRACTION
    goods_matches = re.findall(
        r'HS\s*CODE\s*:\s*([0-9\.]+)[\s\S]*?QUANTITY\s*:\s*([0-9,]+\s*PCS)[\s\S]*?WEIGHT\s*:\s*([0-9,\.]+\s*KGS?)[\s\S]*?PACKING\s*:\s*([0-9,]+\s*CARTONS?)',
        text, FLAGS
    )

    goods = []
    # Attempt to capture Sr No & Marks / Containers block (optional)
    sr_marks_block_match = re.search(r'(\d+\s*X\s*20[\'\’\″\”\"]?\s*FCL[\s\S]*?Container\s*&\s*Seal\s*nos\.?\s*:\s*[\s\S]*?)(?:INDIAN|HS\s*CODE|Description|NO\.|$)', raw, FLAGS)
    sr_marks_block = sr_marks_block_match.group(1).strip() if sr_marks_block_match else ""
    # If not found, try a simpler pattern like "06 X 20' FCL ..."
    if not sr_marks_block:
        simple_fcl = re.search(r'(\d+\s*X\s*20[\'\’\″\”\"]?\s*FCL[^\n]*?)', raw, FLAGS)
        if simple_fcl:
            sr_marks_block = simple_fcl.group(1).strip()
    # If still not found but container/seal extracted, compose sr_marks text
    if not sr_marks_block and (locals().get('container_no') and locals().get('seal_no')):
        sr_marks_block = f"Container & Seal nos.: {container_no} / {seal_no}"
    # Clean sr_marks to avoid product/description lines leaking into left column
    if sr_marks_block:
        sr_lines = [ln.strip() for ln in re.split(r'[\r\n]+', sr_marks_block) if ln.strip()]
        allowed_patterns = [r'\bFCL\b', r'Container', r'Seal', r'Marks', r'Packages?', r'Pkg', r'Cartons?', r'\bNo\.?']
        def is_allowed(line: str) -> bool:
            return any(re.search(p, line, re.IGNORECASE) for p in allowed_patterns)
        sr_filtered = []
        for ln in sr_lines:
            if re.search(r'\d+\s*X\s*20[\'\’\″\”\"]?\s*FCL', ln, re.IGNORECASE):
                m = re.search(r'(\d+\s*X\s*20[\'\’\″\”\"]?\s*FCL)', ln, re.IGNORECASE)
                if m:
                    sr_filtered.append(m.group(1))
                continue
            if is_allowed(ln):
                sr_filtered.append(ln)
        sr_marks_block = "\n".join(sr_filtered).strip()

    # Capture Units (In Metric Tons), Rate Per Unit (USD), Amount (USD) from headers if present
    units_mt = find(r'NO\.\s*OF\s*UNITS\s*\(In\s*Metric\s*Tons\)\s*([0-9,.]+)')
    if units_mt == "NOT FOUND":
        # fallback to NET WEIGHT value in MTS
        units_mt = find(r'TOTAL\s*NET\s*WEIGHT\s*[:\-]?\s*([0-9,.]+)\s*MTS?')

    rate_per_unit = find(r'RATE\s*PER\s*UNIT\s*\(USD\)\s*([0-9,.]+)')
    amount_usd = find(r'Amount\s*\(USD\)\s*([0-9,.]+)')
    total_net_wt = find(r'TOTAL\s*NET\s*WEIGHT\s*[:\-]?\s*([0-9,.]+)\s*(?:MTS?|KGS?)')
    total_gross_wt = find(r'TOTAL\s*GROSS\s*WEIGHT\s*[:\-]?\s*([0-9,.]+)\s*(?:MTS?|KGS?)')
    measurement_cbm = find(r'MEASUREMENT\s*[:\-]?\s*([0-9,.]+\s*CBM)')
    if amount_usd == "NOT FOUND":
        amount_usd = find(r'Total\s*[:\-]?\s*([0-9,.]+)')
    for match in goods_matches:
        hs, qty, weight, pack = match
        desc_match = re.search(rf'HS\s*CODE\s*:\s*{re.escape(hs)}\s*([\s\S]*?)\s*QUANTITY', raw, FLAGS)
        try:
            desc_raw = desc_match.group(1) if desc_match else ""
        except Exception:
            desc_raw = ""
        # Try to capture 1-2 lines just before the HS CODE (product names)
        pre_hs_name = ""
        pre_hs_context = re.search(rf'([A-Z0-9 ,\-\/()]{8,240})\s*[\r\n]+\s*HS\s*CODE\s*:\s*{re.escape(hs)}', raw, FLAGS)
        if pre_hs_context:
            pre_hs_name = pre_hs_context.group(1).strip()
        # Preserve content and newlines (trim excessive length)
        combined_desc = (pre_hs_name + "\n" + (desc_raw or "")).strip()
        if combined_desc:
            combined_desc = combined_desc[:2200]
            desc = re.sub(r'[ \t]{2,}', ' ', combined_desc).strip()
        else:
            desc = ""
        desc_full = desc
        # Append totals to description body as lines
        if total_net_wt or total_gross_wt:
            tail = []
            if total_net_wt:
                tail.append(f"TOTAL NET WEIGHT: {total_net_wt} MTS")
            if total_gross_wt:
                tail.append(f"TOTAL GROSS WEIGHT: {total_gross_wt} MTS")
            desc_full = (desc + "\n" + "\n".join(tail)).strip()
        # Prepend HS CODE to description per request
        if hs:
            desc_full = (f"HS CODE: {hs}\n" + desc_full).strip()
        # Compose weight & measurements text for right column
        wm_lines = []
        if total_net_wt:
            wm_lines.append(f"NET: {total_net_wt} MTS")
        if total_gross_wt:
            wm_lines.append(f"GROSS: {total_gross_wt} MTS")
        if measurement_cbm and measurement_cbm != "NOT FOUND":
            wm_lines.append(f"MEASUREMENT: {measurement_cbm}")
        # If totals missing, fallback to WEIGHT field captured from goods section
        if not wm_lines and weight:
            wm_lines.append(f"WEIGHT: {weight}")
        weight_measurements = "\n".join(wm_lines)
        goods.append({
            "hs_code": hs,
            "description": desc_full,
            "quantity": qty,
            "weight": weight,
            "packing": pack,
            "unit": "PCS",
            "rate": rate_per_unit if rate_per_unit != "NOT FOUND" else "",
            "amount": amount_usd if amount_usd != "NOT FOUND" else "",
            "units_mt": units_mt if units_mt != "NOT FOUND" else "",
            "weight_measurements": weight_measurements,
            "sr_marks": sr_marks_block
        })

    # Fallback: common invoice layout (as in your screenshot)
    # Capture description block around HS CODE and TOTAL NET WEIGHT lines
    if not goods:
        hs_only = re.search(r'HS\s*CODE\s*[:\-]?\s*([0-9\.]{6,10})', raw, FLAGS)
        total_net_match = re.search(r'TOTAL\s*NET\s*WEIGHT\s*[:\-]?\s*([0-9,.]+)\s*MTS?', raw, FLAGS)
        # Description block with larger capture and preserving newlines
        desc_after_hs_match = re.search(
            r'HS\s*CODE\s*[:\-]?\s*[0-9\.]{6,10}\s*([\s\S]{0,3000}?)(?:TOTAL\s*NET\s*WEIGHT|TOTAL\s*GROSS\s*WEIGHT|Amount\s*Chargeable|BIN\s*NO|DECLARATION|RATE\s*PER|NO\.?\s*OF|$)',
            raw, FLAGS
        )
        desc_after_hs = desc_after_hs_match.group(1) if desc_after_hs_match else ""
        pre_hs_match = re.search(
            r'([A-Z0-9 ,\-\/()]{8,240})\s*\n\s*HS\s*CODE',
            raw, FLAGS
        )
        pre_hs = pre_hs_match.group(1) if pre_hs_match else ""
        desc_block = (pre_hs + "\n" + desc_after_hs).strip()
        desc_block = re.sub(r'[ \t]{2,}', ' ', desc_block)
        units_guess = (total_net_match.group(1) if total_net_match else find(r'([0-9,.]+)\s*MTS?'))
        amount_guess = find(r'Total\s*:?\s*([0-9,.]+)')
        desc_full = desc_block
        if total_net_wt or total_gross_wt:
            tail = []
            if total_net_wt:
                tail.append(f"TOTAL NET WEIGHT: {total_net_wt} MTS")
            if total_gross_wt:
                tail.append(f"TOTAL GROSS WEIGHT: {total_gross_wt} MTS")
            desc_full = (desc_block + "\n" + "\n".join(tail)).strip()
        # Prepend HS code when available
        hs_code_val = (hs_only.group(1) if hs_only else find(r'HS\s*CODE\s*:?\s*([0-9\.]+)'))
        if hs_code_val:
            desc_full = (f"HS CODE: {hs_code_val}\n" + desc_full).strip()
        # Compose weight & measurements
        wm_lines = []
        if total_net_wt:
            wm_lines.append(f"NET: {total_net_wt} MTS")
        if total_gross_wt:
            wm_lines.append(f"GROSS: {total_gross_wt} MTS")
        if measurement_cbm and measurement_cbm != "NOT FOUND":
            wm_lines.append(f"MEASUREMENT: {measurement_cbm}")
        weight_measurements_fb = "\n".join(wm_lines)
        goods.append({
            "hs_code": hs_code_val,
            "description": desc_full,
            "quantity": "",
            "weight": "",
            "packing": "",
            "unit": "",
            "rate": rate_per_unit if rate_per_unit != "NOT FOUND" else "",
            "amount": amount_guess if amount_guess else "",
            "units_mt": units_guess if units_guess else "",
            "weight_measurements": weight_measurements_fb,
            "sr_marks": sr_marks_block
        })

    # ✨ RANDOM DEFAULTS FOR MISSING FIELDS
    vessels = ["MSC LORETO", "CMA CGM NEVADA", "APL TOKYO", "MAERSK OHIO", "ONE HAMBURG", "WAN HAI 528", "EVER GIVEN"]
    # Always randomize vessel/voyage per request
    voyage_code = f"V.{random.randint(100,999)}{random.choice(list('ABCDE'))}"
    vessel_voyage = f"{random.choice(vessels)} {voyage_code}"

    container_no = ''.join(random.choices(string.ascii_uppercase, k=4)) + ''.join(random.choices(string.digits, k=7))
    seal_no = ''.join(random.choices(string.digits, k=6))

    # Delivery Agent: random always as requested
    agents = [
        "SEA LINE LOGISTICS PTE. LTD., Singapore",
        "GULF STAR SHIPPING LLC, Dubai",
        "PACIFIC FREIGHT SERVICES, Singapore",
        "BLUE OCEAN LINES, Mumbai",
        "NORTH HARBOUR AGENCIES, Singapore"
    ]
    delivery_agent = random.choice(agents)

    # ✅ STRUCTURED OUTPUT
    return {
        "invoice_no": invoice_no,
        "ie_code": ie_code,
        "po_no": po_no,
        "exporter": exporter_block,
        "exporter_name": exporter_name,
        "consignee": consignee_block,
        "notify_party": notify_block,
        "country_of_origin": country_origin,
        "country_of_final_destination": country_destination,
        "terms": terms,
        "terms_of_payment": terms,
        "drawback_no": drawback_no,
        "benefit_scheme": benefit_scheme,
        "total_amount": total,
        "currency": currency,
        "pre_carriage_by": pre_carriage,
        "vessel_voyage": vessel_voyage,
        "place_of_receipt": place_receipt,
        "place_of_acceptance": (port_loading or place_receipt),
        "port_of_loading": port_loading,
        "port_of_discharge": port_discharge,
        "place_of_delivery": (port_discharge or final_destination),
        "final_destination": final_destination,
        "container_no": container_no,
        "seal_no": seal_no,
        "goods": goods if goods else [],
        "delivery_agent": delivery_agent,
    }

# -------------------------------------------------------
# PDF GENERATION
# -------------------------------------------------------
def generate_bl_pdf(data: dict, template_path="image.jpeg") -> bytes:
    """Overlay extracted data onto Bill of Lading template."""
    buffer = io.BytesIO()
    # Resolve background path safely; fallback if missing
    bg_path = template_path if os.path.isabs(template_path) else os.path.join(os.path.dirname(__file__), template_path)
    c = None
    bg = None
    try:
        if os.path.exists(bg_path):
            bg = Image.open(bg_path)
            w, h = bg.size
            c = canvas.Canvas(buffer, pagesize=(w, h))
            c.drawImage(ImageReader(bg), 0, 0, width=w, height=h)
        else:
            from reportlab.lib.pagesizes import A4
            w, h = A4
            c = canvas.Canvas(buffer, pagesize=A4)
    except Exception:
        from reportlab.lib.pagesizes import A4
        w, h = A4
        c = canvas.Canvas(buffer, pagesize=A4)
    finally:
        if bg is not None:
            try:
                bg.close()
            except Exception:
                pass
    c.setFont("Helvetica", 9)

    def draw_wrapped(text, x, y, max_width):
        if not text: return
        # Support multi-paragraph text (\n separated)
        paragraphs = re.split(r"\r?\n", text)
        line_offset = 0
        for para in paragraphs:
            if not para:
                line_offset += 11
                continue
            words, lines, line = para.split(), [], ""
            for word in words:
                test = f"{line}{word} "
                if c.stringWidth(test, "Helvetica", 9) < max_width:
                    line = test
                else:
                    lines.append(line.strip())
                    line = f"{word} "
            lines.append(line.strip())
            for l in lines:
                c.drawString(x, y - line_offset, l)
                line_offset += 11

    # SHIPPER
    c.setFont("Helvetica-Bold", 10)
    c.drawString(70, h - 72, "EXPORTER / SHIPPER:")
    c.setFont("Helvetica", 9)
    draw_wrapped(data.get("exporter_name", data.get("exporter", "")), 70, h - 90, 350)

    # CONSIGNEE
    c.setFont("Helvetica-Bold", 10)
    c.drawString(70, h - 200, "CONSIGNEE:")
    c.setFont("Helvetica", 9)
    draw_wrapped(data.get("consignee", ""), 70, h - 220, 350)

    # NOTIFY PARTY
    c.setFont("Helvetica-Bold", 10)
    c.drawString(70, h - 300, "NOTIFY PARTY:")
    c.setFont("Helvetica", 9)
    draw_wrapped(data.get("notify_party", ""), 70, h - 320, 350)

    # PORTS
    # PLACE OF ACCEPTANCE
    c.setFont("Helvetica-Bold", 10)
    c.drawString(70, h - 460, "PLACE OF ACCEPTANCE:")
    c.setFont("Helvetica", 9)
    draw_wrapped(data.get("port_of_loading", ""), 70, h - 480, 350)

    c.setFont("Helvetica-Bold", 10)
    c.drawString(460, h - 460, "PORT OF LOADING:")
    c.setFont("Helvetica", 9)
    draw_wrapped(data.get("port_of_loading", ""), 460, h - 480, 350)

    c.setFont("Helvetica-Bold", 10)
    c.drawString(70, h - 510, "PORT OF DISCHARGE:")
    c.setFont("Helvetica", 9)
    draw_wrapped(data.get("port_of_discharge", ""), 70, h - 530, 350)
    # Final Destination value suppressed in header area (per request)

    c.setFont("Helvetica-Bold", 10)
    c.drawString(460, h - 510, "PLACE OF DELIVERY:")
    c.setFont("Helvetica", 9)
    draw_wrapped(data.get("port_of_discharge", ""), 460, h - 530, 350)

    # Removed white rectangles as requested

    # VESSEL / B/L NO.
    c.setFont("Helvetica-Bold", 10)
    c.drawString(70, h - 410, "VESSEL/VOYAGE:")
    c.setFont("Helvetica", 9)
    draw_wrapped(data.get("vessel_voyage", ""), 200, h - 410, 400)

    c.setFont("Helvetica-Bold", 10)
    c.drawString(450, h - 390, f"B/L NO.: {data.get('invoice_no', '')}")

    # GOODS table boxes mapping (tuned positions)
    left_box_x = 100          # fine tune columns
    desc_box_x = 330
    right_box_x = max(w - 200, 520)
    y_start = h - 590
    for i, good in enumerate(data.get("goods", [])):
        row_y = y_start - (i * 115)
        c.setFont("Helvetica", 9)
        # Sr No & Marks – left column
        sr_text = good.get('sr_marks') or ''
        # Always include container & seal if available
        if data.get('container_no') and data.get('seal_no'):
            extra = f"Container & Seal nos.: {data.get('container_no')} / {data.get('seal_no')}"
            if extra not in sr_text:
                sr_text = f"{sr_text}\n{extra}".strip()
        if sr_text:
            draw_wrapped(sr_text, left_box_x, row_y, 200)
        # Description of Goods – middle column (narrower width, multiline safe)
        draw_wrapped(good.get('description',''), desc_box_x, row_y, 430)
        # Numeric columns – units and rate only (no amount in this column)
        units_x = right_box_x
        rate_x = units_x + 100
        draw_wrapped(str(good.get('units_mt','')), units_x, row_y, 80)
        draw_wrapped(str(good.get('rate','')), rate_x, row_y, 80)
        # Weight & Measurements details in the far-right narrow column
        wm = good.get('weight_measurements','')
        if wm:
            wm_x = max(w - 155, rate_x + 120)
            draw_wrapped(wm, wm_x, row_y, 140)

    # No explicit total amount printed to keep Weight & Measurements column clean

    # Footer box: Delivery Agent (left) and Place & Date (right) – move up
    # Position headings exactly inside the footer boxes to avoid overlap
    c.setFont("Helvetica-Bold", 10)
    c.drawString(70, 110, "DELIVERY AGENT:")
    c.setFont("Helvetica", 9)
    draw_wrapped(data.get("delivery_agent", ""), 200, 110, 420)

    from datetime import datetime
    place = (data.get("port_of_loading") or data.get("place_of_receipt") or "").strip()
    today = datetime.now().strftime("%d-%m-%Y")
    c.setFont("Helvetica-Bold", 10)
    c.drawString(w - 250, 96, "PLACE & DATE:")
    c.setFont("Helvetica", 9)
    c.drawRightString(w - 70, 96, f"{place}  {today}")

    c.save()
    return buffer.getvalue()

# -------------------------------------------------------
# API ROUTES
# -------------------------------------------------------
@app.post("/generate-bl/")
async def generate_bl(invoice_pdf: UploadFile = File(...)):
    if not invoice_pdf.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted")

    pdf_bytes = await invoice_pdf.read()
    text = extract_text_from_pdf(pdf_bytes)
    if not text:
        raise HTTPException(422, "No readable text found in PDF")

    data = extract_invoice_data(text)
    # Debug: Print extracted port data
    print(f"DEBUG - Port of Loading: '{data.get('port_of_loading', 'NOT_FOUND')}'")
    print(f"DEBUG - Port of Discharge: '{data.get('port_of_discharge', 'NOT_FOUND')}'")
    print(f"DEBUG - Exporter Name: '{data.get('exporter_name', 'NOT_FOUND')}'")
    print(f"DEBUG - Exporter Block: '{data.get('exporter', 'NOT_FOUND')}'")
    print(f"DEBUG - Consignee: '{data.get('consignee', 'NOT_FOUND')}'")
    print(f"DEBUG - Raw text sample: '{text[:500]}'")
    print(f"DEBUG - Raw text length: {len(text)}")
    # Check for specific patterns in raw text
    if 'Port' in text:
        print("DEBUG - 'Port' found in text")
    if 'Loading' in text:
        print("DEBUG - 'Loading' found in text") 
    if 'Discharge' in text:
        print("DEBUG - 'Discharge' found in text")
    bl_pdf = generate_bl_pdf(data, "image.jpeg")

    return Response(
        content=bl_pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=BL_{data.get('invoice_no','Unknown')}.pdf"}
    )

@app.post("/generate-bl-json/")
async def generate_bl_json(invoice_pdf: UploadFile = File(...)):
    if not invoice_pdf.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted")

    pdf_bytes = await invoice_pdf.read()
    text = extract_text_from_pdf(pdf_bytes)
    if not text:
        raise HTTPException(422, "No readable text found in PDF")

    data = extract_invoice_data(text)
    return JSONResponse(content=data)

@app.get("/")
def home():
    return {"message": "Upload your invoice PDF to /generate-bl or /generate-bl-json"}