# app.py - Casting Exposé Generator v3.2
# Fixes: DIE FAMILIE im PDF, mehr Abstand, Namen fett, Logos im UI

import streamlit as st
import google.generativeai as genai
from PIL import Image, ExifTags
import io
import time
import re
import os
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm, mm
from reportlab.lib import colors
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

# --- Prompts ---
EXTRACTION_PROMPT = """
Analysiere diese Casting-Unterlagen und erstelle ein KURZES, KNACKIGES Exposé.

Format EXAKT so:

FAMILIENNAME|||ORT

FAMILIENMITGLIEDER:
- Vorname (Alter), Beruf/Tätigkeit

FAKTEN ZUM GARTEN:
- Größe: X m²
- Wichtigste Besonderheiten in Stichpunkten

BUDGET: X €

WÜNSCHE FÜR DEN GARTEN:
- Hauptwunsch 1
- Hauptwunsch 2
- Hauptwunsch 3
(max. 5 Wünsche, kurz formuliert)

DIE FAMILIE:
Ein bis zwei Sätze: Wer sind sie und warum wollen sie umgestalten?

NOTIZEN:
Termine, Einschränkungen, Besonderes (nur wenn relevant)

REGELN:
- KURZ und KNACKIG
- Erste Zeile: FAMILIENNAME|||ORT
- Deutsch
"""

SINGLE_IMAGE_PROMPT = """
Extrahiere die wichtigsten Informationen aus diesem Dokument.
Kurz und stichpunktartig. Deutsch.
"""

COMBINE_PROMPT = """
Kombiniere zu EINEM kurzen, knackigen Exposé:

{extracted_infos}

---

Format:

FAMILIENNAME|||ORT

FAMILIENMITGLIEDER:
- Name (Alter), Beruf

FAKTEN ZUM GARTEN:
- Größe, wichtigste Fakten

BUDGET: X €

WÜNSCHE FÜR DEN GARTEN:
- Max. 5 Hauptwünsche

DIE FAMILIE:
1-2 Sätze zur Familie

NOTIZEN:
Nur wenn relevant

KURZ UND KNACKIG!
"""

PHOTO_ANALYSIS_PROMPT = """
Kategorisiere jedes Foto:
NUMMER|KATEGORIE|KURZBESCHREIBUNG

Kategorien: FAMILIE, GARTEN, HAUS, SONSTIGES
"""

# --- Hilfsfunktionen ---

def fix_image_orientation(image):
    """Korrigiert EXIF-Orientierung"""
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
        
        if orientation == 2:
            image = image.transpose(Image.FLIP_LEFT_RIGHT)
        elif orientation == 3:
            image = image.rotate(180, expand=True)
        elif orientation == 4:
            image = image.transpose(Image.FLIP_TOP_BOTTOM)
        elif orientation == 5:
            image = image.transpose(Image.FLIP_LEFT_RIGHT).rotate(270, expand=True)
        elif orientation == 6:
            image = image.rotate(270, expand=True)
        elif orientation == 7:
            image = image.transpose(Image.FLIP_LEFT_RIGHT).rotate(90, expand=True)
        elif orientation == 8:
            image = image.rotate(90, expand=True)
        
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
    img = image.copy()
    img = fix_image_orientation(img)
    img = img.convert('L')
    img = img.resize((hash_size + 1, hash_size), Image.LANCZOS)
    pixels = list(img.getdata())
    diff = []
    for row in range(hash_size):
        for col in range(hash_size):
            left = pixels[row * (hash_size + 1) + col]
            right = pixels[row * (hash_size + 1) + col + 1]
            diff.append(left > right)
    return tuple(diff)


def hamming_distance(hash1, hash2):
    return sum(a != b for a, b in zip(hash1, hash2))


def find_duplicates(images, threshold=10):
    hashes = [get_image_hash(img) for img in images]
    duplicates = set()
    for i in range(len(hashes)):
        for j in range(i + 1, len(hashes)):
            if hamming_distance(hashes[i], hashes[j]) < threshold:
                duplicates.add(j)
    return duplicates


