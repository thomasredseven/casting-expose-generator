# app.py - Casting Exposé Generator v1.6
# Mit adaptiver Verarbeitung: schnell → mittel → langsam

import streamlit as st
import google.generativeai as genai
from PIL import Image
import io
import time
import re
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
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
model = genai.GenerativeModel("gemini-2.5-flash-preview-04-17")

# --- Prompts ---
EXTRACTION_PROMPT = """
Analysiere diese Casting-Unterlagen und extrahiere ALLE relevanten Informationen.

Erstelle ein kompaktes Exposé mit dieser Struktur:

## FAMILIENNAME AUS ORT

**Familienmitglieder:**
(Name, Alter, Beruf - für jede Person)

**Fakten zum Garten:**
- Größe
- Besonderheiten (Zugang, Haustyp etc.)

**Budget:** X €

**Wünsche für den Garten:**
- (Aufzählung, kurz und prägnant)

**Die Familie / Hintergrund:**
(2-3 Sätze, interessante Details hervorheben)

**Besonderheiten / Notizen:**
(TV-Erfahrung, Termine, Einschränkungen)

WICHTIG:
- Schreibe auf Deutsch
- Kurz und prägnant
- Ignoriere Datenschutzerklärungen
- Wenn etwas unleserlich ist, schreibe [unleserlich]
"""

SINGLE_IMAGE_PROMPT = """
Extrahiere ALLE Informationen aus diesem Dokument.
Schreibe auf Deutsch. Bei unleserlichem Text: [unleserlich].
"""

COMBINE_PROMPT = """
Kombiniere diese extrahierten Informationen zu EINEM kompakten Exposé:

{extracted_infos}

---

Struktur:

## FAMILIENNAME AUS ORT

**Familienmitglieder:**
(Name, Alter, Beruf)

**Fakten zum Garten:**
- Größe, Besonderheiten

**Budget:** X €

**Wünsche für den Garten:**
- (Aufzählung)

**Die Familie / Hintergrund:**
(2-3 Sätze)

**Besonderheiten / Notizen:**
(TV-Erfahrung, Termine etc.)

Kurz, prägnant, keine Duplikate, auf Deutsch.
"""

# --- Hilfsfunktionen ---

def compress_image(image, max_size=800):
    """Komprimiert Bilder"""
    ratio = min(max_size / image.width, max_size / image.height)
    if ratio < 1:
        new_size = (int(image.width * ratio), int(image.height * ratio))
        image = image.resize(new_size, Image.LANCZOS)
    
    if image.mode in ('RGBA', 'P'):
        image = image.convert('RGB')
    
    return image


def extract_text_from_pdf(pdf_file):
    """Extrahiert Text aus PDF"""
    pdf_bytes = pdf_file.read()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    text = ""
    for page in doc:
        text += page.get_text()
    doc.close()
    return text


def extract_text_from_docx(docx_file):
    """Extrahiert Text aus Word-Dokument"""
    doc = Document(docx_file)
    text = ""
    for para in doc.paragraphs:
        text += para.text + "\n"
    return text


def wait_with_countdown(seconds, message="Warte"):
    """Zeigt einen Countdown"""
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
    """Prüft ob es ein Rate-Limit-Fehler ist"""
    error_str = str(error).lower()
    return "429" in error_str or "quota" in error_str or "rate" in error_str or "limit" in error_str


def get_retry_delay(error):
    """Extrahiert Wartezeit aus Fehlermeldung"""
    match = re.search(r'retry_delay.*?(\d+)', str(error))
    if match:
        return int(match.group(1)) + 5
    return 30


def call_gemini(contents):
    """Einfacher Gemini-Aufruf ohne Retry"""
    response = model.generate_content(contents)
    return response.text


def call_gemini_with_retry(contents, max_retries=3):
    """Gemini-Aufruf mit Retry bei Rate-Limit"""
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


# --- Adaptive Verarbeitungsstrategien ---

