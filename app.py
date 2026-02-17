# app.py - Casting Exposé Generator v1.7
# Mit schönerem PDF-Design und gemini-2.5-flash-preview

import streamlit as st
import google.generativeai as genai
from PIL import Image
import io
import time
import re
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm, mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.pdfgen import canvas
from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate
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

# --- Farben (angelehnt an den Entwurf) ---
COLORS = {
    'primary_green': colors.HexColor('#4A7C23'),      # Dunkelgrün für Titel
    'light_green': colors.HexColor('#E8F5E9'),        # Hellgrün für Hintergrund
    'accent_green': colors.HexColor('#81C784'),       # Akzentgrün
    'header_bg': colors.HexColor('#2E7D32'),          # Header-Hintergrund
    'section_bg': colors.HexColor('#F1F8E9'),         # Section-Hintergrund
    'text_dark': colors.HexColor('#1B5E20'),          # Dunkler Text
    'text_body': colors.HexColor('#333333'),          # Body-Text
    'white': colors.white,
    'border': colors.HexColor('#A5D6A7'),             # Rahmenfarbe
}

# --- Prompts ---
EXTRACTION_PROMPT = """
Analysiere diese Casting-Unterlagen und extrahiere ALLE relevanten Informationen.

Erstelle ein kompaktes Exposé mit EXAKT dieser Struktur (verwende genau diese Überschriften):

## FAMILIENNAME AUS ORT

**Familienmitglieder:**
- Name (Alter), Beruf
- Name (Alter), Beruf
(für jede Person eine Zeile)

**Fakten zum Garten:**
- Größe: X m²
- Besonderheiten: ...

**Budget:** X €

**Wünsche für den Garten:**
- Wunsch 1
- Wunsch 2
- Wunsch 3

**Die Familie / Hintergrund:**
2-3 Sätze zur Familie und warum sie den Garten umgestalten wollen.

**Besonderheiten / Notizen:**
TV-Erfahrung, Termine, Einschränkungen (falls vorhanden)

WICHTIG:
- Schreibe auf Deutsch
- Kurz und prägnant
- Keine Einleitung, direkt mit ## FAMILIENNAME beginnen
- Ignoriere Datenschutzerklärungen
"""

SINGLE_IMAGE_PROMPT = """
Extrahiere ALLE Informationen aus diesem Dokument.
Schreibe auf Deutsch. Bei unleserlichem Text: [unleserlich].
"""

