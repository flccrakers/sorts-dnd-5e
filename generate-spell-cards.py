# -*- coding: utf-8 -*-
"""
Generate D&D 5e spell cards (front/back) as a Scribus .sla document.

Usage (inside Scribus):
  Script -> Execute Script... -> select this file.

Requirements:
  - Scribus 1.5.x (for scripter).
  - A JSON file with spells (array of dicts) on disk.
  - A front image (same for all cards).

Layout:
  - Document units: millimeters
  - Card size: 63 x 88 mm (customizable)
  - Two pages per card: Page 1 = Recto (image), Page 2 = Verso (details)
"""

import json
import os
import textwrap

try:
    import scribus
except Exception:
    raise SystemExit("This script must be run from inside Scribus (Script → Execute Script…).")

# ----------------------------
# USER SETTINGS
# ----------------------------
JSON_INPUT_PATH = "/home/fhoonakker/Dropbox/dvt/projet-PYTHON/sorts-dnd-5e/5etools_spells_dump/spells_5etools_full.json"  # <-- change me
FRONT_IMAGE_PATH = "/home/fhoonakker/Dropbox/dvt/projet-PYTHON/sorts-dnd-5e/test.png"  # <-- change me (same image for all)
OUTPUT_SLA_PATH = "/home/fhoonakker/Dropbox/dvt/projet-PYTHON/spell_cards.sla"  # <-- change me

CARD_WIDTH_MM = 63.0
CARD_HEIGHT_MM = 88.0
MARGIN_MM = 3.0  # inner margin for back content
TITLE_BAND_HEIGHT = 10.0  # height of title band on back
BODY_FONT = "Noto Sans"  # pick a font you have; fallback applied if missing
TITLE_FONT = "Noto Sans"
ITALIC_FONT = "Noto Sans Italic"
SMALL_CAPS = False

# Limit how many spells to render (None = all)
LIMIT_COUNT = None  # e.g., 60

# Optional PDF export afterwards (set to True and adjust path)
EXPORT_PDF = False
PDF_PATH = "/home/fhoonakker/Dropbox/dvt/projet-PYTHON/spell_cards.pdf"


# ----------------------------
# HELPER FUNCTIONS
# ----------------------------

def _font_or_fallback(name: str) -> str:
    """Return a font that exists in the doc; fallback to default if not installed."""
    try:
        available = set(scribus.getFontNames())
    except Exception:
        available = set()
    return name if name in available else scribus.getDefaultFont()


def clean_text(s):
    if s is None:
        return ""
    # Scribus handles plain text best; keep it simple.
    return str(s).replace("\r\n", "\n").strip()


def list_to_str(x):
    if not x:
        return ""
    if isinstance(x, list):
        return ", ".join([str(i) for i in x])
    return str(x)


def first_line(s, max_len=120):
    s = clean_text(s)
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


def derive_meta_lines(sp):
    """
    Build readable meta lines in FR.
    Expects keys: level, school, range, time, components, duration, classes.
    Robust to missing keys.
    """
    lvl = sp.get("level")
    if isinstance(lvl, int):
        niveau = "Niveau {}".format(lvl) if lvl > 0 else "Sort mineur"
    else:
        niveau = "Niveau {}".format(lvl) if lvl not in (None, "", "0") else "Sort mineur"

    ecole = sp.get("school_fr") or sp.get("school") or ""
    portee = sp.get("range_fr") or sp.get("range") or ""
    temps = sp.get("time_fr") or sp.get("time") or ""
    comp = sp.get("components_fr") or sp.get("components") or ""
    duree = sp.get("duration_fr") or sp.get("duration") or ""
    classes = sp.get("classes_fr") or sp.get("classes") or ""

    lines = []
    if niveau:  lines.append("• {}".format(niveau))
    if ecole:   lines.append("• École : {}".format(ecole))
    if temps:   lines.append("• Incantation : {}".format(temps))
    if portee:  lines.append("• Portée : {}".format(portee))
    if comp:    lines.append("• Composantes : {}".format(comp))
    if duree:   lines.append("• Durée : {}".format(duree))
    if classes: lines.append("• Classes : {}".format(list_to_str(classes)))
    if sp.get("source"):
        lines.append("• Source : {}".format(sp["source"]))
    return "\n".join(lines)


