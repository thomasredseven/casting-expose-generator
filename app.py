# app.py - Casting Exposé Generator v5.0
# Mit PDF Re-Import (Metadaten + eingebettete Fotos)

import streamlit as st
import google.generativeai as genai
from PIL import Image, ExifTags
import io
import time
import re
import os
import json
import base64
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
import fitz
from docx import Document

# --- Konfiguration ---
st.set_page_config(page_title="Casting Exposé Generator", page_icon="🎬", layout="wide")

COLORS = {
    'title_green': (78, 124, 35),
    'section_header_bg': (101, 148, 58),
    'text_dark': (51, 51, 51),
}

APP_VERSION = "5.0"
PDF_MARKER = "CASTING_EXPOSE_GENERATOR"


# --- Passwort ---
def check_password():
    correct = st.secrets.get("APP_PASSWORD", "castinggarten")
    if st.session_state.get("authenticated"):
        return True
    
    c1, c2, c3 = st.columns([1, 2, 1])
    with c2:
        st.markdown("### 🎬 Casting Exposé Generator")
        st.markdown("---")
        pw = st.text_input("Passwort", type="password", placeholder="Passwort...")
        if st.button("🔓 Anmelden", type="primary", use_container_width=True):
            if pw == correct:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("❌ Falsches Passwort!")
    return False


def load_description():
    if os.path.exists("description.md"):
        with open("description.md", "r", encoding="utf-8") as f:
            return f.read()
    return "## Willkommen!\nDieses Tool erstellt Exposés aus Casting-Unterlagen."


if not check_password():
    st.stop()

# --- Gemini Setup ---
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-3-flash-preview")

# --- Prompts ---
EXTRACTION_PROMPT = """
Analysiere diese Casting-Unterlagen und erstelle ein Exposé im MARKDOWN-FORMAT.

# FAMILIENNAME | ORT

## Familienmitglieder
- **Vorname** (Alter), Beruf
- **Vorname** (Alter), Beruf

## Fakten zum Garten
- Größe: X m²
- Weitere Fakten

## Budget
X.XXX €

## Wünsche für den Garten
- Wunsch 1
- Wunsch 2

## Die Familie
Kurzer Fließtext über die Familie.

## Notizen
Termine, Besonderheiten (nur wenn vorhanden)

REGELN: Erste Zeile # NAME | ORT, Sections ##, Namen **fett**, kurz, Deutsch.
"""

SINGLE_IMAGE_PROMPT = "Extrahiere alle Informationen. Kurz, stichpunktartig, Deutsch."

COMBINE_PROMPT = """
Kombiniere zu EINEM Exposé im MARKDOWN-FORMAT:

{extracted_infos}

# FAMILIENNAME | ORT
## Familienmitglieder
## Fakten zum Garten
## Budget
## Wünsche für den Garten
## Die Familie
## Notizen

Kurz! Keine Duplikate!
"""

PHOTO_ANALYSIS_PROMPT = "Kategorisiere: NUMMER|KATEGORIE|BESCHREIBUNG. Kategorien: FAMILIE, GARTEN, HAUS, SONSTIGES"


# =============================================================
# Hilfsfunktionen
# =============================================================

def fix_image_orientation(image):
    try:
        exif = image._getexif()
        if not exif:
            return image
        for key, value in ExifTags.TAGS.items():
            if value == 'Orientation':
                o = exif.get(key, 1)
                ops = {
                    2: lambda i: i.transpose(Image.FLIP_LEFT_RIGHT),
                    3: lambda i: i.rotate(180, expand=True),
                    4: lambda i: i.transpose(Image.FLIP_TOP_BOTTOM),
                    5: lambda i: i.transpose(Image.FLIP_LEFT_RIGHT).rotate(270, expand=True),
                    6: lambda i: i.rotate(270, expand=True),
                    7: lambda i: i.transpose(Image.FLIP_LEFT_RIGHT).rotate(90, expand=True),
                    8: lambda i: i.rotate(90, expand=True),
                }
                if o in ops:
                    return ops[o](image)
                break
        return image
    except:
        return image