def strategy_all_at_once(images, additional_text=""):
    """STUFE 1: Alle Bilder auf einmal senden"""
    contents = [EXTRACTION_PROMPT]
    
    if additional_text:
        contents.append(f"\n\nZusätzliche Infos aus Dokumenten:\n{additional_text}\n\n")
    
    contents.append("Hier sind die Dokumente:")
    for img in images:
        contents.append(img)
    
    return call_gemini(contents)


def strategy_in_batches(images, image_names, additional_text="", batch_size=3, delay=0):
    """STUFE 2: Bilder in Gruppen verarbeiten"""
    extracted_parts = []
    total_batches = (len(images) + batch_size - 1) // batch_size
    
    progress = st.progress(0)
    status = st.empty()
    
    for batch_num in range(total_batches):
        start_idx = batch_num * batch_size
        end_idx = min(start_idx + batch_size, len(images))
        batch_images = images[start_idx:end_idx]
        batch_names = image_names[start_idx:end_idx]
        
        status.markdown(f"### 📦 Verarbeite Gruppe {batch_num + 1}/{total_batches} ({len(batch_images)} Bilder)")
        
        contents = [SINGLE_IMAGE_PROMPT]
        for img in batch_images:
            contents.append(img)
        
        result = call_gemini_with_retry(contents)
        
        if result:
            extracted_parts.append(f"--- Gruppe {batch_num + 1}: {', '.join(batch_names)} ---\n{result}")
            st.success(f"✅ Gruppe {batch_num + 1}/{total_batches} abgeschlossen")
        
        progress.progress((batch_num + 1) / total_batches)
        
        if delay > 0 and batch_num < total_batches - 1:
            wait_with_countdown(delay, "Pause vor nächster Gruppe")
    
    progress.empty()
    status.empty()
    
    # Kombinieren
    all_infos = "\n\n".join(extracted_parts)
    if additional_text:
        all_infos = f"--- Textdokumente ---\n{additional_text}\n\n{all_infos}"
    
    return call_gemini_with_retry([COMBINE_PROMPT.format(extracted_infos=all_infos)])


def strategy_one_by_one(images, image_names, additional_text="", delay=0):
    """STUFE 3: Bilder einzeln verarbeiten"""
    extracted_parts = []
    total = len(images)
    
    progress = st.progress(0)
    status = st.empty()
    
    for i, (img, name) in enumerate(zip(images, image_names)):
        status.markdown(f"### 🖼️ Verarbeite Bild {i+1}/{total}: `{name}`")
        
        result = call_gemini_with_retry([SINGLE_IMAGE_PROMPT, img])
        
        if result:
            extracted_parts.append(f"--- Bild {i+1}: {name} ---\n{result}")
            st.success(f"✅ Bild {i+1}/{total} abgeschlossen")
        
        progress.progress((i + 1) / total)
        
        if delay > 0 and i < total - 1:
            wait_with_countdown(delay, f"Pause vor Bild {i+2}")
    
    progress.empty()
    status.empty()
    
    # Kombinieren
    all_infos = "\n\n".join(extracted_parts)
    if additional_text:
        all_infos = f"--- Textdokumente ---\n{additional_text}\n\n{all_infos}"
    
    return call_gemini_with_retry([COMBINE_PROMPT.format(extracted_infos=all_infos)])


