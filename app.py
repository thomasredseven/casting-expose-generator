# app.py - Casting Exposé Generator v4.0
# Markdown-basiertes Format für stabiles Parsing

import streamlit as st
import google.generativeai as genai
from PIL import Image, ExifTags
import io
import time
import re
import os
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm, mm
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

# --- Farben ---
COLORS = {
    'title_green': (78, 124, 35),
    'section_header_bg': (101, 148, 58),
    'text_dark': (51, 51, 51),
}

# --- NEUER PROMPT mit Markdown-Format ---
EXTRACTION_PROMPT = """
Analysiere diese Casting-Unterlagen und erstelle ein Exposé im folgenden MARKDOWN-FORMAT.

WICHTIG: Halte dich EXAKT an dieses Format mit den Markdown-Zeichen!

---

# FAMILIENNAME | ORT

## Familienmitglieder
- **Vorname** (Alter), Beruf
- **Vorname** (Alter), Beruf

## Fakten zum Garten
- Größe: X m²
- Weitere wichtige Fakten

## Budget
X.XXX €

## Wünsche für den Garten
- Wunsch 1
- Wunsch 2
- Wunsch 3

## Die Familie
Kurzer Fließtext (1-2 Sätze) über die Familie und warum sie umgestalten wollen.

## Notizen
Termine, Einschränkungen, Besonderheiten (nur wenn vorhanden, sonst weglassen)

---

REGELN:
- Erste Zeile MUSS sein: # FAMILIENNAME | ORT
- Jede Section beginnt mit ## 
- Namen in Aufzählungen **fett** markieren
- Kurz und knackig formulieren
- Deutsch
- Keine zusätzlichen Erklärungen, nur das Exposé
"""

SINGLE_IMAGE_PROMPT = """
Extrahiere alle Informationen aus diesem Dokument.
Kurz, stichpunktartig, auf Deutsch.
"""

COMBINE_PROMPT = """
Kombiniere diese Informationen zu EINEM Exposé im MARKDOWN-FORMAT:

{extracted_infos}

---

FORMAT (exakt einhalten!):

# FAMILIENNAME | ORT

## Familienmitglieder
- **Name** (Alter), Beruf

## Fakten zum Garten
- Stichpunkte

## Budget
X.XXX €

## Wünsche für den Garten
- Wünsche als Liste

## Die Familie
Fließtext zur Familie

## Notizen
Falls relevant

---

Kurz und knackig! Keine Duplikate!
"""

PHOTO_ANALYSIS_PROMPT = """
Kategorisiere jedes Foto:
NUMMER|KATEGORIE|BESCHREIBUNG

Kategorien: FAMILIE, GARTEN, HAUS, SONSTIGES
"""

# --- Hilfsfunktionen ---

def fix_image_orientation(image):
    try:
        exif = image._getexif()
        if exif is None:
            return image
        
        orientation_key = None
        for key, value in ExifTags.TAGS.items():
            if value == 'Orientation':
                orientation_key = key
                break
        
        if orientation_key is None or orientation_key not in exif:
            return image
        
        orientation = exif[orientation_key]
        
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
            image = rotations[orientation](image)
        
        return image
    except:
        return image


def compress_image(image, max_size=800):
    image = fix_image_orientation(image)
    ratio = min(max_size / image.width, max_size / image.height)
    if ratio < 1:
        new_size = (int(image.width * ratio), int(image.height * ratio))
        image = image.resize(new_size, Image.LANCZOS)
    if image.mode in ('RGBA', 'P'):
        image = image.convert('RGB')
    return image


def get_image_hash(image, hash_size=8):
    img = fix_image_orientation(image.copy())
    img = img.convert('L').resize((hash_size + 1, hash_size), Image.LANCZOS)
    pixels = list(img.getdata())
    return tuple(pixels[row * (hash_size + 1) + col] > pixels[row * (hash_size + 1) + col + 1]
                 for row in range(hash_size) for col in range(hash_size))