def compress_image(image, max_size=800):
    image = fix_image_orientation(image)
    ratio = min(max_size / image.width, max_size / image.height)
    if ratio < 1:
        image = image.resize((int(image.width * ratio), int(image.height * ratio)), Image.LANCZOS)
    if image.mode in ('RGBA', 'P'):
        image = image.convert('RGB')
    return image


def crop_to_square(image):
    w, h = image.size
    size = min(w, h)
    left, top = (w - size) // 2, (h - size) // 2
    return image.crop((left, top, left + size, top + size))


def image_to_bytes(image, fmt='JPEG', quality=85):
    buf = io.BytesIO()
    image.save(buf, format=fmt, quality=quality)
    buf.seek(0)
    return buf.read()


def bytes_to_image(data):
    return Image.open(io.BytesIO(data))


def get_image_hash(image, hash_size=8):
    img = fix_image_orientation(image.copy()).convert('L').resize((hash_size + 1, hash_size), Image.LANCZOS)
    pixels = list(img.getdata())
    return tuple(pixels[r * (hash_size + 1) + c] > pixels[r * (hash_size + 1) + c + 1]
                 for r in range(hash_size) for c in range(hash_size))


def find_duplicates(images, threshold=10):
    hashes = [get_image_hash(img) for img in images]
    dups = set()
    for i in range(len(hashes)):
        for j in range(i + 1, len(hashes)):
            if sum(a != b for a, b in zip(hashes[i], hashes[j])) < threshold:
                dups.add(j)
    return dups


def extract_text_from_pdf(pdf_file):
    doc = fitz.open(stream=pdf_file.read(), filetype="pdf")
    text = "".join(page.get_text() for page in doc)
    doc.close()
    return text


def extract_text_from_docx(docx_file):
    return "\n".join(para.text for para in Document(docx_file).paragraphs)


def wait_with_countdown(seconds, msg="Warte"):
    if seconds <= 0:
        return
    bar, txt = st.progress(0), st.empty()
    for i in range(seconds):
        txt.text(f"⏱️ {msg}... {seconds - i}s")
        bar.progress((i + 1) / seconds)
        time.sleep(1)
    txt.empty()
    bar.empty()


def is_rate_limit_error(e):
    return any(x in str(e).lower() for x in ["429", "quota", "rate", "limit"])


def get_retry_delay(e):
    m = re.search(r'retry_delay.*?(\d+)', str(e))
    return int(m.group(1)) + 5 if m else 30


def call_gemini(contents):
    return model.generate_content(contents).text


def call_gemini_with_retry(contents, retries=3):
    for attempt in range(retries):
        try:
            return call_gemini(contents)
        except Exception as e:
            if is_rate_limit_error(e) and attempt < retries - 1:
                wait_with_countdown(get_retry_delay(e))
            else:
                raise


def analyze_photos(images):
    if not images:
        return [], None, []
    dups = find_duplicates(images)
    unique = [i for i in range(len(images)) if i not in dups]
    try:
        resp = call_gemini_with_retry([PHOTO_ANALYSIS_PROMPT] + [images[i] for i in unique])
        cats, fam = {}, None
        for line in resp.strip().split('\n'):
            if '|' in line:
                parts = line.split('|')
                try:
                    idx = int(parts[0].strip()) - 1
                    cat = parts[1].strip().upper()
                    if idx < len(unique):
                        ri = unique[idx]
                        cats[ri] = cat
                        if cat == 'FAMILIE' and fam is None:
                            fam = ri
                except:
                    pass
        garden = [i for i in unique if cats.get(i) != 'FAMILIE']
        return garden, fam, list(dups)
    except:
        return unique, None, list(dups)