def extract_text_from_pdf(pdf_file):
    pdf_bytes = pdf_file.read()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    text = ""
    for page in doc:
        text += page.get_text()
    doc.close()
    return text


def extract_text_from_docx(docx_file):
    doc = Document(docx_file)
    text = ""
    for para in doc.paragraphs:
        text += para.text + "\n"
    return text


def wait_with_countdown(seconds, message="Warte"):
    if seconds <= 0:
        return
    progress_bar = st.progress(0)
    countdown_text = st.empty()
    for i in range(seconds):
        remaining = seconds - i
        countdown_text.text(f"⏱️ {message}... {remaining}s")
        progress_bar.progress((i + 1) / seconds)
        time.sleep(1)
    countdown_text.empty()
    progress_bar.empty()


def is_rate_limit_error(error):
    error_str = str(error).lower()
    return "429" in error_str or "quota" in error_str or "rate" in error_str or "limit" in error_str


def get_retry_delay(error):
    match = re.search(r'retry_delay.*?(\d+)', str(error))
    if match:
        return int(match.group(1)) + 5
    return 30


def call_gemini(contents):
    response = model.generate_content(contents)
    return response.text


def call_gemini_with_retry(contents, max_retries=3):
    for attempt in range(max_retries):
        try:
            return call_gemini(contents)
        except Exception as e:
            if is_rate_limit_error(e) and attempt < max_retries - 1:
                wait_time = get_retry_delay(e)
                st.warning(f"⏳ Rate-Limit. Warte {wait_time}s... (Versuch {attempt + 1}/{max_retries})")
                wait_with_countdown(wait_time)
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
        contents = [PHOTO_ANALYSIS_PROMPT]
        for idx in unique_indices:
            contents.append(images[idx])
        
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
        
        garden_photos = [i for i in unique_indices if categories.get(i) in ['GARTEN', 'HAUS', 'SONSTIGES']]
        
        return garden_photos, family_photo_idx, list(duplicates)
        
    except Exception as e:
        st.warning(f"Foto-Analyse fehlgeschlagen: {e}")
        return unique_indices, None, list(duplicates)


# --- Adaptive Verarbeitung ---

def strategy_all_at_once(images, additional_text=""):
    contents = [EXTRACTION_PROMPT]
    if additional_text:
        contents.append(f"\n\nZusätzliche Infos:\n{additional_text}\n\n")
    contents.append("Dokumente:")
    for img in images:
        contents.append(img)
    return call_gemini(contents)


def strategy_in_batches(images, image_names, additional_text="", batch_size=3, delay=0):
    extracted_parts = []
    total_batches = (len(images) + batch_size - 1) // batch_size
    progress = st.progress(0)
    status = st.empty()
    
    for batch_num in range(total_batches):
        start_idx = batch_num * batch_size
        end_idx = min(start_idx + batch_size, len(images))
        batch_images = images[start_idx:end_idx]
        
        status.markdown(f"### 📦 Gruppe {batch_num + 1}/{total_batches}")
        contents = [SINGLE_IMAGE_PROMPT]
        for img in batch_images:
            contents.append(img)
        
        result = call_gemini_with_retry(contents)
        if result:
            extracted_parts.append(result)
        
        progress.progress((batch_num + 1) / total_batches)
        if delay > 0 and batch_num < total_batches - 1:
            wait_with_countdown(delay)
    
    progress.empty()
    status.empty()
    
    all_infos = "\n\n".join(extracted_parts)
    if additional_text:
        all_infos = f"Zusatzinfos:\n{additional_text}\n\n{all_infos}"
    return call_gemini_with_retry([COMBINE_PROMPT.format(extracted_infos=all_infos)])