def find_duplicates(images, threshold=10):
    hashes = [get_image_hash(img) for img in images]
    duplicates = set()
    for i in range(len(hashes)):
        for j in range(i + 1, len(hashes)):
            if sum(a != b for a, b in zip(hashes[i], hashes[j])) < threshold:
                duplicates.add(j)
    return duplicates


def extract_text_from_pdf(pdf_file):
    pdf_bytes = pdf_file.read()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    text = "".join(page.get_text() for page in doc)
    doc.close()
    return text


def extract_text_from_docx(docx_file):
    doc = Document(docx_file)
    return "\n".join(para.text for para in doc.paragraphs)


def wait_with_countdown(seconds, message="Warte"):
    if seconds <= 0:
        return
    progress_bar = st.progress(0)
    countdown_text = st.empty()
    for i in range(seconds):
        countdown_text.text(f"⏱️ {message}... {seconds - i}s")
        progress_bar.progress((i + 1) / seconds)
        time.sleep(1)
    countdown_text.empty()
    progress_bar.empty()


def is_rate_limit_error(error):
    return any(x in str(error).lower() for x in ["429", "quota", "rate", "limit"])


def get_retry_delay(error):
    match = re.search(r'retry_delay.*?(\d+)', str(error))
    return int(match.group(1)) + 5 if match else 30


def call_gemini(contents):
    return model.generate_content(contents).text


def call_gemini_with_retry(contents, max_retries=3):
    for attempt in range(max_retries):
        try:
            return call_gemini(contents)
        except Exception as e:
            if is_rate_limit_error(e) and attempt < max_retries - 1:
                wait_with_countdown(get_retry_delay(e), "Rate-Limit, warte")
            else:
                raise e
    return None


# --- Foto-Analyse ---

def analyze_photos(images):
    if not images:
        return [], None, []
    
    duplicates = find_duplicates(images)
    unique_indices = [i for i in range(len(images)) if i not in duplicates]
    
    try:
        contents = [PHOTO_ANALYSIS_PROMPT] + [images[idx] for idx in unique_indices]
        response = call_gemini_with_retry(contents)
        
        categories = {}
        family_photo_idx = None
        
        for line in response.strip().split('\n'):
            if '|' in line:
                parts = line.split('|')
                if len(parts) >= 2:
                    try:
                        photo_idx = int(parts[0].strip()) - 1
                        category = parts[1].strip().upper()
                        if photo_idx < len(unique_indices):
                            real_idx = unique_indices[photo_idx]
                            categories[real_idx] = category
                            if category == 'FAMILIE' and family_photo_idx is None:
                                family_photo_idx = real_idx
                    except:
                        pass
        
        garden_photos = [i for i in unique_indices if categories.get(i) != 'FAMILIE']
        return garden_photos, family_photo_idx, list(duplicates)
        
    except Exception as e:
        st.warning(f"Foto-Analyse fehlgeschlagen: {e}")
        return unique_indices, None, list(duplicates)


# --- Adaptive Verarbeitung ---