def process_adaptive(images, names, text="", delay=0):
    n = len(images)
    if n == 0:
        return call_gemini_with_retry([EXTRACTION_PROMPT + "\n\n" + text]) if text else None
    if n == 1:
        st.info("📤 Verarbeite...")
        c = [EXTRACTION_PROMPT, images[0]]
        if text:
            c.append(f"\n\nZusatzinfos:\n{text}")
        return call_gemini_with_retry(c)
    
    st.info(f"🚀 Stufe 1: Alle {n} Dokumente...")
    try:
        c = [EXTRACTION_PROMPT]
        if text:
            c.append(f"\n\nZusatzinfos:\n{text}\n\n")
        c.extend(["Dokumente:"] + images)
        return call_gemini(c)
    except Exception as e:
        if is_rate_limit_error(e):
            wait_with_countdown(min(get_retry_delay(e), 30))
        else:
            raise
    
    if n > 3:
        st.info("📦 Stufe 2...")
        try:
            parts = []
            for i in range(0, n, 3):
                r = call_gemini_with_retry([SINGLE_IMAGE_PROMPT] + images[i:i+3])
                if r:
                    parts.append(r)
                if delay:
                    wait_with_countdown(delay)
            ai = "\n\n".join(parts)
            if text:
                ai = f"Zusatzinfos:\n{text}\n\n{ai}"
            return call_gemini_with_retry([COMBINE_PROMPT.format(extracted_infos=ai)])
        except Exception as e:
            if is_rate_limit_error(e):
                wait_with_countdown(min(get_retry_delay(e), 30))
            else:
                raise
    
    st.info("🐢 Stufe 3...")
    parts = []
    for img in images:
        r = call_gemini_with_retry([SINGLE_IMAGE_PROMPT, img])
        if r:
            parts.append(r)
        if delay:
            wait_with_countdown(max(delay, 5))
    ai = "\n\n".join(parts)
    if text:
        ai = f"Zusatzinfos:\n{text}\n\n{ai}"
    return call_gemini_with_retry([COMBINE_PROMPT.format(extracted_infos=ai)])


# =============================================================
# Markdown Parser
# =============================================================

def parse_markdown_content(content):
    blocks, title, current = [], {'name': 'FAMILIE', 'city': 'ORT'}, None
    for line in content.strip().split('\n'):
        line = line.strip()
        if not line or line == '---':
            continue
        if line.startswith('# ') and not line.startswith('## '):
            txt = line[2:].strip()
            if '|' in txt:
                p = txt.split('|')
                title['name'] = p[0].strip()
                title['city'] = p[1].strip() if len(p) > 1 else ''
            else:
                title['name'] = txt
            continue
        if line.startswith('## '):
            if current:
                blocks.append(current)
            current = {'type': 'section', 'title': line[3:].strip(), 'items': []}
            continue
        if (line.startswith('- ') or line.startswith('* ')) and current:
            current['items'].append({'type': 'bullet', 'text': line[2:].strip()})
            continue
        if current:
            current['items'].append({'type': 'text', 'text': line})
    if current:
        blocks.append(current)
    return title, blocks


# =============================================================
# PDF Erstellung
# =============================================================

def draw_rounded_rect(c, x, y, w, h, r, color, alpha=0.88):
    c.saveState()
    c.setFillColorRGB(color[0]/255, color[1]/255, color[2]/255, alpha)
    c.roundRect(x, y, w, h, r, fill=1, stroke=0)
    c.restoreState()


def draw_section_header(c, x, y, text):
    c.saveState()
    c.setFont("Helvetica-Bold", 10)
    w = c.stringWidth(text, "Helvetica-Bold", 10) + 14
    c.setFillColorRGB(COLORS['section_header_bg'][0]/255, COLORS['section_header_bg'][1]/255, COLORS['section_header_bg'][2]/255)
    c.roundRect(x, y - 14, w, 18, 3, fill=1, stroke=0)
    c.setFillColorRGB(1, 1, 1)
    c.drawString(x + 7, y - 9, text)
    c.restoreState()


