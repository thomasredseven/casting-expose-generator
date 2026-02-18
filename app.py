# app.py - Casting Exposé Generator v4.1
# Fixes: Fett nur markiert, Titel rechts, kompakte Foto-Auswahl mit Squares

import streamlit as st
import google.generativeai as genai
from PIL import Image, ExifTags
import io
import time
import re
import os
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
import fitz
from docx import Document

# --- Konfiguration ---
st.set_page_config(
    page_title="Casting Exposé Generator",
    page_icon="🎬",
    layout="wide"
)

GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-3-flash-preview")

COLORS = {
    'title_green': (78, 124, 35),
    'section_header_bg': (101, 148, 58),
    'text_dark': (51, 51, 51),
}

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

REGELN:
- Erste Zeile: # FAMILIENNAME | ORT
- Sections mit ##
- Namen **fett**
- Kurz und knackig
- Deutsch
"""

SINGLE_IMAGE_PROMPT = "Extrahiere alle Informationen. Kurz, stichpunktartig, Deutsch."

COMBINE_PROMPT = """
Kombiniere zu EINEM Exposé im MARKDOWN-FORMAT:

{extracted_infos}

# FAMILIENNAME | ORT

## Familienmitglieder
- **Name** (Alter), Beruf

## Fakten zum Garten
- Stichpunkte

## Budget
X.XXX €

## Wünsche für den Garten
- Liste

## Die Familie
Fließtext

## Notizen
Falls relevant

