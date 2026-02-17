# app.py - Casting Exposé Generator v1.4
# Mit Einzelbild-Verarbeitung (1 Bild pro Minute)

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
model = genai.GenerativeModel("gemini-2.0-flash")

# --- Prompts ---
SINGLE_IMAGE_PROMPT = """
Analysiere dieses Dokument und extrahiere ALLE relevanten Informationen.

Extrahiere (falls vorhanden):
- Namen, Alter, Berufe der Familienmitglieder
- Adresse, Ort
- Informationen zum Garten (Größe, Zustand, Besonderheiten)
- Budget
- Wünsche und Pläne
- Persönliche Hintergründe
- TV-Erfahrung, Termine, Einschränkungen

Ignoriere Datenschutzerklärungen und rechtliche Texte.
Schreibe auf Deutsch. Wenn etwas unleserlich ist, schreibe [unleserlich].
"""

COMBINE_PROMPT = """
Kombiniere die folgenden extrahierten Informationen zu EINEM kompakten Exposé:

{extracted_infos}

---

Erstelle daraus ein Exposé mit dieser Struktur:

## FAMILIENNAME AUS ORT

**Familienmitglieder:**
(Name, Alter, Beruf - für jede Person)

**Fakten zum Garten:**
- Größe
- Besonderheiten

**Budget:** X €

**Wünsche für den Garten:**
- (Aufzählung, kurz und prägnant)

**Die Familie / Hintergrund:**
(2-3 Sätze, interessante Details)

**Besonderheiten / Notizen:**
(TV-Erfahrung, Termine etc.)

WICHTIG: Kurz, prägnant, keine Duplikate, auf Deutsch.
"""

# --- Hilfsfunktionen ---

def compress_image(image, max_size=800):
    """Komprimiert Bilder stark um Token zu sparen."""
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
    progress_bar = st.progress(0)
    countdown_text = st.empty()
    
    for i in range(seconds):
        remaining = seconds - i
        mins = remaining // 60
        secs = remaining % 60
        if mins > 0:
            countdown_text.text(f"⏱️ {message}... {mins}:{secs:02d}")
        else:
            countdown_text.text(f"⏱️ {message}... {secs} Sekunden")
        progress_bar.progress((i + 1) / seconds)
        time.sleep(1)
    
    countdown_text.empty()
    progress_bar.empty()


def call_gemini_safe(contents, max_retries=5):
    """Ruft Gemini API auf mit Retry"""
    for attempt in range(max_retries):
        try:
            response = model.generate_content(contents)
            return response.text
        
        except Exception as e:
            error_message = str(e)
            
            if "429" in error_message or "quota" in error_message.lower():
                wait_time = 65
                
                match = re.search(r'retry_delay.*?(\d+)', error_message)
                if match:
                    wait_time = int(match.group(1)) + 10
                
                if attempt < max_retries - 1:
                    st.warning(f"⏳ API-Limit. Warte {wait_time}s (Versuch {attempt + 1}/{max_retries})")
                    wait_with_countdown(wait_time, "Warte auf API")
                else:
                    raise Exception("API-Limit nach allen Versuchen erreicht.")
            else:
                raise e
    
    return None


def process_images_one_by_one(images, image_names, delay=65):
    """
    Verarbeitet Bilder einzeln mit 65 Sekunden Pause dazwischen.
    """
    extracted_parts = []
    total = len(images)
    
    overall_progress = st.progress(0)
    status_text = st.empty()
    
    for i, (img, name) in enumerate(zip(images, image_names)):
        status_text.markdown(f"### 🖼️ Verarbeite Bild {i+1}/{total}: `{name}`")
        
        # Einzelnes Bild an Gemini senden
        result = call_gemini_safe([SINGLE_IMAGE_PROMPT, img])
        
        if result:
            extracted_parts.append(f"--- Bild {i+1}: {name} ---\n{result}")
            st.success(f"✅ Bild {i+1}/{total} abgeschlossen")
        
        overall_progress.progress((i + 1) / total)
        
        # Warte vor nächstem Bild (außer beim letzten)
        if i < total - 1:
            status_text.markdown(f"### ⏳ Pause vor nächstem Bild...")
            wait_with_countdown(delay, f"Warte vor Bild {i+2}")
    
    status_text.empty()
    overall_progress.empty()
    
    return extracted_parts


def combine_extracted_parts(parts, additional_text=""):
    """Kombiniert die extrahierten Teile zu einem Exposé"""
    all_infos = "\n\n".join(parts)
    if additional_text:
        all_infos = f"--- Textdokumente ---\n{additional_text}\n\n{all_infos}"
    
    prompt = COMBINE_PROMPT.format(extracted_infos=all_infos)
    return call_gemini_safe([prompt])


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
        
        if len(image_files) > 1:
            estimated_time = (len(image_files) - 1) * 65 + len(image_files) * 10
            mins = estimated_time // 60
            secs = estimated_time % 60
            st.info(f"⏱️ Geschätzte Dauer: **{mins} Min {secs} Sek** (1 Bild pro Minute)")

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
        image_delay = st.slider("Pause zwischen Bildern (Sek.)", 60, 120, 70)

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
            
            # Verarbeitung
            if len(pil_images) == 0:
                # Nur Text
                result = call_gemini_safe([COMBINE_PROMPT.format(extracted_infos=extracted_text)])
            
            elif len(pil_images) == 1:
                # Einzelnes Bild
                st.info("📤 Sende Dokument an Gemini...")
                contents = [SINGLE_IMAGE_PROMPT, pil_images[0]]
                if extracted_text:
                    contents.append(f"\n\nZusätzliche Infos:\n{extracted_text}")
                result = call_gemini_safe(contents)
            
            else:
                # Mehrere Bilder: Einzelverarbeitung
                st.warning(f"🐢 Starte langsame Verarbeitung: {len(pil_images)} Bilder, 1 pro Minute")
                
                extracted_parts = process_images_one_by_one(
                    pil_images, 
                    image_names,
                    delay=image_delay
                )
                
                st.info("🔗 Kombiniere Ergebnisse...")
                wait_with_countdown(image_delay, "Pause vor Kombination")
                result = combine_extracted_parts(extracted_parts, extracted_text)
            
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
st.caption("🔒 Daten werden nur temporär verarbeitet.")