def strategy_one_by_one(images, image_names, additional_text="", delay=0):
    extracted_parts = []
    total = len(images)
    progress = st.progress(0)
    status = st.empty()
    
    for i, (img, name) in enumerate(zip(images, image_names)):
        status.markdown(f"### 🖼️ Bild {i+1}/{total}")
        result = call_gemini_with_retry([SINGLE_IMAGE_PROMPT, img])
        if result:
            extracted_parts.append(result)
        progress.progress((i + 1) / total)
        if delay > 0 and i < total - 1:
            wait_with_countdown(delay)
    
    progress.empty()
    status.empty()
    
    all_infos = "\n\n".join(extracted_parts)
    if additional_text:
        all_infos = f"Zusatzinfos:\n{additional_text}\n\n{all_infos}"
    return call_gemini_with_retry([COMBINE_PROMPT.format(extracted_infos=all_infos)])


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
    
    st.info(f"🚀 **Stufe 1:** Alle {num_images} Dokumente auf einmal...")
    try:
        result = strategy_all_at_once(images, additional_text)
        st.success("✅ Stufe 1 erfolgreich!")
        return result
    except Exception as e:
        if is_rate_limit_error(e):
            st.warning("⚠️ Wechsle zu Stufe 2...")
            wait_with_countdown(min(get_retry_delay(e), 30))
        else:
            raise e
    
    if num_images > 3:
        st.info("📦 **Stufe 2:** 3er-Gruppen...")
        try:
            result = strategy_in_batches(images, image_names, additional_text, batch_size=3, delay=delay)
            st.success("✅ Stufe 2 erfolgreich!")
            return result
        except Exception as e:
            if is_rate_limit_error(e):
                st.warning("⚠️ Wechsle zu Stufe 3...")
                wait_with_countdown(min(get_retry_delay(e), 30))
            else:
                raise e
    
    st.info("🐢 **Stufe 3:** Einzeln...")
    result = strategy_one_by_one(images, image_names, additional_text, delay=max(delay, 5))
    st.success("✅ Stufe 3 erfolgreich!")
    return result


# --- Content Parser (VERBESSERT) ---

def parse_content(content):
    """Parst den KI-Output - VERBESSERTE VERSION"""
    data = {
        'family_name': 'FAMILIE',
        'city': 'ORT',
        'members': [],
        'garden_facts': [],
        'budget': '',
        'wishes': [],
        'background': '',
        'notes': ''
    }
    
    lines = content.strip().split('\n')
    current_section = None
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Erste Zeile: FAMILIENNAME|||ORT
        if '|||' in line and data['family_name'] == 'FAMILIE':
            parts = line.split('|||')
            data['family_name'] = parts[0].strip()
            data['city'] = parts[1].strip() if len(parts) > 1 else ''
            continue
        
        # Section Headers erkennen (case-insensitive)
        line_upper = line.upper().replace(':', '')
        
        if 'FAMILIENMITGLIEDER' in line_upper:
            current_section = 'members'
            continue
        elif 'FAKTEN' in line_upper and 'GARTEN' in line_upper:
            current_section = 'garden'
            continue
        elif line_upper.startswith('BUDGET'):
            budget_match = re.search(r'[\d.,]+', line)
            if budget_match:
                data['budget'] = budget_match.group(0).strip()
            current_section = None
            continue
        elif 'WÜNSCHE' in line_upper or 'WUENSCHE' in line_upper:
            current_section = 'wishes'
            continue
        elif line_upper == 'DIE FAMILIE' or line_upper.startswith('DIE FAMILIE'):
            current_section = 'background'
            continue
        elif 'NOTIZEN' in line_upper or 'BESONDERHEITEN' in line_upper:
            current_section = 'notes'
            continue
        
        # Content zu Sections hinzufügen
        if line.startswith('-') or line.startswith('•'):
            item = line[1:].strip()
            if current_section == 'members':
                data['members'].append(item)
            elif current_section == 'garden':
                data['garden_facts'].append(item)
            elif current_section == 'wishes':
                data['wishes'].append(item)
            elif current_section == 'background':
                data['background'] += item + ' '
            elif current_section == 'notes':
                data['notes'] += item + ' '
        else:
            # Normaler Text (ohne Aufzählungszeichen)
            if current_section == 'background':
                data['background'] += line + ' '
            elif current_section == 'notes':
                data['notes'] += line + ' '
    
    data['background'] = data['background'].strip()
    data['notes'] = data['notes'].strip()
    
    return data


# --- PDF-Erstellung ---

def draw_rounded_rect(c, x, y, width, height, radius, fill_color=None, alpha=0.88):
    c.saveState()
    if fill_color:
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