def process_adaptive(images, image_names, additional_text="", delay=0):
    num_images = len(images)
    
    if num_images == 0:
        if additional_text:
            return call_gemini_with_retry([EXTRACTION_PROMPT + "\n\n" + additional_text])
        return None
    
    if num_images == 1:
        st.info("📤 Verarbeite Dokument...")
        contents = [EXTRACTION_PROMPT, images[0]]
        if additional_text:
            contents.append(f"\n\nZusatzinfos:\n{additional_text}")
        return call_gemini_with_retry(contents)
    
    # Stufe 1: Alle auf einmal
    st.info(f"🚀 **Stufe 1:** Alle {num_images} Dokumente...")
    try:
        contents = [EXTRACTION_PROMPT]
        if additional_text:
            contents.append(f"\n\nZusatzinfos:\n{additional_text}\n\n")
        contents.append("Dokumente:")
        contents.extend(images)
        result = call_gemini(contents)
        st.success("✅ Stufe 1 erfolgreich!")
        return result
    except Exception as e:
        if is_rate_limit_error(e):
            st.warning("⚠️ Wechsle zu Stufe 2...")
            wait_with_countdown(min(get_retry_delay(e), 30))
        else:
            raise e
    
    # Stufe 2: In Batches
    if num_images > 3:
        st.info("📦 **Stufe 2:** 3er-Gruppen...")
        try:
            extracted_parts = []
            for i in range(0, num_images, 3):
                batch = images[i:i+3]
                result = call_gemini_with_retry([SINGLE_IMAGE_PROMPT] + batch)
                if result:
                    extracted_parts.append(result)
                if delay > 0:
                    wait_with_countdown(delay)
            
            all_infos = "\n\n".join(extracted_parts)
            if additional_text:
                all_infos = f"Zusatzinfos:\n{additional_text}\n\n{all_infos}"
            result = call_gemini_with_retry([COMBINE_PROMPT.format(extracted_infos=all_infos)])
            st.success("✅ Stufe 2 erfolgreich!")
            return result
        except Exception as e:
            if is_rate_limit_error(e):
                st.warning("⚠️ Wechsle zu Stufe 3...")
                wait_with_countdown(min(get_retry_delay(e), 30))
            else:
                raise e
    
    # Stufe 3: Einzeln
    st.info("🐢 **Stufe 3:** Einzeln...")
    extracted_parts = []
    for i, img in enumerate(images):
        result = call_gemini_with_retry([SINGLE_IMAGE_PROMPT, img])
        if result:
            extracted_parts.append(result)
        if delay > 0:
            wait_with_countdown(max(delay, 5))
    
    all_infos = "\n\n".join(extracted_parts)
    if additional_text:
        all_infos = f"Zusatzinfos:\n{additional_text}\n\n{all_infos}"
    result = call_gemini_with_retry([COMBINE_PROMPT.format(extracted_infos=all_infos)])
    st.success("✅ Stufe 3 erfolgreich!")
    return result


# --- NEUER Markdown Parser ---

def parse_markdown_content(content):
    """
    Parst Markdown-formatierten Content in strukturierte Blöcke.
    Jeder Block hat einen Typ und Inhalt.
    """
    blocks = []
    lines = content.strip().split('\n')
    
    current_block = None
    title_info = {'name': 'FAMILIE', 'city': 'ORT'}
    
    for line in lines:
        line_stripped = line.strip()
        
        if not line_stripped or line_stripped == '---':
            continue
        
        # Haupttitel: # FAMILIENNAME | ORT
        if line_stripped.startswith('# ') and not line_stripped.startswith('## '):
            title_text = line_stripped[2:].strip()
            if '|' in title_text:
                parts = title_text.split('|')
                title_info['name'] = parts[0].strip()
                title_info['city'] = parts[1].strip() if len(parts) > 1 else ''
            else:
                title_info['name'] = title_text
            continue
        
        # Section Header: ## Überschrift
        if line_stripped.startswith('## '):
            # Vorherigen Block speichern
            if current_block:
                blocks.append(current_block)
            
            section_title = line_stripped[3:].strip()
            current_block = {
                'type': 'section',
                'title': section_title,
                'items': []
            }
            continue
        
        # Aufzählung: - Item oder * Item
        if line_stripped.startswith('- ') or line_stripped.startswith('* '):
            item_text = line_stripped[2:].strip()
            if current_block:
                current_block['items'].append({'type': 'bullet', 'text': item_text})
            continue
        
        # Normaler Text (Fließtext)
        if current_block:
            current_block['items'].append({'type': 'text', 'text': line_stripped})
    
    # Letzten Block speichern
    if current_block:
        blocks.append(current_block)
    
    return title_info, blocks


# --- PDF-Erstellung mit Markdown ---

def draw_rounded_rect(c, x, y, width, height, radius, fill_color, alpha=0.88):
    c.saveState()
    c.setFillColorRGB(fill_color[0]/255, fill_color[1]/255, fill_color[2]/255, alpha)
    c.roundRect(x, y, width, height, radius, fill=1, stroke=0)
    c.restoreState()