def get_titles(sp):
    # fallbacks: use EN if FR missing
    title_fr = sp.get("name_fr") or sp.get("nameFR") or sp.get("name") or sp.get("name_en") or "Sans titre"
    title_en = sp.get("name_en") or sp.get("name") or ""
    return clean_text(title_fr), clean_text(title_en)


def get_desc_fr(sp):
    return clean_text(sp.get("desc_fr") or sp.get("description_fr") or sp.get("entries_fr") or sp.get("desc"))


def load_spells(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("Le JSON doit être un tableau de sorts.")

    # Optional filtering / sorting
    # Example: sort by level then name_fr
    def keyf(x):
        lvl = x.get("level")
        try:
            lvl = int(lvl)
        except Exception:
            lvl = 0
        return (lvl, (x.get("name_fr") or x.get("name_en") or x.get("name") or ""))

    data.sort(key=keyf)
    return data


def ensure_doc(width_mm, height_mm):
    if scribus.haveDoc():
        scribus.closeDoc()
    # units=scribus.UNIT_MILLIMETERS, firstPageNumber=1, pages=1, margins L/T/R/B
    scribus.newDocument((width_mm, height_mm), (MARGIN_MM, MARGIN_MM, MARGIN_MM, MARGIN_MM),
                        scribus.PORTRAIT, 1, scribus.UNIT_MILLIMETERS,
                        scribus.FACINGPAGES, scribus.FIRSTPAGERIGHT, 1)
    # set fonts (fallbacks)
    global BODY_FONT, TITLE_FONT, ITALIC_FONT
    BODY_FONT = _font_or_fallback(BODY_FONT)
    TITLE_FONT = _font_or_fallback(TITLE_FONT)
    ITALIC_FONT = _font_or_fallback(ITALIC_FONT)


def add_recto(image_path):
    """Create recto page with a full-bleed image."""
    # Make sure page exists
    # Fill entire page
    w, h = CARD_WIDTH_MM, CARD_HEIGHT_MM
    img = scribus.createImage(0, 0, w, h)
    try:
        scribus.loadImage(image_path, img)
        scribus.setScaleImageToFrame(True, True, img)
    except Exception as e:
        # Fallback: colored rectangle if image not found
        rect = scribus.createRect(0, 0, w, h)
        scribus.setFillColor("Black", rect)
        scribus.setLineColor("None", rect)
    # No text, just image


def add_verso(spell):
    """Create verso page with FR details and dual-language title band."""
    w, h = CARD_WIDTH_MM, CARD_HEIGHT_MM

    # Title band (top)
    band = scribus.createRect(0, 0, w, TITLE_BAND_HEIGHT)
    scribus.setFillColor("Black", band)
    scribus.setLineColor("None", band)

    # Title text (FR — big, bold)
    title_fr, title_en = get_titles(spell)
    title_frame = scribus.createText(MARGIN_MM, 1.0, w - 2 * MARGIN_MM, TITLE_BAND_HEIGHT - 2.0)
    scribus.setTextColor("White", title_frame)
    scribus.setTextAlignment(scribus.ALIGN_CENTERED, title_frame)
    scribus.setFont(TITLE_FONT, title_frame)
    scribus.setFontSize(12, title_frame)
    if SMALL_CAPS:
        scribus.setSmallCaps(True, title_frame)
    scribus.insertText(title_fr, -1, title_frame)

    # Subtitle (EN)
    sub_frame = scribus.createText(MARGIN_MM, TITLE_BAND_HEIGHT - 3.8, w - 2 * MARGIN_MM, 3.2)
    scribus.setTextColor("White", sub_frame)
    scribus.setTextAlignment(scribus.ALIGN_CENTERED, sub_frame)
    scribus.setFont(ITALIC_FONT, sub_frame)
    scribus.setFontSize(8.5, sub_frame)
    scribus.insertText(title_en, -1, sub_frame)

    # Meta block
    meta = derive_meta_lines(spell)
    meta_frame = scribus.createText(MARGIN_MM, TITLE_BAND_HEIGHT + 1.5, w - 2 * MARGIN_MM, 22.0)
    scribus.setText(meta, meta_frame)
    scribus.setFont(BODY_FONT, meta_frame)
    scribus.setFontSize(8.5, meta_frame)
    scribus.setLineSpacing(10.0, meta_frame)

    # Body (FR description)
    body_top = TITLE_BAND_HEIGHT + 1.5 + 22.0 + 1.5
    body_h = h - body_top - MARGIN_MM
    body = get_desc_fr(spell)
    body_frame = scribus.createText(MARGIN_MM, body_top, w - 2 * MARGIN_MM, body_h)
    scribus.setText(body, body_frame)
    scribus.setFont(BODY_FONT, body_frame)
    scribus.setFontSize(8.8, body_frame)
    scribus.setLineSpacing(10.6, body_frame)
    scribus.setTextAlignment(scribus.ALIGN_LEFT, body_frame)


def main():
    # Validate inputs
    if not os.path.isfile(JSON_INPUT_PATH):
        scribus.messageBox("Erreur", "Fichier JSON introuvable:\n{}".format(JSON_INPUT_PATH), scribus.ICON_WARNING,
                           scribus.BUTTON_OK)
        return
    if not os.path.isfile(FRONT_IMAGE_PATH):
        scribus.messageBox("Avertissement",
                           "Image de recto introuvable (un rectangle noir sera utilisé):\n{}".format(FRONT_IMAGE_PATH),
                           scribus.ICON_WARNING, scribus.BUTTON_OK)

    spells = load_spells(JSON_INPUT_PATH)
    if LIMIT_COUNT:
        spells = spells[:LIMIT_COUNT]

    ensure_doc(CARD_WIDTH_MM, CARD_HEIGHT_MM)

    # Page 1 already exists in newDocument; we will reuse it for the first recto.
    # For each spell, make: recto page, verso page
    total = len(spells)
    for idx, sp in enumerate(spells, 1):
        # --- Recto
        if idx == 1:
            # first page exists
            scribus.gotoPage(1)
            scribus.deletePageItem = None  # no-op; keep empty
        else:
            scribus.newPage(-1)
        add_recto(FRONT_IMAGE_PATH)

        # --- Verso
        scribus.newPage(-1)
        add_verso(sp)

        # Progress feedback
        if idx % 20 == 0 or idx == total:
            scribus.statusMessage(f"Cartes générées: {idx}/{total}")
            scribus.progressSet(int((idx / float(total)) * 100))

    # Save SLA
    try:
        scribus.saveDocAs(OUTPUT_SLA_PATH)
    except Exception as e:
        scribus.messageBox("Erreur", "Impossible d'enregistrer le .sla:\n{}\n{}".format(OUTPUT_SLA_PATH, e),
                           scribus.ICON_WARNING, scribus.BUTTON_OK)

    # Optional PDF export
    if EXPORT_PDF:
        try:
            # minimal PDF options
            pdf = scribus.PDFfile()
            pdf.file = PDF_PATH
            pdf.outdst = 0
            pdf.bleedr = pdf.bleedl = pdf.bleedt = pdf.bleedb = 0.0
            pdf.compress = True
            pdf.quality = 0
            pdf.version = 16
            pdf.save()
        except Exception as e:
            scribus.messageBox("Erreur PDF", "Export PDF échoué:\n{}".format(e), scribus.ICON_WARNING,
                               scribus.BUTTON_OK)

    scribus.messageBox("Terminé", "Génération terminée.\n{} cartes ({} pages)".format(total, total * 2),
                       scribus.ICON_NONE, scribus.BUTTON_OK)


if __name__ == "__main__":
    if not scribus.haveDoc():
        scribus.progressReset()
    try:
        main()
    except Exception as e:
        scribus.messageBox("Erreur fatale", str(e), scribus.ICON_CRITICAL, scribus.BUTTON_OK)