def draw_content_box(c, x, y, width, height):
    draw_rounded_rect(c, x, y, width, height, 8, fill_color=(255, 255, 255), alpha=0.88)


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


def format_member_name_bold(c, member, x, y):
    """Schreibt den Namen fett und den Rest normal"""
    # Versuche Name zu extrahieren (vor der Klammer oder vor dem Komma)
    match = re.match(r'^([^(,]+)(.*)$', member)
    if match:
        name = match.group(1).strip()
        rest = match.group(2).strip()
        
        # Name fett
        c.setFont("Helvetica-Bold", 9)
        c.drawString(x, y, f"• {name}")
        name_width = c.stringWidth(f"• {name} ", "Helvetica-Bold", 9)
        
        # Rest normal
        if rest:
            c.setFont("Helvetica", 9)
            c.drawString(x + name_width, y, rest)
    else:
        c.setFont("Helvetica", 9)
        c.drawString(x, y, f"• {member}")


def create_pdf_page1(c, data, family_photo=None, background_path=None):
    width, height = A4
    
    # Hintergrund
    if background_path and os.path.exists(background_path):
        try:
            c.drawImage(background_path, 0, 0, width=width, height=height, 
                       preserveAspectRatio=False, mask='auto')
        except:
            pass
    
    # --- Titel ---
    title_y = height - 122
    title_text = f"EXPOSÉ FAMILIE {data['family_name']} AUS {data['city']}"
    
    c.setFont("Helvetica-Bold", 15)
    c.setFillColorRGB(COLORS['title_green'][0]/255, 
                      COLORS['title_green'][1]/255, 
                      COLORS['title_green'][2]/255)
    
    title_width = c.stringWidth(title_text, "Helvetica-Bold", 15)
    c.drawString((width - title_width) / 2, title_y, title_text)
    
    # --- Layout ---
    margin_left = 20
    margin_right = 20
    content_width = width - margin_left - margin_right
    
    photo_width = (content_width / 2) - 10
    photo_height = 120
    photo_x = margin_left + 5
    
    members_x = margin_left + photo_width + 25
    members_width = content_width - photo_width - 35
    
    current_y = height - 145
    
    # Mehr Abstand unter Überschriften: text_start_offset
    text_start_offset = 32  # Erhöht von 26/28
    
    # --- Familienmitglieder + Foto ---
    if data['members']:
        box_height = max(len(data['members']) * 14 + 38, photo_height + 15)
        
        draw_content_box(c, margin_left, current_y - box_height, content_width, box_height)
        
        if family_photo:
            try:
                img_buffer = io.BytesIO()
                photo_corrected = fix_image_orientation(family_photo)
                photo_corrected.save(img_buffer, format='JPEG', quality=90)
                img_buffer.seek(0)
                
                c.drawImage(ImageReader(img_buffer), photo_x, current_y - box_height + 8, 
                           width=photo_width, height=photo_height, preserveAspectRatio=True, anchor='nw')
            except:
                pass
        
        draw_section_header(c, members_x - 5, current_y - 5, "Familienmitglieder:")
        
        c.setFillColorRGB(0.2, 0.2, 0.2)
        text_y = current_y - text_start_offset
        for member in data['members']:
            member_clean = member.replace('**', '').replace('*', '')
            format_member_name_bold(c, member_clean, members_x + 5, text_y)
            text_y -= 14
        
        current_y -= box_height + 8
    
    # --- Fakten zum Garten ---
    if data['garden_facts'] or data['budget']:
        facts_lines = []
        for fact in data['garden_facts']:
            wrapped = wrap_text(c, fact, content_width - 35)
            for i, line in enumerate(wrapped):
                facts_lines.append(f"• {line}" if i == 0 else f"  {line}")
        if data['budget']:
            facts_lines.append(f"• Budget: {data['budget']} €")
        
        box_height = len(facts_lines) * 12 + 32
        draw_content_box(c, margin_left, current_y - box_height, content_width, box_height)
        draw_section_header(c, margin_left + 5, current_y - 5, "Fakten zum Garten:")
        
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(0.2, 0.2, 0.2)
        text_y = current_y - text_start_offset
        for line in facts_lines:
            c.drawString(margin_left + 15, text_y, line)
            text_y -= 12
        
        current_y -= box_height + 8
    
    # --- Wünsche ---
    if data['wishes']:
        wish_lines = []
        for wish in data['wishes']:
            wish_clean = wish.replace('**', '').replace('*', '')
            wrapped = wrap_text(c, wish_clean, content_width - 35)
            for i, line in enumerate(wrapped):
                wish_lines.append(f"• {line}" if i == 0 else f"  {line}")
        
        box_height = len(wish_lines) * 12 + 32
        draw_content_box(c, margin_left, current_y - box_height, content_width, box_height)
        draw_section_header(c, margin_left + 5, current_y - 5, "Wünsche für den Garten:")
        
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(0.2, 0.2, 0.2)
        text_y = current_y - text_start_offset
        for line in wish_lines:
            c.drawString(margin_left + 15, text_y, line)
            text_y -= 12
        
        current_y -= box_height + 8
    
    # --- Die Familie (JETZT IMMER ANGEZEIGT) ---
    if data['background']:
        bg_lines = wrap_text(c, data['background'], content_width - 30)
        box_height = len(bg_lines) * 12 + 32
        draw_content_box(c, margin_left, current_y - box_height, content_width, box_height)
        draw_section_header(c, margin_left + 5, current_y - 5, "Die Familie:")
        
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(0.2, 0.2, 0.2)
        text_y = current_y - text_start_offset
        for line in bg_lines:
            c.drawString(margin_left + 15, text_y, line)
            text_y -= 12
        
        current_y -= box_height + 8
    
    # --- Notizen ---
    if data['notes']:
        notes_lines = wrap_text(c, data['notes'], content_width - 30)
        box_height = len(notes_lines) * 12 + 32
        draw_content_box(c, margin_left, current_y - box_height, content_width, box_height)
        draw_section_header(c, margin_left + 5, current_y - 5, "Notizen:")
        
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(0.2, 0.2, 0.2)
        text_y = current_y - text_start_offset
        for line in notes_lines:
            c.drawString(margin_left + 15, text_y, line)
            text_y -= 12