def draw_section_header(c, x, y, text):
    c.saveState()
    c.setFont("Helvetica-Bold", 10)
    text_width = c.stringWidth(text, "Helvetica-Bold", 10)
    box_width = text_width + 14
    box_height = 18
    
    c.setFillColorRGB(COLORS['section_header_bg'][0]/255, 
                      COLORS['section_header_bg'][1]/255, 
                      COLORS['section_header_bg'][2]/255)
    c.roundRect(x, y - box_height + 4, box_width, box_height, 3, fill=1, stroke=0)
    
    c.setFillColorRGB(1, 1, 1)
    c.drawString(x + 7, y - 9, text)
    c.restoreState()
    return box_height


def wrap_text(c, text, max_width, font="Helvetica", size=9):
    c.setFont(font, size)
    words = text.split()
    lines = []
    current_line = ""
    
    for word in words:
        test_line = current_line + " " + word if current_line else word
        if c.stringWidth(test_line, font, size) < max_width:
            current_line = test_line
        else:
            if current_line:
                lines.append(current_line)
            current_line = word
    if current_line:
        lines.append(current_line)
    
    return lines


def draw_text_with_bold(c, text, x, y, max_width):
    """
    Zeichnet Text mit **fett** Markierungen.
    Gibt die Anzahl der verwendeten Zeilen zurück.
    """
    # Fett-Bereiche finden
    parts = re.split(r'(\*\*[^*]+\*\*)', text)
    
    current_x = x
    c.setFillColorRGB(0.2, 0.2, 0.2)
    
    for part in parts:
        if part.startswith('**') and part.endswith('**'):
            # Fetter Text
            bold_text = part[2:-2]
            c.setFont("Helvetica-Bold", 9)
            c.drawString(current_x, y, bold_text)
            current_x += c.stringWidth(bold_text, "Helvetica-Bold", 9)
        else:
            # Normaler Text
            c.setFont("Helvetica", 9)
            c.drawString(current_x, y, part)
            current_x += c.stringWidth(part, "Helvetica", 9)


def calculate_block_height(c, block, content_width):
    """Berechnet die Höhe eines Blocks"""
    line_height = 13
    header_height = 32  # Überschrift + Abstand
    
    total_lines = 0
    for item in block['items']:
        text = item['text'].replace('**', '')
        wrapped = wrap_text(c, text, content_width - 35)
        total_lines += len(wrapped)
    
    return header_height + total_lines * line_height + 10


def draw_block(c, block, x, y, width):
    """Zeichnet einen kompletten Block (Section) ins PDF"""
    line_height = 13
    
    # Höhe berechnen
    height = calculate_block_height(c, block, width)
    
    # Weißer Hintergrund
    draw_rounded_rect(c, x, y - height, width, height, 8, (255, 255, 255), 0.88)
    
    # Section Header
    draw_section_header(c, x + 5, y - 5, block['title'] + ":")
    
    # Inhalt
    text_y = y - 34  # Mehr Abstand unter Header
    c.setFillColorRGB(0.2, 0.2, 0.2)
    
    for item in block['items']:
        text = item['text']
        
        if item['type'] == 'bullet':
            # Aufzählung mit Bullet
            prefix = "• "
            text_clean = text.replace('**', '')
            wrapped = wrap_text(c, text_clean, width - 35)
            
            for i, line in enumerate(wrapped):
                if i == 0:
                    # Erste Zeile mit Bullet und ggf. fettem Namen
                    draw_text_with_bold(c, prefix + text if len(wrapped) == 1 else prefix + line, 
                                       x + 15, text_y, width - 35)
                else:
                    c.setFont("Helvetica", 9)
                    c.drawString(x + 25, text_y, line)
                text_y -= line_height
        else:
            # Fließtext
            text_clean = text.replace('**', '')
            wrapped = wrap_text(c, text_clean, width - 30)
            
            for line in wrapped:
                c.setFont("Helvetica", 9)
                c.drawString(x + 15, text_y, line)
                text_y -= line_height
    
    return height