def wrap_text(c, text, max_w, font="Helvetica", size=9):
    c.setFont(font, size)
    words = text.split()
    lines, cur = [], ""
    for word in words:
        test = cur + " " + word if cur else word
        if c.stringWidth(test, font, size) < max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def draw_text_with_bold(c, text, x, y):
    parts = re.split(r'(\*\*[^*]+\*\*)', text)
    cx = x
    for part in parts:
        if not part:
            continue
        if part.startswith('**') and part.endswith('**'):
            c.setFont("Helvetica-Bold", 9)
            t = part[2:-2]
            c.drawString(cx, y, t)
            cx += c.stringWidth(t, "Helvetica-Bold", 9)
        else:
            c.setFont("Helvetica", 9)
            c.drawString(cx, y, part)
            cx += c.stringWidth(part, "Helvetica", 9)
    c.setFont("Helvetica", 9)


def calc_block_height(c, block, w):
    lines = sum(len(wrap_text(c, it['text'].replace('**', ''), w - 35)) for it in block['items'])
    return 34 + lines * 13 + 10


def draw_block(c, block, x, y, w):
    h = calc_block_height(c, block, w)
    draw_rounded_rect(c, x, y - h, w, h, 8, (255, 255, 255), 0.88)
    draw_section_header(c, x + 5, y - 5, block['title'] + ":")
    ty = y - 36
    c.setFillColorRGB(0.2, 0.2, 0.2)
    for item in block['items']:
        if item['type'] == 'bullet':
            wrapped = wrap_text(c, item['text'].replace('**', ''), w - 35)
            for i, line in enumerate(wrapped):
                if i == 0:
                    draw_text_with_bold(c, "• " + item['text'], x + 15, ty)
                else:
                    c.setFont("Helvetica", 9)
                    c.drawString(x + 25, ty, line)
                ty -= 13
        else:
            for line in wrap_text(c, item['text'], w - 30):
                c.setFont("Helvetica", 9)
                c.drawString(x + 15, ty, line)
                ty -= 13
    return h


def create_pdf_page1(c, content, family_photo=None, bg_path=None):
    w, h = A4
    if bg_path and os.path.exists(bg_path):
        try:
            c.drawImage(bg_path, 0, 0, width=w, height=h, preserveAspectRatio=False, mask='auto')
        except:
            pass
    
    title, blocks = parse_markdown_content(content)
    c.setFont("Helvetica-Bold", 14)
    c.setFillColorRGB(COLORS['title_green'][0]/255, COLORS['title_green'][1]/255, COLORS['title_green'][2]/255)
    c.drawString(200, h - 122, f"EXPOSÉ FAMILIE {title['name']} AUS {title['city']}")
    
    margin, cw = 20, w - 40
    cy = h - 145
    photo_w, photo_h = (cw / 2) - 10, 120
    
    first = blocks[0] if blocks else None
    if first and 'mitglieder' in first['title'].lower():
        bh = max(calc_block_height(c, first, cw / 2), photo_h + 20)
        draw_rounded_rect(c, margin, cy - bh, cw, bh, 8, (255, 255, 255), 0.88)
        
        if family_photo:
            try:
                buf = io.BytesIO()
                fix_image_orientation(family_photo).save(buf, format='JPEG', quality=90)
                buf.seek(0)
                c.drawImage(ImageReader(buf), margin + 5, cy - bh + 8, width=photo_w, height=photo_h, preserveAspectRatio=True)
            except:
                pass
        
        mx = margin + photo_w + 20
        draw_section_header(c, mx, cy - 5, first['title'] + ":")
        ty = cy - 36
        c.setFillColorRGB(0.2, 0.2, 0.2)
        for item in first['items']:
            if item['type'] == 'bullet':
                draw_text_with_bold(c, "• " + item['text'], mx + 5, ty)
                ty -= 14
        cy -= bh + 8
        blocks = blocks[1:]
    
    for block in blocks:
        if not block['items']:
            continue
        bh = calc_block_height(c, block, cw)
        if cy - bh < 50:
            break
        draw_block(c, block, margin, cy, cw)
        cy -= bh + 8