def create_pdf_page2(c, photos, photo_names, data, background_path=None):
    width, height = A4
    
    c.showPage()
    
    if background_path and os.path.exists(background_path):
        try:
            c.drawImage(background_path, 0, 0, width=width, height=height, 
                       preserveAspectRatio=False, mask='auto')
        except:
            pass
    
    title_y = height - 122
    title_text = f"FOTOS - FAMILIE {data['family_name']}"
    
    c.setFont("Helvetica-Bold", 15)
    c.setFillColorRGB(COLORS['title_green'][0]/255, 
                      COLORS['title_green'][1]/255, 
                      COLORS['title_green'][2]/255)
    
    title_width = c.stringWidth(title_text, "Helvetica-Bold", 15)
    c.drawString((width - title_width) / 2, title_y, title_text)
    
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
            photo_corrected = fix_image_orientation(photo)
            photo_corrected.save(img_buffer, format='JPEG', quality=90)
            img_buffer.seek(0)
            
            draw_rounded_rect(c, x - 5, y - photo_height - 5, col_width + 10, photo_height + 22, 5, 
                            fill_color=(255, 255, 255), alpha=0.9)
            
            c.drawImage(ImageReader(img_buffer), x, y - photo_height, 
                       width=col_width, height=photo_height, preserveAspectRatio=True)
            
            c.setFont("Helvetica", 7)
            c.setFillColorRGB(0.3, 0.3, 0.3)
            c.drawString(x, y - photo_height - 12, name[:50])
            
        except:
            pass


def create_full_pdf(content, family_photo=None, garden_photos=None, photo_names=None, background_path=None):
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    
    data = parse_content(content)
    
    create_pdf_page1(c, data, family_photo, background_path)
    
    if garden_photos:
        create_pdf_page2(c, garden_photos, photo_names or [], data, background_path)
    
    c.save()
    buffer.seek(0)
    return buffer


# --- UI MIT LOGOS ---

# Logos im Header anzeigen
col_logo1, col_title, col_logo2 = st.columns([1, 3, 1])

