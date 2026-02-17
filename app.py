# app.py - Casting Exposé Generator v1.2
# Mit Bildkomprimierung, Retry-Logik und Warte-Dialog

import streamlit as st
import google.generativeai as genai
from PIL import Image
import io
import time
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
import fitz  # PyMuPDF für PDF
from docx import Document  # python-docx für Word

# --- Konfiguration ---
st.set_page_config(
    page_title="Casting Exposé Generator",
    page_icon="🎬",
    layout="wide"
)

# Gemini API Setup
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.0-flash")

# --- Prompt für Extraktion ---
EXTRACTION_PROMPT = """
Analysiere die folgenden Casting-Unterlagen und extrahiere die wichtigsten Informationen.

Erstelle daraus ein kompaktes Exposé mit folgender Struktur:

## FAMILIENNAME AUS ORT

**Familienmitglieder:**
(Name, Alter, Beruf - für jede Person)

**Fakten zum Garten:**
- Größe
- Besonderheiten (Zugang, Haustyp etc.)

**Budget:** X €

**Wünsche für den Garten:**
- (Aufzählung der wichtigsten Wünsche, kurz und prägnant)

**Die Familie / Hintergrund:**
(2-3 Sätze zur Familie und warum sie den Garten umgestalten wollen. Interessante Details hervorheben.)

**Besonderheiten / Notizen:**
(Falls relevant: TV-Erfahrung, Termine, Einschränkungen)

WICHTIG:
- Schreibe auf Deutsch
- Fasse dich kurz und prägnant
- Nur relevante, interessante Informationen
- Ignoriere Datenschutzerklärungen und rechtliche Texte
- Das Exposé soll auf eine Seite passen
"""

# --- Hilfsfunktionen ---

def compress_image(image, max_size=1024, quality=85):
    """
    Komprimiert Bilder um Token zu sparen.
    Reduziert Auflösung auf max 1024px und optimiert Qualität.
    """
    # Größe reduzieren wenn nötig
    ratio = min(max_size / image.width, max_size / image.height)
    if ratio < 1:
        new_size = (int(image.width * ratio), int(image.height * ratio))
        image = image.resize(new_size, Image.LANCZOS)
    
    # In RGB konvertieren falls nötig (für JPEG-Kompatibilität)
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


def call_gemini_with_retry(contents, max_retries=3, initial_wait=45):
    """
    Ruft Gemini API auf mit automatischem Retry bei Rate-Limit-Fehlern.
    Zeigt einen Countdown während der Wartezeit.
    """
    for attempt in range(max_retries):
        try:
            response = model.generate_content(contents)
            return response.text
        
        except Exception as e:
            error_message = str(e)
            
            # Prüfen ob es ein Rate-Limit-Fehler ist
            if "429" in error_message or "quota" in error_message.lower() or "rate" in error_message.lower():
                
                # Wartezeit aus Fehlermeldung extrahieren oder Standard verwenden
                wait_time = initial_wait
                if "retry_delay" in error_message:
                    try:
                        # Versuche Wartezeit aus Fehler zu parsen
                        import re
                        match = re.search(r'retry_delay.*?(\d+)', error_message)
                        if match:
                            wait_time = int(match.group(1)) + 5  # +5 Sekunden Puffer
                    except:
                        pass
                
                if attempt < max_retries - 1:
                    # Warte-Dialog mit Countdown anzeigen
                    st.warning(f"⏳ API-Limit erreicht. Warte {wait_time} Sekunden vor erneutem Versuch... (Versuch {attempt + 1}/{max_retries})")
                    
                    # Countdown anzeigen
                    progress_bar = st.progress(0)
                    countdown_text = st.empty()
                    
                    for i in range(wait_time):
                        remaining = wait_time - i
                        countdown_text.text(f"⏱️ Noch {remaining} Sekunden...")
                        progress_bar.progress((i + 1) / wait_time)
                        time.sleep(1)
                    
                    countdown_text.empty()
                    progress_bar.empty()
                    st.info("🔄 Versuche erneut...")
                else:
                    # Alle Versuche aufgebraucht
                    raise Exception(
                        f"API-Limit auch nach {max_retries} Versuchen noch erreicht. "
                        f"Bitte warte einige Minuten und versuche es dann erneut, "
                        f"oder verwende weniger/kleinere Bilder."
                    )
            else:
                # Anderer Fehler - direkt weitergeben
                raise e
    
    return None