def create_pdf_page1(c, content, family_photo=None, background_path=None):
    width, height = A4
    
    # Hintergrund
    if background_path and os.path.exists(background_path):
        try:
            c.drawImage(background_path, 0, 0, width=width, height=height, 
                       preserveAspectRatio=False, mask='auto')
        except:
            pass
    
    # Content parsen
    title_info, blocks = parse_markdown_content(content)
    
    # --- Titel (LINKSBÜNDIG für lange Namen) ---
    title_y = height - 122
    title_text = f"EXPOSÉ FAMILIE {title_info['name']} AUS {title_info['city']}"
    
    c.setFont("Helvetica-Bold", 14)
    c.setFillColorRGB(COLORS['title_green'][0]/255, 
                      COLORS['title_green'][1]/255, 
                      COLORS['title_green'][2]/255)
    
    # Linksbündig mit Einrückung
    margin_left = 20
    c.drawString(margin_left + 5, title_y, title_text)
    
    # --- Layout ---
    content_width = width - margin_left - 20
    current_y = height - 145
    
    # Familienfoto-Bereich
    photo_width = (content_width / 2) - 10
    photo_height = 120
    
    # Ersten Block (Familienmitglieder) mit Foto kombinieren
    first_block = blocks[0] if blocks else None
    
    if first_block and 'mitglieder' in first_block['title'].lower():
        # Kombinierter Block mit Foto
        members_block_height = max(calculate_block_height(c, first_block, content_width / 2), photo_height + 20)
        
        draw_rounded_rect(c, margin_left, current_y - members_block_height, content_width, members_block_height, 8, (255, 255, 255), 0.88)
        
        # Foto links
        if family_photo:
            try:
                img_buffer = io.BytesIO()
                photo_corrected = fix_image_orientation(family_photo)
                photo_corrected.save(img_buffer, format='JPEG', quality=90)
                img_buffer.seek(0)
                
                c.drawImage(ImageReader(img_buffer), margin_left + 5, current_y - members_block_height + 8, 
                           width=photo_width, height=photo_height, preserveAspectRatio=True)
            except:
                pass
        
        # Mitglieder rechts
        members_x = margin_left + photo_width + 20
        draw_section_header(c, members_x, current_y - 5, first_block['title'] + ":")
        
        text_y = current_y - 34
        c.setFillColorRGB(0.2, 0.2, 0.2)
        
        for item in first_block['items']:
            if item['type'] == 'bullet':
                draw_text_with_bold(c, "• " + item['text'], members_x + 5, text_y, content_width / 2 - 30)
                text_y -= 14
        
        current_y -= members_block_height + 8
        blocks = blocks[1:]  # Rest der Blocks
    
    # Restliche Blocks
    for block in blocks:
        if not block['items']:
            continue
            
        block_height = calculate_block_height(c, block, content_width)
        
        # Prüfen ob noch Platz auf der Seite
        if current_y - block_height < 50:
            break  # Nicht mehr genug Platz
        
        draw_block(c, block, margin_left, current_y, content_width)
        current_y -= block_height + 8


def create_pdf_page2(c, photos, photo_names, content, background_path=None):
    width, height = A4
    
    c.showPage()
    
    if background_path and os.path.exists(background_path):
        try:
            c.drawImage(background_path, 0, 0, width=width, height=height, 
                       preserveAspectRatio=False, mask='auto')
        except:
            pass
    
    title_info, _ = parse_markdown_content(content)
    
    title_y = height - 122
    title_text = f"FOTOS - FAMILIE {title_info['name']}"
    
    c.setFont("Helvetica-Bold", 14)
    c.setFillColorRGB(COLORS['title_green'][0]/255, 
                      COLORS['title_green'][1]/255, 
                      COLORS['title_green'][2]/255)
    c.drawString(25, title_y, title_text)
    
    if not photos:
        return
    
    margin = 25
    gap = 15
    col_width = (width - 2 * margin - gap) / 2
    photo_height = 160
    start_y = height - 150
    
    for i, (photo, name) in enumerate(zip(photos, photo_names)):
        col = i % 2
        row = i // 2
        
        x = margin + col * (col_width + gap)
        y = start_y - row * (photo_height + 30)
        
        if y < 80:
            break
        
        try:
            img_buffer = io.BytesIO()
            fix_image_orientation(photo).save(img_buffer, format='JPEG', quality=90)
            img_buffer.seek(0)
            
            draw_rounded_rect(c, x - 5, y - photo_height - 5, col_width + 10, photo_height + 22, 5, (255, 255, 255), 0.9)
            c.drawImage(ImageReader(img_buffer), x, y - photo_height, width=col_width, height=photo_height, preserveAspectRatio=True)
            
            c.setFont("Helvetica", 7)
            c.setFillColorRGB(0.3, 0.3, 0.3)
            c.drawString(x, y - photo_height - 12, name[:50])
        except:
            pass