with col_logo1:
    if os.path.exists("logo_ddg.png"):
        st.image("logo_ddg.png", width=150)
    else:
        st.write("")  # Platzhalter

with col_title:
    st.title("🎬 Casting Exposé Generator")
    st.markdown("*Automatische Erstellung von Exposés*")

with col_logo2:
    if os.path.exists("logo_redseven.png"):
        st.image("logo_redseven.png", width=120)
    else:
        st.write("")  # Platzhalter

st.divider()

# --- Upload (3 Felder) ---
st.header("1️⃣ Unterlagen hochladen")

col1, col2, col3 = st.columns(3)

with col1:
    st.subheader("📄 Dokumente")
    doc_files = st.file_uploader(
        "Casting-Bögen, PDFs, Word",
        type=["png", "jpg", "jpeg", "webp", "pdf", "docx"],
        accept_multiple_files=True,
        key="docs"
    )
    if doc_files:
        st.success(f"✅ {len(doc_files)} Dokument(e)")

with col2:
    st.subheader("📷 Fotos")
    photo_files = st.file_uploader(
        "Familie & Garten",
        type=["png", "jpg", "jpeg", "webp"],
        accept_multiple_files=True,
        key="photos"
    )
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
                        st.text(f"📄 Lese {f.name}...")
                        extracted_text += "\n\n" + extract_text_from_pdf(f)
                    elif f.type == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document':
                        st.text(f"📝 Lese {f.name}...")
                        extracted_text += "\n\n" + extract_text_from_docx(f)
                    elif f.type.startswith('image/'):
                        img = Image.open(f)
                        img = compress_image(img, max_size=max_image_size)
                        doc_images.append(img)
                        doc_names.append(f.name)
            
            st.info("📄 Extrahiere Informationen...")
            result = process_adaptive(doc_images, doc_names, extracted_text, delay=fallback_delay)
            st.session_state["extracted_content"] = result
            
            if photo_files:
                st.info("📷 Analysiere Fotos...")
                
                all_photos = []
                all_photo_names = []
                
                for f in photo_files:
                    f.seek(0)
                    img = Image.open(f)
                    img = compress_image(img, max_size=1200)
                    all_photos.append(img)
                    all_photo_names.append(f.name)
                
                garden_indices, family_idx, duplicate_indices = analyze_photos(all_photos)
                
                st.session_state["all_photos"] = all_photos
                st.session_state["all_photo_names"] = all_photo_names
                st.session_state["garden_indices"] = garden_indices
                st.session_state["family_idx"] = family_idx
                st.session_state["duplicate_indices"] = duplicate_indices
                
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
    edited_content = st.text_area(
        "Exposé-Text:",
        value=st.session_state["extracted_content"],
        height=300
    )
    
    if "all_photos" in st.session_state and st.session_state["all_photos"]:
        st.subheader("📷 Foto-Auswahl")
        
        all_photos = st.session_state["all_photos"]
        all_names = st.session_state["all_photo_names"]
        family_idx = st.session_state.get("family_idx")
        duplicates = st.session_state.get("duplicate_indices", [])
        
        st.markdown("**Familienfoto (Seite 1):**")
        family_options = ["Keins"] + [f"{i+1}: {all_names[i]}" for i in range(len(all_photos))]
        default_family = 0 if family_idx is None else family_idx + 1
        
        selected_family = st.selectbox("Familienfoto", range(len(family_options)),
                                       format_func=lambda x: family_options[x], index=default_family)
        
        st.markdown("**Fotos für Seite 2:**")
        cols = st.columns(4)
        selected_garden = []
        
        for i, (photo, name) in enumerate(zip(all_photos, all_names)):
            with cols[i % 4]:
                st.image(photo, width=140, caption=name[:15])
                status = "🔄" if i in duplicates else ("👨‍👩‍👧" if i == family_idx else "")
                default = i not in duplicates and i != family_idx
                if st.checkbox(f"Nutzen {status}", value=default, key=f"p_{i}"):
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
                bg_path = "Background.jpg"
                if not os.path.exists(bg_path):
                    bg_path = None
                    st.warning("⚠️ Background.jpg nicht gefunden")
                
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