def process_adaptive(images, image_names, additional_text="", delay=0):
    """
    Adaptive Verarbeitung: Startet schnell, wird bei Fehlern langsamer.
    """
    num_images = len(images)
    
    # Bei nur 1 Bild: direkt verarbeiten
    if num_images == 1:
        st.info("📤 Verarbeite einzelnes Dokument...")
        contents = [EXTRACTION_PROMPT, images[0]]
        if additional_text:
            contents.append(f"\n\nZusätzliche Infos:\n{additional_text}")
        return call_gemini_with_retry(contents)
    
    # STUFE 1: Alle auf einmal versuchen
    st.info(f"🚀 **Stufe 1:** Versuche alle {num_images} Bilder auf einmal...")
    
    try:
        result = strategy_all_at_once(images, additional_text)
        st.success("✅ Stufe 1 erfolgreich! Alle Bilder auf einmal verarbeitet.")
        return result
    
    except Exception as e:
        if is_rate_limit_error(e):
            st.warning(f"⚠️ Stufe 1 fehlgeschlagen (API-Limit). Wechsle zu Stufe 2...")
            
            # Kurz warten bevor nächste Stufe
            wait_time = get_retry_delay(e)
            wait_with_countdown(min(wait_time, 30), "Kurze Pause vor Stufe 2")
        else:
            raise e
    
    # STUFE 2: In 3er-Gruppen
    if num_images > 3:
        st.info(f"📦 **Stufe 2:** Verarbeite in 3er-Gruppen...")
        
        try:
            result = strategy_in_batches(images, image_names, additional_text, batch_size=3, delay=delay)
            st.success("✅ Stufe 2 erfolgreich!")
            return result
        
        except Exception as e:
            if is_rate_limit_error(e):
                st.warning(f"⚠️ Stufe 2 fehlgeschlagen. Wechsle zu Stufe 3...")
                wait_time = get_retry_delay(e)
                wait_with_countdown(min(wait_time, 30), "Kurze Pause vor Stufe 3")
            else:
                raise e
    
    # STUFE 3: Einzeln
    st.info(f"🐢 **Stufe 3:** Verarbeite Bilder einzeln (langsam aber sicher)...")
    
    result = strategy_one_by_one(images, image_names, additional_text, delay=max(delay, 5))
    st.success("✅ Stufe 3 erfolgreich!")
    return result


def create_pdf(content):
    """Erstellt ein PDF"""
    buffer = io.BytesIO()
    
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        rightMargin=2*cm, leftMargin=2*cm,
        topMargin=2*cm, bottomMargin=2*cm
    )
    
    styles = getSampleStyleSheet()
    
    title_style = ParagraphStyle(
        'CustomTitle', parent=styles['Heading1'],
        fontSize=18, spaceAfter=20, alignment=TA_CENTER,
        textColor=colors.HexColor('#2E7D32')
    )
    
    heading_style = ParagraphStyle(
        'CustomHeading', parent=styles['Heading2'],
        fontSize=12, spaceBefore=15, spaceAfter=8,
        textColor=colors.HexColor('#1565C0')
    )
    
    body_style = ParagraphStyle(
        'CustomBody', parent=styles['Normal'],
        fontSize=10, spaceAfter=6, leading=14
    )
    
    story = []
    
    for line in content.split('\n'):
        line = line.strip()
        if not line:
            story.append(Spacer(1, 6))
        elif line.startswith('## '):
            story.append(Paragraph(line[3:], title_style))
        elif line.startswith('**') and line.endswith('**'):
            story.append(Paragraph(line[2:-2], heading_style))
        elif line.startswith('**') and ':**' in line:
            story.append(Paragraph(line.replace('**', '<b>', 1).replace('**', '</b>', 1), body_style))
        elif line.startswith('- '):
            story.append(Paragraph('• ' + line[2:], body_style))
        else:
            story.append(Paragraph(line, body_style))
    
    doc.build(story)
    buffer.seek(0)
    return buffer


# --- UI ---
st.title("🎬 Casting Exposé Generator")
st.markdown("*Automatische Erstellung von Exposés aus Casting-Unterlagen*")

st.divider()

# --- Schritt 1: Upload ---
st.header("1️⃣ Unterlagen hochladen")

col1, col2 = st.columns(2)