def create_pdf_page2(c, photos, names, content, bg_path=None):
    w, h = A4
    c.showPage()
    if bg_path and os.path.exists(bg_path):
        try:
            c.drawImage(bg_path, 0, 0, width=w, height=h, preserveAspectRatio=False, mask='auto')
        except:
            pass
    
    title, _ = parse_markdown_content(content)
    c.setFont("Helvetica-Bold", 14)
    c.setFillColorRGB(COLORS['title_green'][0]/255, COLORS['title_green'][1]/255, COLORS['title_green'][2]/255)
    c.drawString(200, h - 122, f"FOTOS - FAMILIE {title['name']}")
    
    if not photos:
        return
    
    margin, gap = 25, 15
    col_w, ph = (w - 2 * margin - gap) / 2, 160
    sy = h - 150
    
    for i, (photo, name) in enumerate(zip(photos, names)):
        col, row = i % 2, i // 2
        x, y = margin + col * (col_w + gap), sy - row * (ph + 30)
        if y < 80:
            break
        try:
            buf = io.BytesIO()
            fix_image_orientation(photo).save(buf, format='JPEG', quality=90)
            buf.seek(0)
            draw_rounded_rect(c, x - 5, y - ph - 5, col_w + 10, ph + 22, 5, (255, 255, 255), 0.9)
            c.drawImage(ImageReader(buf), x, y - ph, width=col_w, height=ph, preserveAspectRatio=True)
            c.setFont("Helvetica", 7)
            c.setFillColorRGB(0.3, 0.3, 0.3)
            c.drawString(x, y - ph - 12, name[:50])
        except:
            pass


# =============================================================
# PDF Export MIT eingebetteten Projektdaten
# =============================================================

