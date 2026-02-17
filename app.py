# app.py - Casting Exposé Generator v1
import streamlit as st
import google.generativeai as genai
from PIL import Image
import io
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import tempfile
import os

# --- Konfiguration ---
st.set_page_config(
    page_title="Casting Exposé Generator",
    page_icon="🎬",
    layout="wide"
)

# Gemini API Setup
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "AIzaSyACy3fxuMQB4ZYg5RMjXz-n6odiOKoETAs")
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

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
def extract_info_from_images(images):
    """Extrahiert Informationen aus Bildern via Gemini"""
    contents = [EXTRACTION_PROMPT]
    
    for img in images:
        contents.append(img)
    
    response = model.generate_content(contents)
    return response.text

def extract_info_from_text(text):
    """Extrahiert Informationen aus Text via Gemini"""
    prompt = EXTRACTION_PROMPT + "\n\nHier sind die Unterlagen:\n\n" + text
    response = model.generate_content(prompt)
    return response.text

def extract_info_combined(images, text):
    """Kombiniert Bilder und Text für Extraktion"""
    contents = [EXTRACTION_PROMPT + "\n\nHier sind die Unterlagen:\n"]
    
    if text:
        contents.append(f"TEXTUELLE INFORMATIONEN:\n{text}\n\n")
    
    if images:
        contents.append("GESCANNTE DOKUMENTE/BILDER:")
        for img in images:
            contents.append(img)
    
    response = model.generate_content(contents)
    return response.text

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
    
    # Custom Styles
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
    
    # Content parsen und formatieren
    story = []
    
    lines = content.split('\n')
    for line in lines:
        line = line.strip()
        if not line:
            story.append(Spacer(1, 6))
        elif line.startswith('## '):
            # Haupttitel
            story.append(Paragraph(line[3:], title_style))
        elif line.startswith('**') and line.endswith('**'):
            # Fette Überschrift
            story.append(Paragraph(line[2:-2], heading_style))
        elif line.startswith('**') and ':**' in line:
            # Label: Wert
            story.append(Paragraph(line.replace('**', '<b>', 1).replace('**', '</b>', 1), body_style))
        elif line.startswith('- '):
            # Aufzählung
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
    st.subheader("📄 Scans & Bilder")
    uploaded_images = st.file_uploader(
        "Casting-Bögen, Protokolle etc.",
        type=["png", "jpg", "jpeg", "webp"],
        accept_multiple_files=True,
        help="Fotos oder Scans von handschriftlichen Unterlagen"
    )
    
    if uploaded_images:
        st.success(f"{len(uploaded_images)} Datei(en) hochgeladen")
        with st.expander("Vorschau"):
            cols = st.columns(min(len(uploaded_images), 3))
            for i, img_file in enumerate(uploaded_images):
                with cols[i % 3]:
                    st.image(img_file, use_container_width=True)

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

if st.button("🔍 KI-Analyse starten", type="primary", use_container_width=True):
    if not uploaded_images and not manual_text:
        st.error("Bitte laden Sie mindestens ein Bild hoch oder geben Sie Text ein.")
    else:
        with st.spinner("Analysiere Unterlagen mit Gemini..."):
            try:
                # Bilder vorbereiten
                pil_images = []
                if uploaded_images:
                    for img_file in uploaded_images:
                        img = Image.open(img_file)
                        pil_images.append(img)
                
                # Extraktion
                if pil_images and manual_text:
                    result = extract_info_combined(pil_images, manual_text)
                elif pil_images:
                    result = extract_info_from_images(pil_images)
                else:
                    result = extract_info_from_text(manual_text)
                
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
    
    # Update session state
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
        st.write("")  # Spacing
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
