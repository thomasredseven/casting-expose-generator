# app.py - Casting Exposé Generator v1.3
# Mit Chunk-Verarbeitung, Bildkomprimierung und Retry-Logik

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

# --- Prompts ---
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

CHUNK_EXTRACTION_PROMPT = """
Analysiere diese Dokument-Seite(n) und extrahiere ALLE relevanten Informationen für ein Casting-Exposé.

Extrahiere (falls vorhanden):
- Namen, Alter, Berufe der Familienmitglieder
- Adresse, Ort
- Informationen zum Garten (Größe, Zustand, Besonderheiten)
- Budget
- Wünsche und Pläne für den Garten
- Persönliche Hintergründe, Backstory
- TV-Erfahrung, Termine, Einschränkungen

Ignoriere:
- Datenschutzerklärungen
- Rechtliche Texte
- Unterschriften-Felder

Gib die Informationen strukturiert zurück. Wenn du etwas nicht lesen kannst, schreibe [unleserlich].
"""

COMBINE_PROMPT = """
Hier sind extrahierte Informationen aus verschiedenen Dokumenten einer Casting-Bewerbung.
Kombiniere diese zu EINEM kompakten Exposé.

{extracted_infos}

---

Erstelle daraus ein Exposé mit folgender Struktur:

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
- Keine Duplikate
- Das Exposé soll auf eine Seite passen
"""

# --- Hilfsfunktionen ---

def compress_image(image, max_size=1024):
    """Komprimiert Bilder um Token zu sparen."""
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
    """Zeigt einen Countdown mit Progress-Bar"""
    progress_bar = st.progress(0)
    countdown_text = st.empty()
    
    for i in range(seconds):
        remaining = seconds - i
        countdown_text.text(f"⏱️ {message}... Noch {remaining} Sekunden")
        progress_bar.progress((i + 1) / seconds)
        time.sleep(1)
    
    countdown_text.empty()
    progress_bar.empty()


def call_gemini_safe(contents, max_retries=3):
    """
    Ruft Gemini API auf mit automatischem Retry bei Rate-Limit-Fehlern.
    """
    for attempt in range(max_retries):
        try:
            response = model.generate_content(contents)
            return response.text
        
        except Exception as e:
            error_message = str(e)
            
            if "429" in error_message or "quota" in error_message.lower() or "rate" in error_message.lower():
                wait_time = 60
                
                # Versuche Wartezeit aus Fehler zu parsen
                match = re.search(r'retry_delay.*?(\d+)', error_message)
                if match:
                    wait_time = int(match.group(1)) + 10
                
                if attempt < max_retries - 1:
                    st.warning(f"⏳ API-Limit erreicht. (Versuch {attempt + 1}/{max_retries})")
                    wait_with_countdown(wait_time, "Warte auf API")
                    st.info("🔄 Versuche erneut...")
                else:
                    raise Exception(
                        f"API-Limit auch nach {max_retries} Versuchen noch erreicht. "
                        f"Bitte versuche es in einigen Minuten erneut."
                    )
            else:
                raise e
    
    return None