Kurz! Keine Duplikate!
"""

PHOTO_ANALYSIS_PROMPT = "Kategorisiere: NUMMER|KATEGORIE|BESCHREIBUNG. Kategorien: FAMILIE, GARTEN, HAUS, SONSTIGES"

# --- Hilfsfunktionen ---

def fix_image_orientation(image):
    try:
        exif = image._getexif()
        if not exif:
            return image
        
        for key, value in ExifTags.TAGS.items():
            if value == 'Orientation':
                orientation = exif.get(key, 1)
                rotations = {
                    2: lambda img: img.transpose(Image.FLIP_LEFT_RIGHT),
                    3: lambda img: img.rotate(180, expand=True),
                    4: lambda img: img.transpose(Image.FLIP_TOP_BOTTOM),
                    5: lambda img: img.transpose(Image.FLIP_LEFT_RIGHT).rotate(270, expand=True),
                    6: lambda img: img.rotate(270, expand=True),
                    7: lambda img: img.transpose(Image.FLIP_LEFT_RIGHT).rotate(90, expand=True),
                    8: lambda img: img.rotate(90, expand=True),
                }
                if orientation in rotations:
                    return rotations[orientation](image)
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
    """Croppt Bild quadratisch (Mitte)"""
    w, h = image.size
    size = min(w, h)
    left = (w - size) // 2
    top = (h - size) // 2
    return image.crop((left, top, left + size, top + size))


def get_image_hash(image, hash_size=8):
    img = fix_image_orientation(image.copy()).convert('L').resize((hash_size + 1, hash_size), Image.LANCZOS)
    pixels = list(img.getdata())
    return tuple(pixels[r * (hash_size + 1) + c] > pixels[r * (hash_size + 1) + c + 1]
                 for r in range(hash_size) for c in range(hash_size))


def find_duplicates(images, threshold=10):
    hashes = [get_image_hash(img) for img in images]
    duplicates = set()
    for i in range(len(hashes)):
        for j in range(i + 1, len(hashes)):
            if sum(a != b for a, b in zip(hashes[i], hashes[j])) < threshold:
                duplicates.add(j)
    return duplicates


def extract_text_from_pdf(pdf_file):
    doc = fitz.open(stream=pdf_file.read(), filetype="pdf")
    text = "".join(page.get_text() for page in doc)
    doc.close()
    return text


def extract_text_from_docx(docx_file):
    return "\n".join(para.text for para in Document(docx_file).paragraphs)


def wait_with_countdown(seconds, message="Warte"):
    if seconds <= 0:
        return
    bar = st.progress(0)
    txt = st.empty()
    for i in range(seconds):
        txt.text(f"⏱️ {message}... {seconds - i}s")
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
    
    duplicates = find_duplicates(images)
    unique = [i for i in range(len(images)) if i not in duplicates]
    
    try:
        resp = call_gemini_with_retry([PHOTO_ANALYSIS_PROMPT] + [images[i] for i in unique])
        categories = {}
        family_idx = None
        
        for line in resp.strip().split('\n'):
            if '|' in line:
                parts = line.split('|')
                try:
                    idx = int(parts[0].strip()) - 1
                    cat = parts[1].strip().upper()
                    if idx < len(unique):
                        real_idx = unique[idx]
                        categories[real_idx] = cat
                        if cat == 'FAMILIE' and family_idx is None:
                            family_idx = real_idx
                except:
                    pass
        
        garden = [i for i in unique if categories.get(i) != 'FAMILIE']
        return garden, family_idx, list(duplicates)
    except:
        return unique, None, list(duplicates)


def process_adaptive(images, names, text="", delay=0):
    n = len(images)
    
    if n == 0:
        return call_gemini_with_retry([EXTRACTION_PROMPT + "\n\n" + text]) if text else None
    
    if n == 1:
        st.info("📤 Verarbeite...")
        contents = [EXTRACTION_PROMPT, images[0]]
        if text:
            contents.append(f"\n\nZusatzinfos:\n{text}")
        return call_gemini_with_retry(contents)
    
    st.info(f"🚀 Stufe 1: Alle {n} Dokumente...")
    try:
        contents = [EXTRACTION_PROMPT]
        if text:
            contents.append(f"\n\nZusatzinfos:\n{text}\n\n")
        contents.extend(["Dokumente:"] + images)
        return call_gemini(contents)
    except Exception as e:
        if is_rate_limit_error(e):
            st.warning("⚠️ Stufe 2...")
            wait_with_countdown(min(get_retry_delay(e), 30))
        else:
            raise
    
    if n > 3:
        st.info("📦 Stufe 2: Batches...")
        try:
            parts = []
            for i in range(0, n, 3):
                r = call_gemini_with_retry([SINGLE_IMAGE_PROMPT] + images[i:i+3])
                if r:
                    parts.append(r)
                if delay:
                    wait_with_countdown(delay)
            
            all_info = "\n\n".join(parts)
            if text:
                all_info = f"Zusatzinfos:\n{text}\n\n{all_info}"
            return call_gemini_with_retry([COMBINE_PROMPT.format(extracted_infos=all_info)])
        except Exception as e:
            if is_rate_limit_error(e):
                st.warning("⚠️ Stufe 3...")
                wait_with_countdown(min(get_retry_delay(e), 30))
            else:
                raise
    
    st.info("🐢 Stufe 3: Einzeln...")
    parts = []
    for img in images:
        r = call_gemini_with_retry([SINGLE_IMAGE_PROMPT, img])
        if r:
            parts.append(r)
        if delay:
            wait_with_countdown(max(delay, 5))
    
    all_info = "\n\n".join(parts)
    if text:
        all_info = f"Zusatzinfos:\n{text}\n\n{all_info}"
    return call_gemini_with_retry([COMBINE_PROMPT.format(extracted_infos=all_info)])


# --- Markdown Parser ---

def parse_markdown_content(content):
    blocks = []
    title = {'name': 'FAMILIE', 'city': 'ORT'}
    current = None
    
    for line in content.strip().split('\n'):
        line = line.strip()
        if not line or line == '---':
            continue
        
        if line.startswith('# ') and not line.startswith('## '):
            txt = line[2:].strip()
            if '|' in txt:
                parts = txt.split('|')
                title['name'] = parts[0].strip()
                title['city'] = parts[1].strip() if len(parts) > 1 else ''
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


# --- PDF Erstellung ---

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
    lines, current = [], ""
    for word in words:
        test = current + " " + word if current else word
        if c.stringWidth(test, font, size) < max_w:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def draw_text_with_bold(c, text, x, y):
    """Zeichnet Text, nur **markierter** Text wird fett"""
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
    lines = 0
    for item in block['items']:
        lines += len(wrap_text(c, item['text'].replace('**', ''), w - 35))
    return 34 + lines * 13 + 10


def draw_block(c, block, x, y, w):
    h = calc_block_height(c, block, w)
    draw_rounded_rect(c, x, y - h, w, h, 8, (255, 255, 255), 0.88)
    draw_section_header(c, x + 5, y - 5, block['title'] + ":")
    
    ty = y - 36
    c.setFillColorRGB(0.2, 0.2, 0.2)
    
    for item in block['items']:
        text = item['text']
        if item['type'] == 'bullet':
            wrapped = wrap_text(c, text.replace('**', ''), w - 35)
            for i, line in enumerate(wrapped):
                if i == 0:
                    draw_text_with_bold(c, "• " + text, x + 15, ty)
                else:
                    c.setFont("Helvetica", 9)
                    c.drawString(x + 25, ty, line)
                ty -= 13
        else:
            for line in wrap_text(c, text, w - 30):
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
    
    # Titel - weiter rechts (nach Büschen, ca. X=200)
    c.setFont("Helvetica-Bold", 14)
    c.setFillColorRGB(COLORS['title_green'][0]/255, COLORS['title_green'][1]/255, COLORS['title_green'][2]/255)
    c.drawString(200, h - 122, f"EXPOSÉ FAMILIE {title['name']} AUS {title['city']}")
    
    margin = 20
    cw = w - margin * 2
    cy = h - 145
    
    photo_w = (cw / 2) - 10
    photo_h = 120
    
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
    col_w = (w - 2 * margin - gap) / 2
    ph = 160
    sy = h - 150
    
    for i, (photo, name) in enumerate(zip(photos, names)):
        col, row = i % 2, i // 2
        x = margin + col * (col_w + gap)
        y = sy - row * (ph + 30)
        
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


def create_full_pdf(content, family_photo=None, garden_photos=None, names=None, bg_path=None):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    create_pdf_page1(c, content, family_photo, bg_path)
    if garden_photos:
        create_pdf_page2(c, garden_photos, names or [], content, bg_path)
    c.save()
    buf.seek(0)
    return buf


# --- UI ---

col1, col2, col3 = st.columns([1, 3, 1])
with col1:
    if os.path.exists("logo_ddg.png"):
        st.image("logo_ddg.png", width=150)
with col2:
    st.title("🎬 Casting Exposé Generator")
with col3:
    if os.path.exists("logo_redseven.png"):
        st.image("logo_redseven.png", width=120)

st.divider()
st.header("1️⃣ Unterlagen hochladen")

c1, c2, c3 = st.columns(3)
with c1:
    st.subheader("📄 Dokumente")
    doc_files = st.file_uploader("PDFs, Scans", type=["png", "jpg", "jpeg", "webp", "pdf", "docx"], accept_multiple_files=True, key="docs")
with c2:
    st.subheader("📷 Fotos")
    photo_files = st.file_uploader("Familie & Garten", type=["png", "jpg", "jpeg", "webp"], accept_multiple_files=True, key="photos")
with c3:
    st.subheader("📝 Text")
    manual_text = st.text_area("Zusätzliche Infos", height=150)

st.divider()
st.header("2️⃣ Analyse")

with st.expander("⚙️ Optionen"):
    c1, c2 = st.columns(2)
    max_size = c1.slider("Bildgröße", 512, 1024, 800, 128)
    delay = c2.slider("Pause (Sek.)", 0, 60, 5, 5)

if st.button("🔍 KI-Analyse", type="primary", use_container_width=True):
    if not doc_files and not photo_files and not manual_text:
        st.error("Bitte Dateien hochladen.")
    else:
        try:
            text = manual_text or ""
            doc_imgs, doc_names = [], []
            
            if doc_files:
                for f in doc_files:
                    f.seek(0)
                    if f.type == 'application/pdf':
                        text += "\n\n" + extract_text_from_pdf(f)
                    elif 'word' in f.type:
                        text += "\n\n" + extract_text_from_docx(f)
                    elif f.type.startswith('image/'):
                        doc_imgs.append(compress_image(Image.open(f), max_size))
                        doc_names.append(f.name)
            
            result = process_adaptive(doc_imgs, doc_names, text, delay)
            st.session_state["extracted_content"] = result
            
            if photo_files:
                st.info("📷 Fotos...")
                photos = [compress_image(Image.open(f), 1200) for f in photo_files]
                names = [f.name for f in photo_files]
                garden, fam_idx, dups = analyze_photos(photos)
                
                st.session_state.update({
                    "all_photos": photos, "all_photo_names": names,
                    "garden_indices": garden, "family_idx": fam_idx, "duplicate_indices": dups
                })
            
            st.success("✅ Fertig!")
            st.balloons()
        except Exception as e:
            st.error(f"Fehler: {e}")

st.divider()
st.header("3️⃣ Bearbeiten")

if "extracted_content" in st.session_state:
    st.caption("Format: `# Name | Ort`, `## Section`, `- Aufzählung`, `**fett**`")
    edited = st.text_area("Exposé:", st.session_state["extracted_content"], height=300)
    
    if "all_photos" in st.session_state:
        st.subheader("📷 Fotos")
        
        photos = st.session_state["all_photos"]
        names = st.session_state["all_photo_names"]
        fam_idx = st.session_state.get("family_idx")
        dups = st.session_state.get("duplicate_indices", [])
        
        # Quadratische Thumbnails
        thumbs = [crop_to_square(p.copy()).resize((80, 80)) for p in photos]
        
        # Familienfoto-Auswahl mit Bildern
        st.markdown("**Familienfoto (Seite 1):**")
        sel_fam = st.session_state.get("selected_family_idx", fam_idx)
        
        fam_cols = st.columns(min(len(photos) + 1, 8))
        with fam_cols[0]:
            st.image(Image.new('RGB', (80, 80), (50, 50, 50)), width=80, caption="Keins")
            if st.button("✓" if sel_fam is None else "○", key="fam_none"):
                st.session_state["selected_family_idx"] = None
                st.rerun()
        
        for i, (thumb, name) in enumerate(zip(thumbs, names)):
            col_i = (i + 1) % 8
            if col_i == 0 and i > 0:
                fam_cols = st.columns(8)
            with fam_cols[col_i]:
                st.image(thumb, width=80, caption=name[:8])
                if st.button("✓" if sel_fam == i else "○", key=f"fam_{i}"):
                    st.session_state["selected_family_idx"] = i
                    st.rerun()
        
        # Seite-2-Fotos kompakt
        st.markdown("**Fotos für Seite 2:**")
        sel_garden = st.session_state.get("selected_garden_indices", 
                     [i for i in range(len(photos)) if i not in dups and i != fam_idx])
        
        cols = st.columns(6)
        new_sel = []
        for i, (thumb, name) in enumerate(zip(thumbs, names)):
            with cols[i % 6]:
                st.image(thumb, width=90)
                status = "🔄" if i in dups else ("👨‍👩‍👧" if i == sel_fam else "")
                if st.checkbox(status or "✓", value=i in sel_garden, key=f"s_{i}", disabled=i in dups or i == sel_fam):
                    new_sel.append(i)
        st.session_state["selected_garden_indices"] = new_sel
    
    st.divider()
    st.header("4️⃣ Export")
    
    c1, c2 = st.columns([2, 1])
    fname = c1.text_input("Dateiname:", "Expose_Familie")
    
    with c2:
        st.write("")
        st.write("")
        if st.button("📥 PDF", type="primary"):
            bg = "Background.jpg" if os.path.exists("Background.jpg") else None
            
            fam_photo = None
            if "all_photos" in st.session_state:
                fidx = st.session_state.get("selected_family_idx")
                if fidx is not None and fidx >= 0:
                    fam_photo = st.session_state["all_photos"][fidx]
            
            garden_photos, garden_names = [], []
            if "all_photos" in st.session_state:
                for idx in st.session_state.get("selected_garden_indices", []):
                    if idx != st.session_state.get("selected_family_idx"):
                        garden_photos.append(st.session_state["all_photos"][idx])
                        garden_names.append(st.session_state["all_photo_names"][idx])
            
            pdf = create_full_pdf(edited, fam_photo, garden_photos, garden_names, bg)
            st.download_button("⬇️ Download", pdf, f"{fname}.pdf", "application/pdf")

else:
    st.info("👆 Erst analysieren.")

st.divider()
st.caption("🔒 Daten nur temporär.")