COMBINE_PROMPT = """
Kombiniere diese extrahierten Informationen zu EINEM kompakten Exposé:

{extracted_infos}

---

Verwende EXAKT diese Struktur:

## FAMILIENNAME AUS ORT

**Familienmitglieder:**
- Name (Alter), Beruf

**Fakten zum Garten:**
- Größe: X m²
- Besonderheiten: ...

**Budget:** X €

**Wünsche für den Garten:**
- Wunsch 1
- Wunsch 2

**Die Familie / Hintergrund:**
2-3 Sätze

**Besonderheiten / Notizen:**
Falls vorhanden

Keine Einleitung, direkt mit ## beginnen. Kurz, prägnant, auf Deutsch.
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
    """Einfacher Gemini-Aufruf"""
    response = model.generate_content(contents)
    return response.text


def call_gemini_with_retry(contents, max_retries=3):
    """Gemini-Aufruf mit Retry"""
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


# --- Adaptive Verarbeitung ---

def strategy_all_at_once(images, additional_text=""):
    """STUFE 1: Alle Bilder auf einmal"""
    contents = [EXTRACTION_PROMPT]
    if additional_text:
        contents.append(f"\n\nZusätzliche Infos:\n{additional_text}\n\n")
    contents.append("Dokumente:")
    for img in images:
        contents.append(img)
    return call_gemini(contents)


def strategy_in_batches(images, image_names, additional_text="", batch_size=3, delay=0):
    """STUFE 2: In Gruppen"""
    extracted_parts = []
    total_batches = (len(images) + batch_size - 1) // batch_size
    
    progress = st.progress(0)
    status = st.empty()
    
    for batch_num in range(total_batches):
        start_idx = batch_num * batch_size
        end_idx = min(start_idx + batch_size, len(images))
        batch_images = images[start_idx:end_idx]
        batch_names = image_names[start_idx:end_idx]
        
        status.markdown(f"### 📦 Gruppe {batch_num + 1}/{total_batches}")
        
        contents = [SINGLE_IMAGE_PROMPT]
        for img in batch_images:
            contents.append(img)
        
        result = call_gemini_with_retry(contents)
        if result:
            extracted_parts.append(f"--- Gruppe {batch_num + 1} ---\n{result}")
        
        progress.progress((batch_num + 1) / total_batches)
        
        if delay > 0 and batch_num < total_batches - 1:
            wait_with_countdown(delay)
    
    progress.empty()
    status.empty()
    
    all_infos = "\n\n".join(extracted_parts)
    if additional_text:
        all_infos = f"--- Textdokumente ---\n{additional_text}\n\n{all_infos}"
    
    return call_gemini_with_retry([COMBINE_PROMPT.format(extracted_infos=all_infos)])


def strategy_one_by_one(images, image_names, additional_text="", delay=0):
    """STUFE 3: Einzeln"""
    extracted_parts = []
    total = len(images)
    
    progress = st.progress(0)
    status = st.empty()
    
    for i, (img, name) in enumerate(zip(images, image_names)):
        status.markdown(f"### 🖼️ Bild {i+1}/{total}: `{name}`")
        
        result = call_gemini_with_retry([SINGLE_IMAGE_PROMPT, img])
        if result:
            extracted_parts.append(f"--- Bild {i+1} ---\n{result}")
        
        progress.progress((i + 1) / total)
        
        if delay > 0 and i < total - 1:
            wait_with_countdown(delay)
    
    progress.empty()
    status.empty()
    
    all_infos = "\n\n".join(extracted_parts)
    if additional_text:
        all_infos = f"--- Textdokumente ---\n{additional_text}\n\n{all_infos}"
    
    return call_gemini_with_retry([COMBINE_PROMPT.format(extracted_infos=all_infos)])


def process_adaptive(images, image_names, additional_text="", delay=0):
    """Adaptive Verarbeitung"""
    num_images = len(images)
    
    if num_images == 1:
        st.info("📤 Verarbeite Dokument...")
        contents = [EXTRACTION_PROMPT, images[0]]
        if additional_text:
            contents.append(f"\n\nZusätzliche Infos:\n{additional_text}")
        return call_gemini_with_retry(contents)
    
    # STUFE 1
    st.info(f"🚀 **Stufe 1:** Alle {num_images} Bilder auf einmal...")
    try:
        result = strategy_all_at_once(images, additional_text)
        st.success("✅ Stufe 1 erfolgreich!")
        return result
    except Exception as e:
        if is_rate_limit_error(e):
            st.warning("⚠️ Stufe 1 fehlgeschlagen. Wechsle zu Stufe 2...")
            wait_with_countdown(min(get_retry_delay(e), 30))
        else:
            raise e
    
    # STUFE 2
    if num_images > 3:
        st.info("📦 **Stufe 2:** 3er-Gruppen...")
        try:
            result = strategy_in_batches(images, image_names, additional_text, batch_size=3, delay=delay)
            st.success("✅ Stufe 2 erfolgreich!")
            return result
        except Exception as e:
            if is_rate_limit_error(e):
                st.warning("⚠️ Stufe 2 fehlgeschlagen. Wechsle zu Stufe 3...")
                wait_with_countdown(min(get_retry_delay(e), 30))
            else:
                raise e
    
    # STUFE 3
    st.info("🐢 **Stufe 3:** Einzeln...")
    result = strategy_one_by_one(images, image_names, additional_text, delay=max(delay, 5))
    st.success("✅ Stufe 3 erfolgreich!")
    return result


# --- PDF-Erstellung (NEUES DESIGN) ---

def draw_page_background(canvas, doc):
    """Zeichnet den Seitenhintergrund"""
    width, height = A4
    
    # Header-Bereich (grüner Balken oben)
    canvas.setFillColor(COLORS['header_bg'])
    canvas.rect(0, height - 2.5*cm, width, 2.5*cm, fill=True, stroke=False)
    
    # Subtiler grüner Rand unten
    canvas.setFillColor(COLORS['accent_green'])
    canvas.rect(0, 0, width, 0.5*cm, fill=True, stroke=False)
    
    # Linker Akzentstreifen
    canvas.setFillColor(COLORS['light_green'])
    canvas.rect(0, 0.5*cm, 0.5*cm, height - 3*cm, fill=True, stroke=False)


def create_styled_pdf(content, show_name="Duell der Gartenprofis"):
    """Erstellt ein schön gestaltetes PDF"""
    buffer = io.BytesIO()
    
    # Dokument mit custom PageTemplate
    doc = BaseDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=1.5*cm,
        leftMargin=1.5*cm,
        topMargin=3.5*cm,
        bottomMargin=1.5*cm
    )
    
    # Frame für den Inhalt
    frame = Frame(
        doc.leftMargin,
        doc.bottomMargin,
        doc.width,
        doc.height,
        id='normal'
    )
    
    # PageTemplate mit Hintergrund
    template = PageTemplate(
        id='styled',
        frames=frame,
        onPage=draw_page_background
    )
    doc.addPageTemplates([template])
    
    # Styles definieren
    styles = getSampleStyleSheet()
    
    # Showname im Header
    show_style = ParagraphStyle(
        'ShowName',
        parent=styles['Normal'],
        fontSize=14,
        textColor=COLORS['white'],
        alignment=TA_CENTER,
        fontName='Helvetica-Bold',
        spaceAfter=0
    )
    
    # Haupttitel (Familienname)
    title_style = ParagraphStyle(
        'MainTitle',
        parent=styles['Heading1'],
        fontSize=22,
        textColor=COLORS['primary_green'],
        alignment=TA_CENTER,
        fontName='Helvetica-Bold',
        spaceBefore=10,
        spaceAfter=15,
        borderWidth=0,
        borderColor=COLORS['border'],
        borderPadding=5
    )
    
    # Section Headers
    section_style = ParagraphStyle(
        'SectionHeader',
        parent=styles['Heading2'],
        fontSize=13,
        textColor=COLORS['text_dark'],
        fontName='Helvetica-Bold',
        spaceBefore=12,
        spaceAfter=6,
        borderWidth=0,
        borderPadding=0,
        underlineWidth=1,
        underlineColor=COLORS['accent_green']
    )
    
    # Body Text
    body_style = ParagraphStyle(
        'BodyText',
        parent=styles['Normal'],
        fontSize=10,
        textColor=COLORS['text_body'],
        fontName='Helvetica',
        spaceAfter=4,
        leading=14
    )
    
    # Aufzählungen
    bullet_style = ParagraphStyle(
        'BulletPoint',
        parent=body_style,
        leftIndent=15,
        bulletIndent=5,
        spaceAfter=3
    )
    
    # Budget (hervorgehoben)
    budget_style = ParagraphStyle(
        'Budget',
        parent=styles['Normal'],
        fontSize=12,
        textColor=COLORS['primary_green'],
        fontName='Helvetica-Bold',
        spaceBefore=8,
        spaceAfter=8,
        backColor=COLORS['light_green'],
        borderWidth=1,
        borderColor=COLORS['border'],
        borderPadding=8,
        borderRadius=3
    )
    
    # Story aufbauen
    story = []
    
    # Content parsen
    lines = content.split('\n')
    current_section = None
    
    for line in lines:
        line = line.strip()
        
        if not line:
            story.append(Spacer(1, 4))
            continue
        
        # Haupttitel (## FAMILIENNAME)
        if line.startswith('## '):
            title_text = line[3:].strip()
            story.append(Paragraph(title_text, title_style))
            story.append(Spacer(1, 10))
            continue
        
        # Section Header (**Überschrift:**)
        if line.startswith('**') and line.endswith(':**'):
            section_text = line[2:-3].strip()
            
            # Dekorative Linie vor Section
            story.append(Spacer(1, 8))
            
            # Section mit Unterstrich-Effekt
            section_html = f'<u>{section_text}:</u>'
            story.append(Paragraph(section_html, section_style))
            current_section = section_text.lower()
            continue
        
        # Section Header alternative (**Überschrift**)
        if line.startswith('**') and line.endswith('**') and ':**' not in line:
            section_text = line[2:-2].strip()
            story.append(Spacer(1, 8))
            story.append(Paragraph(f'<u>{section_text}</u>', section_style))
            continue
        
        # Budget (spezielle Formatierung)
        if line.lower().startswith('**budget'):
            budget_text = line.replace('**', '').strip()
            story.append(Paragraph(f'💰 {budget_text}', budget_style))
            continue
        
        # Aufzählungspunkte
        if line.startswith('- ') or line.startswith('• ') or line.startswith('* '):
            bullet_text = line[2:].strip()
            # Fett-Formatierung erhalten
            bullet_text = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', bullet_text)
            story.append(Paragraph(f'• {bullet_text}', bullet_style))
            continue
        
        # Inline **fett** ersetzen
        if '**' in line:
            line = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', line)
        
        # Normaler Text
        story.append(Paragraph(line, body_style))
    
    # Footer-Spacer
    story.append(Spacer(1, 20))
    
    # PDF bauen
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
    col1, col2, col3 = st.columns(3)
    with col1:
        max_image_size = st.slider("Bildgröße (px)", 512, 1024, 800, 128)
    with col2:
        fallback_delay = st.slider("Pause bei Fallback (Sek.)", 0, 60, 5, 5)
    with col3:
        show_name = st.text_input("Show-Name (für PDF)", value="Duell der Gartenprofis")

if st.button("🔍 KI-Analyse starten", type="primary", use_container_width=True):
    if not uploaded_files and not manual_text:
        st.error("Bitte Dateien hochladen oder Text eingeben.")
    else:
        try:
            extracted_text = manual_text or ""
            
            for f in uploaded_files:
                f.seek(0)
                if f.type == 'application/pdf':
                    st.text(f"📄 Lese {f.name}...")
                    extracted_text += "\n\n" + extract_text_from_pdf(f)
                elif f.type == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document':
                    st.text(f"📝 Lese {f.name}...")
                    extracted_text += "\n\n" + extract_text_from_docx(f)
            
            pil_images = []
            image_names = []
            
            for f in uploaded_files:
                f.seek(0)
                if f.type.startswith('image/'):
                    img = Image.open(f)
                    img = compress_image(img, max_size=max_image_size)
                    pil_images.append(img)
                    image_names.append(f.name)
            
            if len(pil_images) == 0 and extracted_text:
                st.info("📤 Verarbeite Text...")
                result = call_gemini_with_retry([EXTRACTION_PROMPT + "\n\n" + extracted_text])
            else:
                result = process_adaptive(pil_images, image_names, extracted_text, delay=fallback_delay)
            
            st.session_state["extracted_content"] = result
            st.session_state["show_name"] = show_name
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
            try:
                pdf_buffer = create_styled_pdf(
                    edited_content, 
                    show_name=st.session_state.get("show_name", "Duell der Gartenprofis")
                )
                st.download_button(
                    "⬇️ PDF herunterladen",
                    data=pdf_buffer,
                    file_name=f"{family_name}.pdf",
                    mime="application/pdf"
                )
            except Exception as e:
                st.error(f"PDF-Fehler: {str(e)}")
    
    # Vorschau
    with st.expander("👁️ Text-Vorschau"):
        st.markdown(edited_content)

else:
    st.info("👆 Erst Unterlagen hochladen und Analyse starten.")

st.divider()
st.caption("🔒 Daten werden nur temporär verarbeitet. | Modell: gemini-2.5-flash-preview")