def process_images_in_chunks(images, chunk_size=3, delay_between_chunks=65):
    """
    Verarbeitet Bilder in kleinen Chunks mit Wartezeit dazwischen.
    Sammelt Teilergebnisse und kombiniert sie am Ende.
    """
    extracted_parts = []
    total_chunks = (len(images) + chunk_size - 1) // chunk_size
    
    for i in range(0, len(images), chunk_size):
        chunk = images[i:i + chunk_size]
        chunk_num = (i // chunk_size) + 1
        
        st.info(f"📦 Verarbeite Chunk {chunk_num}/{total_chunks} ({len(chunk)} Bilder)...")
        
        # Chunk an Gemini senden
        contents = [CHUNK_EXTRACTION_PROMPT]
        for img in chunk:
            contents.append(img)
        
        result = call_gemini_safe(contents)
        if result:
            extracted_parts.append(f"--- Teil {chunk_num} ---\n{result}")
            st.success(f"✅ Chunk {chunk_num}/{total_chunks} abgeschlossen")
        
        # Warten vor nächstem Chunk (außer beim letzten)
        if i + chunk_size < len(images):
            st.info(f"⏳ Warte {delay_between_chunks} Sekunden vor nächstem Chunk (API-Limit)...")
            wait_with_countdown(delay_between_chunks, "Pause zwischen Chunks")
    
    return extracted_parts


def combine_extracted_parts(parts, additional_text=""):
    """Kombiniert die extrahierten Teile zu einem finalen Exposé"""
    
    all_infos = "\n\n".join(parts)
    if additional_text:
        all_infos = f"--- Textuelle Dokumente ---\n{additional_text}\n\n{all_infos}"
    
    prompt = COMBINE_PROMPT.format(extracted_infos=all_infos)
    
    return call_gemini_safe([prompt])


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
        image_files = [f for f in uploaded_files if f.type.startswith('image/')]
        pdf_files = [f for f in uploaded_files if f.type == 'application/pdf']
        docx_files = [f for f in uploaded_files if f.type == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document']
        
        st.success(f"✅ {len(uploaded_files)} Datei(en) hochgeladen")
        st.caption(f"📷 {len(image_files)} Bilder | 📄 {len(pdf_files)} PDFs | 📝 {len(docx_files)} Word-Dokumente")
        
        if len(image_files) > 3:
            estimated_time = ((len(image_files) // 3) * 65) + 30
            st.info(
                f"ℹ️ Bei {len(image_files)} Bildern wird die Chunk-Verarbeitung aktiviert. "
                f"Geschätzte Dauer: ~{estimated_time // 60} Minuten {estimated_time % 60} Sekunden"
            )
        
        with st.expander("Vorschau Bilder"):
            if image_files:
                cols = st.columns(min(len(image_files), 4))
                for i, img_file in enumerate(image_files):
                    with cols[i % 4]:
                        st.image(img_file, use_container_width=True, caption=img_file.name)
            else:
                st.info("Keine Bilder hochgeladen")

with col2:
    st.subheader("📝 Text (optional)")
    manual_text = st.text_area(
        "E-Mail-Text, Notizen etc.",
        height=200,
        placeholder="Hier können Sie zusätzlichen Text einfügen..."
    )

st.divider()

# --- Schritt 2: Verarbeitung ---
st.header("2️⃣ Informationen extrahieren")

with st.expander("⚙️ Erweiterte Optionen"):
    col1, col2, col3 = st.columns(3)
    with col1:
        max_image_size = st.slider(
            "Max. Bildgröße (px)",
            min_value=512,
            max_value=2048,
            value=1024,
            step=256,
            help="Kleinere Bilder = weniger Tokens"
        )
    with col2:
        chunk_size = st.slider(
            "Bilder pro Chunk",
            min_value=1,
            max_value=5,
            value=3,
            help="Weniger = sicherer, mehr = schneller"
        )
    with col3:
        chunk_delay = st.slider(
            "Pause zwischen Chunks (Sek.)",
            min_value=30,
            max_value=120,
            value=65,
            help="Mindestens 60 Sekunden empfohlen"
        )

if st.button("🔍 KI-Analyse starten", type="primary", use_container_width=True):
    if not uploaded_files and not manual_text:
        st.error("Bitte laden Sie mindestens eine Datei hoch oder geben Sie Text ein.")
    else:
        try:
            status = st.empty()
            
            # --- Text aus PDFs und Word extrahieren ---
            extracted_text = manual_text or ""
            
            for f in uploaded_files:
                f.seek(0)
                if f.type == 'application/pdf':
                    status.text(f"📄 Extrahiere Text aus {f.name}...")
                    extracted_text += "\n\n--- PDF: " + f.name + " ---\n" + extract_text_from_pdf(f)
                elif f.type == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document':
                    status.text(f"📝 Extrahiere Text aus {f.name}...")
                    extracted_text += "\n\n--- Word: " + f.name + " ---\n" + extract_text_from_docx(f)
            
            # --- Bilder vorbereiten und komprimieren ---
            pil_images = []
            image_files = [f for f in uploaded_files if f.type.startswith('image/')]
            
            for i, f in enumerate(image_files):
                f.seek(0)
                status.text(f"🖼️ Komprimiere Bild {i+1}/{len(image_files)}: {f.name}...")
                img = Image.open(f)
                img = compress_image(img, max_size=max_image_size)
                pil_images.append(img)
            
            status.empty()
            
            # --- Verarbeitung je nach Anzahl ---
            if len(pil_images) <= 3:
                # Wenige Bilder: Alles auf einmal
                st.info("📤 Sende alle Dokumente an Gemini...")
                
                contents = [EXTRACTION_PROMPT]
                if extracted_text:
                    contents.append(f"TEXTUELLE INFORMATIONEN:\n{extracted_text}\n\n")
                contents.append("GESCANNTE DOKUMENTE:")
                for img in pil_images:
                    contents.append(img)
                
                result = call_gemini_safe(contents)
            
            else:
                # Viele Bilder: Chunk-Verarbeitung
                st.warning(f"📦 Starte Chunk-Verarbeitung für {len(pil_images)} Bilder...")
                
                extracted_parts = process_images_in_chunks(
                    pil_images, 
                    chunk_size=chunk_size, 
                    delay_between_chunks=chunk_delay
                )
                
                st.info("🔗 Kombiniere alle Teilergebnisse...")
                result = combine_extracted_parts(extracted_parts, extracted_text)
            
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
        help="Hier können Sie den Text anpassen."
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
        if st.button("📥 PDF erstellen", type="primary"):
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
st.caption("🔒 Die hochgeladenen Daten werden nur temporär verarbeitet und nicht gespeichert.")