def create_full_pdf(content, family_photo=None, garden_photos=None, photo_names=None, background_path=None):
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    
    create_pdf_page1(c, content, family_photo, background_path)
    
    if garden_photos:
        create_pdf_page2(c, garden_photos, photo_names or [], content, background_path)
    
    c.save()
    buffer.seek(0)
    return buffer


# --- UI ---

# Logos im Header
col_logo1, col_title, col_logo2 = st.columns([1, 3, 1])

with col_logo1:
    if os.path.exists("logo_ddg.png"):
        st.image("logo_ddg.png", width=150)

with col_title:
    st.title("🎬 Casting Exposé Generator")
    st.markdown("*Automatische Erstellung von Exposés*")

with col_logo2:
    if os.path.exists("logo_redseven.png"):
        st.image("logo_redseven.png", width=120)

st.divider()

# --- Upload ---
st.header("1️⃣ Unterlagen hochladen")

col1, col2, col3 = st.columns(3)

with col1:
    st.subheader("📄 Dokumente")
    doc_files = st.file_uploader("Casting-Bögen, PDFs, Word",
        type=["png", "jpg", "jpeg", "webp", "pdf", "docx"],
        accept_multiple_files=True, key="docs")
    if doc_files:
        st.success(f"✅ {len(doc_files)} Dokument(e)")

with col2:
    st.subheader("📷 Fotos")
    photo_files = st.file_uploader("Familie & Garten",
        type=["png", "jpg", "jpeg", "webp"],
        accept_multiple_files=True, key="photos")
    if photo_files:
        st.success(f"✅ {len(photo_files)} Foto(s)")

with col3:
    st.subheader("📝 Text (optional)")
    manual_text = st.text_area("Zusätzliche Infos", height=150)

st.divider()

# --- Verarbeitung ---
st.header("2️⃣ Analyse starten")

with st.expander("⚙️ Optionen"):
    col1, col2 = st.columns(2)
    with col1:
        max_image_size = st.slider("Bildgröße (px)", 512, 1024, 800, 128)
    with col2:
        fallback_delay = st.slider("Pause bei Fallback (Sek.)", 0, 60, 5, 5)

if st.button("🔍 KI-Analyse starten", type="primary", use_container_width=True):
    if not doc_files and not photo_files and not manual_text:
        st.error("Bitte Dateien hochladen oder Text eingeben.")
    else:
        try:
            extracted_text = manual_text or ""
            doc_images = []
            doc_names = []
            
            if doc_files:
                for f in doc_files:
                    f.seek(0)
                    if f.type == 'application/pdf':
                        extracted_text += "\n\n" + extract_text_from_pdf(f)
                    elif f.type == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document':
                        extracted_text += "\n\n" + extract_text_from_docx(f)
                    elif f.type.startswith('image/'):
                        img = compress_image(Image.open(f), max_size=max_image_size)
                        doc_images.append(img)
                        doc_names.append(f.name)
            
            st.info("📄 Extrahiere Informationen...")
            result = process_adaptive(doc_images, doc_names, extracted_text, delay=fallback_delay)
            st.session_state["extracted_content"] = result
            
            if photo_files:
                st.info("📷 Analysiere Fotos...")
                all_photos = [compress_image(Image.open(f), max_size=1200) for f in photo_files]
                all_photo_names = [f.name for f in photo_files]
                
                garden_indices, family_idx, duplicate_indices = analyze_photos(all_photos)
                
                st.session_state.update({
                    "all_photos": all_photos,
                    "all_photo_names": all_photo_names,
                    "garden_indices": garden_indices,
                    "family_idx": family_idx,
                    "duplicate_indices": duplicate_indices
                })
                
                if duplicate_indices:
                    st.warning(f"⚠️ {len(duplicate_indices)} Duplikate erkannt")
                if family_idx is not None:
                    st.success(f"✅ Familienfoto: {all_photo_names[family_idx]}")
            
            st.success("✅ Analyse abgeschlossen!")
            st.balloons()
            
        except Exception as e:
            st.error(f"Fehler: {str(e)}")