with col1:
    st.subheader("📄 Dokumente & Bilder")
    uploaded_files = st.file_uploader(
        "Casting-Bögen, PDFs, Word-Dokumente",
        type=["png", "jpg", "jpeg", "webp", "pdf", "docx"],
        accept_multiple_files=True
    )
    
    if uploaded_files:
        image_files = [f for f in uploaded_files if f.type.startswith('image/')]
        pdf_files = [f for f in uploaded_files if f.type == 'application/pdf']
        docx_files = [f for f in uploaded_files if f.type == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document']
        
        st.success(f"✅ {len(uploaded_files)} Datei(en)")
        st.caption(f"📷 {len(image_files)} Bilder | 📄 {len(pdf_files)} PDFs | 📝 {len(docx_files)} Word")

with col2:
    st.subheader("📝 Text (optional)")
    manual_text = st.text_area("E-Mail-Text, Notizen etc.", height=150)

st.divider()

# --- Schritt 2: Verarbeitung ---
st.header("2️⃣ Informationen extrahieren")

with st.expander("⚙️ Optionen"):
    col1, col2 = st.columns(2)
    with col1:
        max_image_size = st.slider("Bildgröße (px)", 512, 1024, 800, 128)
    with col2:
        fallback_delay = st.slider("Pause bei Fallback (Sek.)", 0, 60, 5, 5)
    
    st.info("""
    **Adaptive Verarbeitung:**
    - Stufe 1: Alle Bilder auf einmal (schnellste)
    - Stufe 2: In 3er-Gruppen (bei API-Limit)
    - Stufe 3: Einzeln (langsamste, aber sicherste)
    """)

if st.button("🔍 KI-Analyse starten", type="primary", use_container_width=True):
    if not uploaded_files and not manual_text:
        st.error("Bitte Dateien hochladen oder Text eingeben.")
    else:
        try:
            # Text aus PDFs und Word extrahieren
            extracted_text = manual_text or ""
            
            for f in uploaded_files:
                f.seek(0)
                if f.type == 'application/pdf':
                    st.text(f"📄 Lese {f.name}...")
                    extracted_text += "\n\n" + extract_text_from_pdf(f)
                elif f.type == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document':
                    st.text(f"📝 Lese {f.name}...")
                    extracted_text += "\n\n" + extract_text_from_docx(f)
            
            # Bilder vorbereiten
            pil_images = []
            image_names = []
            
            for f in uploaded_files:
                f.seek(0)
                if f.type.startswith('image/'):
                    img = Image.open(f)
                    img = compress_image(img, max_size=max_image_size)
                    pil_images.append(img)
                    image_names.append(f.name)
            
            # Adaptive Verarbeitung
            if len(pil_images) == 0 and extracted_text:
                # Nur Text
                st.info("📤 Verarbeite Text...")
                result = call_gemini_with_retry([EXTRACTION_PROMPT + "\n\n" + extracted_text])
            else:
                # Bilder (adaptiv)
                result = process_adaptive(
                    pil_images, 
                    image_names, 
                    extracted_text,
                    delay=fallback_delay
                )
            
            st.session_state["extracted_content"] = result
            st.success("✅ Analyse abgeschlossen!")
            st.balloons()
            
        except Exception as e:
            st.error(f"Fehler: {str(e)}")

st.divider()

# --- Schritt 3 & 4: Bearbeiten & Export ---
st.header("3️⃣ Überprüfen & Bearbeiten")

if "extracted_content" in st.session_state:
    edited_content = st.text_area(
        "Exposé (bearbeitbar):",
        value=st.session_state["extracted_content"],
        height=400
    )
    
    st.divider()
    st.header("4️⃣ PDF exportieren")
    
    col1, col2 = st.columns([2, 1])
    with col1:
        family_name = st.text_input("Dateiname:", value="Expose_Familie")
    
    with col2:
        st.write("")
        st.write("")
        if st.button("📥 PDF erstellen", type="primary"):
            pdf_buffer = create_pdf(edited_content)
            st.download_button(
                "⬇️ PDF herunterladen",
                data=pdf_buffer,
                file_name=f"{family_name}.pdf",
                mime="application/pdf"
            )
else:
    st.info("👆 Erst Unterlagen hochladen und Analyse starten.")

st.divider()
st.caption("🔒 Daten werden nur temporär verarbeitet. | Modell: gemini-2.5-flash-preview | Adaptive Verarbeitung")