def extract_info_combined(images, text):
    """Kombiniert Bilder und Text für Extraktion"""
    contents = [EXTRACTION_PROMPT + "\n\nHier sind die Unterlagen:\n"]
    
    if text:
        contents.append(f"TEXTUELLE INFORMATIONEN:\n{text}\n\n")
    
    if images:
        contents.append("GESCANNTE DOKUMENTE/BILDER:")
        for img in images:
            contents.append(img)
    
    # Mit Retry-Logik aufrufen
    return call_gemini_with_retry(contents)


def extract_info_from_images(images):
    """Extrahiert Informationen aus Bildern via Gemini"""
    contents = [EXTRACTION_PROMPT]
    for img in images:
        contents.append(img)
    
    return call_gemini_with_retry(contents)


def extract_info_from_text(text):
    """Extrahiert Informationen aus Text via Gemini"""
    prompt = EXTRACTION_PROMPT + "\n\nHier sind die Unterlagen:\n\n" + text
    
    return call_gemini_with_retry([prompt])


def create_pdf(content, title="Exposé"):
    """Erstellt ein PDF aus dem Markdown-Content"""
    buffer = io.BytesIO()
    
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=2*cm,
        leftMargin=2*cm,
        topMargin=2*cm,
        bottomMargin=2*cm
    )
    
    styles = getSampleStyleSheet()
    
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=18,
        spaceAfter=20,
        alignment=TA_CENTER,
        textColor=colors.HexColor('#2E7D32')
    )
    
    heading_style = ParagraphStyle(
        'CustomHeading',
        parent=styles['Heading2'],
        fontSize=12,
        spaceBefore=15,
        spaceAfter=8,
        textColor=colors.HexColor('#1565C0')
    )
    
    body_style = ParagraphStyle(
        'CustomBody',
        parent=styles['Normal'],
        fontSize=10,
        spaceAfter=6,
        leading=14
    )
    
    story = []
    
    lines = content.split('\n')
    for line in lines:
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
    st.subheader("📄 Scans, Bilder & Dokumente")
    uploaded_files = st.file_uploader(
        "Casting-Bögen, Protokolle, PDFs, Word-Dokumente",
        type=["png", "jpg", "jpeg", "webp", "pdf", "docx"],
        accept_multiple_files=True,
        help="Fotos, Scans, PDFs oder Word-Dokumente"
    )
    
    if uploaded_files:
        # Dateien kategorisieren
        image_files = [f for f in uploaded_files if f.type.startswith('image/')]
        pdf_files = [f for f in uploaded_files if f.type == 'application/pdf']
        docx_files = [f for f in uploaded_files if f.type == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document']
        
        st.success(f"✅ {len(uploaded_files)} Datei(en) hochgeladen")
        st.caption(f"📷 {len(image_files)} Bilder | 📄 {len(pdf_files)} PDFs | 📝 {len(docx_files)} Word-Dokumente")
        
        # Warnung bei vielen Bildern
        if len(image_files) > 5:
            st.warning(
                f"⚠️ {len(image_files)} Bilder hochgeladen. "
                f"Bilder werden automatisch komprimiert um API-Limits zu vermeiden. "
                f"Bei Problemen: Versuche es mit weniger Bildern (3-5 reichen meist)."
            )
        
        with st.expander("Vorschau Bilder"):
            if image_files:
                cols = st.columns(min(len(image_files), 3))
                for i, img_file in enumerate(image_files):
                    with cols[i % 3]:
                        st.image(img_file, use_container_width=True)
            else:
                st.info("Keine Bilder hochgeladen")

with col2:
    st.subheader("📝 Text (optional)")
    manual_text = st.text_area(
        "E-Mail-Text, Notizen etc.",
        height=200,
        placeholder="Hier können Sie zusätzlichen Text einfügen, z.B. aus E-Mails oder Protokollen..."
    )

st.divider()

# --- Schritt 2: Verarbeitung ---
st.header("2️⃣ Informationen extrahieren")

# Optionen für Bildkomprimierung
with st.expander("⚙️ Erweiterte Optionen"):
    col1, col2 = st.columns(2)
    with col1:
        max_image_size = st.slider(
            "Max. Bildgröße (Pixel)",
            min_value=512,
            max_value=2048,
            value=1024,
            step=256,
            help="Kleinere Bilder = weniger Token = schneller & günstiger"
        )
    with col2:
        max_retries = st.slider(
            "Max. Wiederholungsversuche",
            min_value=1,
            max_value=5,
            value=3,
            help="Wie oft bei API-Limit automatisch erneut versucht werden soll"
        )

if st.button("🔍 KI-Analyse starten", type="primary", use_container_width=True):
    if not uploaded_files and not manual_text:
        st.error("Bitte laden Sie mindestens eine Datei hoch oder geben Sie Text ein.")
    else:
        with st.spinner("Analysiere Unterlagen mit Gemini..."):
            try:
                # Text aus PDFs und Word-Dokumenten extrahieren
                extracted_text = manual_text or ""
                
                # Status-Updates
                status = st.empty()
                
                # PDFs und Word-Dokumente verarbeiten
                for f in uploaded_files:
                    f.seek(0)
                    if f.type == 'application/pdf':
                        status.text(f"📄 Extrahiere Text aus {f.name}...")
                        extracted_text += "\n\n--- PDF-DOKUMENT ---\n" + extract_text_from_pdf(f)
                    elif f.type == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document':
                        status.text(f"📝 Extrahiere Text aus {f.name}...")
                        extracted_text += "\n\n--- WORD-DOKUMENT ---\n" + extract_text_from_docx(f)
                
                # Bilder vorbereiten (MIT KOMPRIMIERUNG)
                pil_images = []
                image_count = len([f for f in uploaded_files if f.type.startswith('image/')])
                
                for i, f in enumerate(uploaded_files):
                    f.seek(0)
                    if f.type.startswith('image/'):
                        status.text(f"🖼️ Komprimiere Bild {i+1}/{image_count}: {f.name}...")
                        img = Image.open(f)
                        
                        # Komprimierung anwenden
                        original_size = f"{img.width}x{img.height}"
                        img = compress_image(img, max_size=max_image_size)
                        new_size = f"{img.width}x{img.height}"
                        
                        pil_images.append(img)
                
                status.text("🤖 Sende an Gemini API...")
                
                # Extraktion mit Retry-Logik
                if pil_images and extracted_text:
                    result = extract_info_combined(pil_images, extracted_text)
                elif pil_images:
                    result = extract_info_from_images(pil_images)
                else:
                    result = extract_info_from_text(extracted_text)
                
                status.empty()
                st.session_state["extracted_content"] = result
                st.success("✅ Analyse abgeschlossen!")
                
            except Exception as e:
                st.error(f"Fehler bei der Analyse: {str(e)}")

st.divider()

# --- Schritt 3: Bearbeiten ---
st.header("3️⃣ Überprüfen & Bearbeiten")

if "extracted_content" in st.session_state:
    edited_content = st.text_area(
        "Extrahiertes Exposé (bearbeitbar):",
        value=st.session_state["extracted_content"],
        height=400,
        help="Hier können Sie den Text anpassen, bevor Sie das PDF erstellen."
    )
    
    st.session_state["edited_content"] = edited_content
    
    st.divider()
    
    # --- Schritt 4: Export ---
    st.header("4️⃣ PDF exportieren")
    
    col1, col2 = st.columns([2, 1])
    
    with col1:
        family_name = st.text_input(
            "Dateiname:",
            value="Expose_Familie",
            help="Name für die PDF-Datei"
        )
    
    with col2:
        st.write("")
        st.write("")
        if st.button("📥 PDF erstellen & herunterladen", type="primary"):
            try:
                pdf_buffer = create_pdf(edited_content)
                
                st.download_button(
                    label="⬇️ PDF herunterladen",
                    data=pdf_buffer,
                    file_name=f"{family_name}.pdf",
                    mime="application/pdf",
                    use_container_width=True
                )
            except Exception as e:
                st.error(f"Fehler beim PDF-Export: {str(e)}")

else:
    st.info("👆 Laden Sie zunächst Unterlagen hoch und starten Sie die KI-Analyse.")

# --- Footer ---
st.divider()
st.caption("🔒 Hinweis: Die hochgeladenen Daten werden nur temporär verarbeitet und nicht gespeichert.")