st.divider()

# --- Bearbeiten ---
st.header("3️⃣ Überprüfen & Bearbeiten")

if "extracted_content" in st.session_state:
    st.markdown("""
    **Markdown-Format:**
    - `# FAMILIENNAME | ORT` = Haupttitel
    - `## Überschrift` = Section-Header (grüne Box)
    - `- Text` = Aufzählung
    - `**fett**` = Fetter Text
    """)
    
    edited_content = st.text_area("Exposé (Markdown):",
        value=st.session_state["extracted_content"], height=350)
    
    # Vorschau
    with st.expander("👁️ Vorschau (Parsed)"):
        title_info, blocks = parse_markdown_content(edited_content)
        st.write(f"**Titel:** {title_info['name']} | {title_info['city']}")
        for block in blocks:
            st.write(f"**{block['title']}:** {len(block['items'])} Items")
    
    if "all_photos" in st.session_state and st.session_state["all_photos"]:
        st.subheader("📷 Foto-Auswahl")
        
        all_photos = st.session_state["all_photos"]
        all_names = st.session_state["all_photo_names"]
        family_idx = st.session_state.get("family_idx")
        duplicates = st.session_state.get("duplicate_indices", [])
        
        family_options = ["Keins"] + [f"{i+1}: {all_names[i]}" for i in range(len(all_photos))]
        default_family = 0 if family_idx is None else family_idx + 1
        selected_family = st.selectbox("Familienfoto (Seite 1)", range(len(family_options)),
                                       format_func=lambda x: family_options[x], index=default_family)
        
        st.markdown("**Fotos für Seite 2:**")
        cols = st.columns(4)
        selected_garden = []
        
        for i, (photo, name) in enumerate(zip(all_photos, all_names)):
            with cols[i % 4]:
                st.image(photo, width=140, caption=name[:15])
                status = "🔄" if i in duplicates else ("👨‍👩‍👧" if i == family_idx else "")
                if st.checkbox(f"Nutzen {status}", value=(i not in duplicates and i != family_idx), key=f"p_{i}"):
                    selected_garden.append(i)
        
        st.session_state["selected_family_idx"] = selected_family - 1 if selected_family > 0 else None
        st.session_state["selected_garden_indices"] = selected_garden
    
    st.divider()
    st.header("4️⃣ PDF exportieren")
    
    col1, col2 = st.columns([2, 1])
    with col1:
        family_name = st.text_input("Dateiname:", value="Expose_Familie")
    
    with col2:
        st.write("")
        st.write("")
        if st.button("📥 PDF erstellen", type="primary"):
            try:
                bg_path = "Background.jpg" if os.path.exists("Background.jpg") else None
                
                family_photo = None
                if "all_photos" in st.session_state:
                    fidx = st.session_state.get("selected_family_idx")
                    if fidx is not None and fidx >= 0:
                        family_photo = st.session_state["all_photos"][fidx]
                
                garden_photos = []
                garden_names = []
                if "all_photos" in st.session_state:
                    for idx in st.session_state.get("selected_garden_indices", []):
                        if idx != st.session_state.get("selected_family_idx"):
                            garden_photos.append(st.session_state["all_photos"][idx])
                            garden_names.append(st.session_state["all_photo_names"][idx])
                
                pdf_buffer = create_full_pdf(edited_content, family_photo, garden_photos, garden_names, bg_path)
                
                st.download_button("⬇️ PDF herunterladen", data=pdf_buffer,
                                  file_name=f"{family_name}.pdf", mime="application/pdf")
                
            except Exception as e:
                st.error(f"Fehler: {str(e)}")

else:
    st.info("👆 Erst Unterlagen hochladen und Analyse starten.")

st.divider()
st.caption("🔒 Daten werden nur temporär verarbeitet.")