def create_full_pdf(content, family_photo=None, garden_photos=None, 
                    photo_names=None, bg_path=None,
                    family_photo_name=None, all_photo_data=None):
    """
    Erstellt PDF mit eingebetteten Projektdaten.
    
    all_photo_data: Liste von dicts mit {'name': str, 'bytes': bytes, 'is_family': bool, 'selected': bool}
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    
    create_pdf_page1(c, content, family_photo, bg_path)
    if garden_photos:
        create_pdf_page2(c, garden_photos, photo_names or [], content, bg_path)
    
    c.save()
    buf.seek(0)
    
    # --- Projektdaten in PDF einbetten ---
    doc = fitz.open(stream=buf.read(), filetype="pdf")
    
    # 1. Markdown-Content als Metadaten
    project_meta = {
        "marker": PDF_MARKER,
        "version": APP_VERSION,
        "markdown": content,
        "family_photo_name": family_photo_name or "",
        "photo_count": len(all_photo_data) if all_photo_data else 0,
        "created": time.strftime("%Y-%m-%d %H:%M")
    }
    
    # Metadaten setzen
    doc.set_metadata({
        "author": "Casting Exposé Generator",
        "subject": json.dumps(project_meta, ensure_ascii=False),
        "title": f"Exposé - {parse_markdown_content(content)[0]['name']}"
    })
    
    # 2. Fotos als Attachments einbetten
    if all_photo_data:
        # Photo-Index als JSON
        photo_index = []
        for pd in all_photo_data:
            photo_index.append({
                "name": pd["name"],
                "is_family": pd.get("is_family", False),
                "selected": pd.get("selected", True)
            })
        
        # Index-Datei einbetten
        index_bytes = json.dumps(photo_index, ensure_ascii=False).encode('utf-8')
        doc.embfile_add("photo_index.json", index_bytes, 
                       filename="photo_index.json", 
                       desc="Foto-Index für Re-Import")
        
        # Jedes Foto einbetten
        for pd in all_photo_data:
            doc.embfile_add(
                pd["name"], 
                pd["bytes"],
                filename=pd["name"],
                desc=f"{'Familienfoto' if pd.get('is_family') else 'Foto'}: {pd['name']}"
            )
    
    # Fertiges PDF ausgeben
    output = io.BytesIO()
    doc.save(output)
    doc.close()
    output.seek(0)
    return output


# =============================================================
# PDF Import (Re-Edit)
# =============================================================

def import_from_pdf(pdf_file):
    """
    Importiert Projektdaten aus einer generierten PDF.
    Gibt zurück: (markdown_content, photos_list, photo_names, family_idx)
    """
    pdf_bytes = pdf_file.read()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    
    # 1. Metadaten lesen
    metadata = doc.metadata
    subject = metadata.get("subject", "")
    
    try:
        project_meta = json.loads(subject)
    except:
        doc.close()
        return None, None, None, None
    
    # Prüfen ob es eine Exposé-PDF ist
    if project_meta.get("marker") != PDF_MARKER:
        doc.close()
        return None, None, None, None
    
    markdown_content = project_meta.get("markdown", "")
    family_photo_name = project_meta.get("family_photo_name", "")
    
    # 2. Fotos aus Attachments extrahieren
    photos = []
    photo_names = []
    family_idx = None
    
    # Photo-Index laden
    photo_index = []
    try:
        if "photo_index.json" in doc.embfile_names():
            index_bytes = doc.embfile_get("photo_index.json")
            photo_index = json.loads(index_bytes.decode('utf-8'))
    except:
        pass
    
    # Fotos extrahieren
    for i, pi in enumerate(photo_index):
        name = pi["name"]
        try:
            if name in doc.embfile_names():
                img_bytes = doc.embfile_get(name)
                img = Image.open(io.BytesIO(img_bytes))
                photos.append(img)
                photo_names.append(name)
                
                if pi.get("is_family", False):
                    family_idx = len(photos) - 1
        except:
            pass
    
    doc.close()
    
    return markdown_content, photos, photo_names, family_idx


def is_expose_pdf(pdf_file):
    """Prüft ob eine PDF von unserem Generator stammt"""
    try:
        pdf_bytes = pdf_file.read()
        pdf_file.seek(0)  # Reset für späteren Gebrauch
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        meta = doc.metadata
        doc.close()
        
        subject = meta.get("subject", "")
        data = json.loads(subject)
        return data.get("marker") == PDF_MARKER
    except:
        return False


# =============================================================
# UI
# =============================================================

# Header
col1, col2, col3 = st.columns([1, 3, 1])
with col1:
    if os.path.exists("logo_ddg.png"):
        st.image("logo_ddg.png", width=150)
with col2:
    st.title("🎬 Casting Exposé Generator")
with col3:
    if os.path.exists("logo_redseven.png"):
        st.image("logo_redseven.png", width=120)

# Logout
_, col_logout = st.columns([6, 1])
with col_logout:
    if st.button("🚪 Logout", use_container_width=True):
        st.session_state["authenticated"] = False
        st.rerun()

st.divider()

# Beschreibung
desc = load_description()
desc_lines = desc.split('\n')
mid = len(desc_lines) // 2
dc1, dc2 = st.columns(2)
dc1.markdown('\n'.join(desc_lines[:mid]))
dc2.markdown('\n'.join(desc_lines[mid:]))

st.divider()

# =============================================================
# Import-Bereich (NEU)
# =============================================================
st.header("📂 Bestehendes Exposé bearbeiten")

import_col1, import_col2 = st.columns([3, 1])

with import_col1:
    import_file = st.file_uploader(
        "Exportierte Exposé-PDF zum Bearbeiten laden",
        type=["pdf"],
        key="import_pdf",
        help="Laden Sie eine zuvor exportierte PDF, um sie zu bearbeiten."
    )

with import_col2:
    st.write("")
    st.write("")
    if import_file and st.button("📥 PDF importieren", type="secondary", use_container_width=True):
        # Prüfen ob es unsere PDF ist
        if is_expose_pdf(import_file):
            import_file.seek(0)
            md_content, imp_photos, imp_names, imp_fam_idx = import_from_pdf(import_file)
            
            if md_content:
                st.session_state["extracted_content"] = md_content
                
                if imp_photos:
                    st.session_state["all_photos"] = imp_photos
                    st.session_state["all_photo_names"] = imp_names
                    st.session_state["family_idx"] = imp_fam_idx
                    st.session_state["selected_family_idx"] = imp_fam_idx
                    st.session_state["duplicate_indices"] = []
                    st.session_state["garden_indices"] = [i for i in range(len(imp_photos)) if i != imp_fam_idx]
                    st.session_state["selected_garden_indices"] = [i for i in range(len(imp_photos)) if i != imp_fam_idx]
                
                st.success(f"✅ Importiert! Text + {len(imp_photos) if imp_photos else 0} Fotos wiederhergestellt.")
                st.rerun()
            else:
                st.error("Konnte keine Daten aus der PDF extrahieren.")
        else:
            st.error("❌ Diese PDF wurde nicht mit dem Exposé Generator erstellt.")

st.divider()

# =============================================================
# Upload-Bereich
# =============================================================
st.header("1️⃣ Neues Exposé erstellen")

c1, c2, c3 = st.columns(3)
with c1:
    st.subheader("📄 Dokumente")
    doc_files = st.file_uploader("PDFs, Scans", type=["png", "jpg", "jpeg", "webp", "pdf", "docx"], accept_multiple_files=True, key="docs")
    if doc_files:
        st.success(f"✅ {len(doc_files)}")
with c2:
    st.subheader("📷 Fotos")
    photo_files = st.file_uploader("Familie & Garten", type=["png", "jpg", "jpeg", "webp"], accept_multiple_files=True, key="photos")
    if photo_files:
        st.success(f"✅ {len(photo_files)}")
with c3:
    st.subheader("📝 Text")
    manual_text = st.text_area("Zusätzliche Infos", height=150)

st.divider()

# --- Analyse ---
st.header("2️⃣ Analyse")

with st.expander("⚙️ Optionen"):
    oc1, oc2 = st.columns(2)
    max_size = oc1.slider("Bildgröße", 512, 1024, 800, 128)
    delay = oc2.slider("Pause (Sek.)", 0, 60, 5, 5)

if st.button("🔍 KI-Analyse starten", type="primary", use_container_width=True):
    if not doc_files and not photo_files and not manual_text:
        st.error("Bitte Dateien hochladen.")
    else:
        try:
            text = manual_text or ""
            di, dn = [], []
            if doc_files:
                for f in doc_files:
                    f.seek(0)
                    if f.type == 'application/pdf':
                        text += "\n\n" + extract_text_from_pdf(f)
                    elif 'word' in f.type:
                        text += "\n\n" + extract_text_from_docx(f)
                    elif f.type.startswith('image/'):
                        di.append(compress_image(Image.open(f), max_size))
                        dn.append(f.name)
            
            result = process_adaptive(di, dn, text, delay)
            st.session_state["extracted_content"] = result
            
            if photo_files:
                st.info("📷 Fotos...")
                photos = [compress_image(Image.open(f), 1200) for f in photo_files]
                names = [f.name for f in photo_files]
                garden, fam, dups = analyze_photos(photos)
                st.session_state.update({
                    "all_photos": photos, "all_photo_names": names,
                    "garden_indices": garden, "family_idx": fam, "duplicate_indices": dups,
                    "selected_garden_indices": garden
                })
            
            st.success("✅ Fertig!")
            st.balloons()
        except Exception as e:
            st.error(f"Fehler: {e}")

st.divider()

# --- Bearbeiten ---
st.header("3️⃣ Überprüfen & Bearbeiten")

if "extracted_content" in st.session_state:
    st.caption("Format: `# Name | Ort`, `## Section`, `- Aufzählung`, `**fett**`")
    edited = st.text_area("Exposé:", st.session_state["extracted_content"], height=300)
    
    if "all_photos" in st.session_state:
        st.subheader("📷 Fotos")
        photos = st.session_state["all_photos"]
        names = st.session_state["all_photo_names"]
        fam_idx = st.session_state.get("family_idx")
        dups = st.session_state.get("duplicate_indices", [])
        thumbs = [crop_to_square(p.copy()).resize((80, 80)) for p in photos]
        
        # Familienfoto
        st.markdown("**Familienfoto (Seite 1):**")
        sel_fam = st.session_state.get("selected_family_idx", fam_idx)
        
        fam_cols = st.columns(min(len(photos) + 1, 8))
        with fam_cols[0]:
            st.image(Image.new('RGB', (80, 80), (50, 50, 50)), width=80, caption="Keins")
            if st.button("✓" if sel_fam is None else "○", key="fn"):
                st.session_state["selected_family_idx"] = None
                st.rerun()
        
        for i, (th, nm) in enumerate(zip(thumbs, names)):
            ci = (i + 1) % 8
            if ci == 0 and i > 0:
                fam_cols = st.columns(8)
            with fam_cols[ci]:
                st.image(th, width=80, caption=nm[:8])
                if st.button("✓" if sel_fam == i else "○", key=f"f_{i}"):
                    st.session_state["selected_family_idx"] = i
                    st.rerun()
        
        # Seite-2-Fotos
        st.markdown("**Fotos für Seite 2:**")
        sel_g = st.session_state.get("selected_garden_indices", 
                [i for i in range(len(photos)) if i not in dups and i != fam_idx])
        
        cols = st.columns(6)
        new_sel = []
        for i, (th, nm) in enumerate(zip(thumbs, names)):
            with cols[i % 6]:
                st.image(th, width=90)
                s = "🔄" if i in dups else ("👨‍👩‍👧" if i == sel_fam else "")
                if st.checkbox(s or "✓", value=i in sel_g, key=f"s_{i}", disabled=i in dups or i == sel_fam):
                    new_sel.append(i)
        st.session_state["selected_garden_indices"] = new_sel
    
    st.divider()
    
    # --- Export ---
    st.header("4️⃣ Export")
    
    ec1, ec2 = st.columns([2, 1])
    fname = ec1.text_input("Dateiname:", "Expose_Familie")
    
    with ec2:
        st.write("")
        st.write("")
        if st.button("📥 PDF erstellen", type="primary"):
            try:
                bg = "Background.jpg" if os.path.exists("Background.jpg") else None
                
                # Familienfoto
                fam_photo = None
                fam_photo_name = None
                sel_fam = st.session_state.get("selected_family_idx")
                
                if "all_photos" in st.session_state and sel_fam is not None and sel_fam >= 0:
                    fam_photo = st.session_state["all_photos"][sel_fam]
                    fam_photo_name = st.session_state["all_photo_names"][sel_fam]
                
                # Gartenfotos
                gp, gn = [], []
                if "all_photos" in st.session_state:
                    for idx in st.session_state.get("selected_garden_indices", []):
                        if idx != sel_fam:
                            gp.append(st.session_state["all_photos"][idx])
                            gn.append(st.session_state["all_photo_names"][idx])
                
                # Alle Fotos für Einbettung vorbereiten
                all_photo_data = []
                if "all_photos" in st.session_state:
                    for i, (photo, name) in enumerate(zip(
                        st.session_state["all_photos"], 
                        st.session_state["all_photo_names"]
                    )):
                        img_bytes = image_to_bytes(photo, quality=85)
                        all_photo_data.append({
                            "name": name,
                            "bytes": img_bytes,
                            "is_family": i == sel_fam,
                            "selected": i in st.session_state.get("selected_garden_indices", [])
                        })
                
                pdf = create_full_pdf(
                    edited, fam_photo, gp, gn, bg,
                    family_photo_name=fam_photo_name,
                    all_photo_data=all_photo_data
                )
                
                st.download_button("⬇️ PDF herunterladen", pdf, f"{fname}.pdf", "application/pdf")
                st.info("💾 Projektdaten sind in der PDF eingebettet. Sie können diese PDF später wieder importieren.")
                
            except Exception as e:
                st.error(f"Fehler: {e}")
                import traceback
                st.code(traceback.format_exc())

else:
    st.info("👆 Neues Exposé erstellen oder bestehendes PDF importieren.")

st.divider()
st.caption("🔒 Daten werden nur temporär verarbeitet. | v" + APP_VERSION)
