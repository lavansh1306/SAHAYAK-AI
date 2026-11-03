"""A tiny document model rendered to both Word (python-docx) and PDF (reportlab) so the two always match.

Blocks: title, subtitle, h1, h2, p, bullets, table, image, kpis, note, pagebreak.
Tables and figures are numbered automatically ("Table 3: ...", "Figure 2: ...") and carry a source line."""
from __future__ import annotations

import re
from pathlib import Path

NAVY = "1F4E79"
LIGHT = "DCE6F1"


def _bold_runs(text: str):
    parts = re.split(r"(\*\*[^*]+\*\*)", text or "")
    for p in parts:
        if p.startswith("**") and p.endswith("**"):
            yield p[2:-2], True
        elif p:
            yield p, False


def number_blocks(blocks: list[dict]) -> list[dict]:
    t = f = 0
    out = []
    for b in blocks:
        b = dict(b)
        if b["t"] == "table":
            t += 1
            b["label"] = f"Table {t}: {b.get('caption', '')}"
        if b["t"] == "image":
            f += 1
            b["label"] = f"Figure {f}: {b.get('caption', '')}"
        out.append(b)
    return out


# ------------------------------------------------------------------------------------------ DOCX
def to_docx(blocks: list[dict], path: Path, header_text: str = "", footer_text: str = "") -> Path:
    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor

    blocks = number_blocks(blocks)
    d = Document()
    sec = d.sections[0]
    sec.left_margin = sec.right_margin = Cm(2)
    sec.top_margin = sec.bottom_margin = Cm(1.8)
    st = d.styles["Normal"]
    st.font.name = "Arial"
    st.font.size = Pt(10)
    for name, size in (("Heading 1", 14), ("Heading 2", 12), ("Title", 20)):
        s = d.styles[name]
        s.font.name = "Arial"
        s.font.size = Pt(size)
        s.font.color.rgb = RGBColor.from_string(NAVY)
    if header_text:
        sec.header.paragraphs[0].text = header_text
        sec.header.paragraphs[0].runs[0].font.size = Pt(8)
    if footer_text:
        sec.footer.paragraphs[0].text = footer_text
        sec.footer.paragraphs[0].runs[0].font.size = Pt(8)

    def shade(cell, hex_color):
        tcPr = cell._tc.get_or_add_tcPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), hex_color)
        tcPr.append(shd)

    def para(text, style=None, italic=False, size=None, color=None):
        p = d.add_paragraph(style=style)
        for txt, b in _bold_runs(text):
            r = p.add_run(txt)
            r.bold = b
            r.italic = italic
            if size:
                r.font.size = Pt(size)
            if color:
                r.font.color.rgb = RGBColor.from_string(color)
        return p

    for b in blocks:
        t = b["t"]
        if t == "title":
            d.add_heading(b["text"], 0)
        elif t == "subtitle":
            para(b["text"], italic=True, size=9, color="555555")
        elif t == "h1":
            d.add_heading(b["text"], 1)
        elif t == "h2":
            d.add_heading(b["text"], 2)
        elif t == "p":
            para(b["text"])
        elif t == "note":
            para(b["text"], italic=True, size=8.5, color="555555")
        elif t == "bullets":
            for it in b["items"]:
                para(it, style="List Bullet")
        elif t == "kpis":
            items = b["items"]
            tb = d.add_table(rows=2, cols=len(items))
            tb.alignment = WD_TABLE_ALIGNMENT.CENTER
            for j, (lab, val) in enumerate(items):
                c0, c1 = tb.cell(0, j), tb.cell(1, j)
                c0.text, c1.text = lab, val
                shade(c0, LIGHT)
                shade(c1, LIGHT)
                c0.paragraphs[0].runs[0].font.size = Pt(8)
                r = c1.paragraphs[0].runs[0]
                r.bold = True
                r.font.size = Pt(13)
                r.font.color.rgb = RGBColor.from_string(NAVY)
            d.add_paragraph()
        elif t == "table":
            cap = d.add_paragraph()
            rr = cap.add_run(b["label"])
            rr.bold = True
            rr.font.size = Pt(9.5)
            cols, rows = b["columns"], b["rows"]
            tb = d.add_table(rows=1 + len(rows), cols=len(cols))
            tb.style = "Table Grid"
            for j, c in enumerate(cols):
                cell = tb.cell(0, j)
                cell.text = str(c)
                shade(cell, NAVY)
                run = cell.paragraphs[0].runs[0]
                run.bold = True
                run.font.size = Pt(8.5)
                run.font.color.rgb = RGBColor(255, 255, 255)
            for i, row in enumerate(rows, 1):
                for j, v in enumerate(row):
                    cell = tb.cell(i, j)
                    cell.text = "" if v is None else str(v)
                    run = cell.paragraphs[0].runs[0] if cell.paragraphs[0].runs else cell.paragraphs[0].add_run("")
                    run.font.size = Pt(8.5)
                    if j > 0 and re.match(r"^[-+]?[\d,]+(\.\d+)?%?$", str(v or "")):
                        cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
                    if b.get("highlight") and i - 1 in b["highlight"]:
                        shade(cell, "FDE9E7")
            if b.get("source"):
                para("Source: " + b["source"], italic=True, size=7.5, color="666666")
            d.add_paragraph()
        elif t == "image":
            d.add_picture(str(b["path"]), width=Cm(b.get("width_cm", 16)))
            p = para(b["label"], italic=True, size=8.5)
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            if b.get("source"):
                para("Source: " + b["source"], italic=True, size=7.5, color="666666")
        elif t == "pagebreak":
            d.add_page_break()
    d.save(str(path))
    return path


# ------------------------------------------------------------------------------------------- PDF
def to_pdf(blocks: list[dict], path: Path, header_text: str = "", footer_text: str = "") -> Path:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    blocks = number_blocks(blocks)
    ss = getSampleStyleSheet()
    navy = colors.HexColor("#" + NAVY)
    S = {
        "title": ParagraphStyle("t", parent=ss["Title"], fontName="Helvetica-Bold", fontSize=18, textColor=navy, spaceAfter=4),
        "subtitle": ParagraphStyle("st", parent=ss["Normal"], fontName="Helvetica-Oblique", fontSize=8.5, textColor=colors.HexColor("#555555"), spaceAfter=8),
        "h1": ParagraphStyle("h1", parent=ss["Heading2"], fontName="Helvetica-Bold", fontSize=13, textColor=navy, spaceBefore=10, spaceAfter=4),
        "h2": ParagraphStyle("h2", parent=ss["Heading3"], fontName="Helvetica-Bold", fontSize=11, textColor=navy, spaceBefore=6, spaceAfter=3),
        "p": ParagraphStyle("p", parent=ss["BodyText"], fontName="Helvetica", fontSize=9.5, leading=13),
        "note": ParagraphStyle("n", parent=ss["BodyText"], fontName="Helvetica-Oblique", fontSize=7.5, textColor=colors.HexColor("#666666"), leading=10),
        "cap": ParagraphStyle("c", parent=ss["BodyText"], fontName="Helvetica-Bold", fontSize=9, spaceBefore=4, spaceAfter=2),
        "figcap": ParagraphStyle("fc", parent=ss["BodyText"], fontName="Helvetica-Oblique", fontSize=8.5, alignment=TA_CENTER),
        "cell": ParagraphStyle("cell", parent=ss["BodyText"], fontName="Helvetica", fontSize=7.8, leading=9.5),
        "hcell": ParagraphStyle("hcell", parent=ss["BodyText"], fontName="Helvetica-Bold", fontSize=7.8, leading=9.5, textColor=colors.white),
        "kl": ParagraphStyle("kl", parent=ss["BodyText"], fontName="Helvetica", fontSize=7.5, alignment=TA_CENTER, textColor=colors.HexColor("#333333")),
        "kv": ParagraphStyle("kv", parent=ss["BodyText"], fontName="Helvetica-Bold", fontSize=13, alignment=TA_CENTER, textColor=navy, leading=16),
    }

    def md(text):
        text = (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)

    W = A4[0] - 3.6 * cm
    story = []
    for b in blocks:
        t = b["t"]
        if t in ("title", "subtitle", "h1", "h2", "p", "note"):
            story.append(Paragraph(md(b["text"]), S[t]))
        elif t == "bullets":
            for it in b["items"]:
                story.append(Paragraph("&bull; " + md(it), ParagraphStyle("b", parent=S["p"], leftIndent=10, firstLineIndent=-8)))
        elif t == "kpis":
            items = b["items"]
            data = [[Paragraph(md(l), S["kl"]) for l, _ in items], [Paragraph(md(v), S["kv"]) for _, v in items]]
            tb = Table(data, colWidths=[W / len(items)] * len(items))
            tb.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#" + LIGHT)), ("BOX", (0, 0), (-1, -1), 0.5, colors.white),
                                    ("INNERGRID", (0, 0), (-1, -1), 2, colors.white), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                    ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
            story += [tb, Spacer(1, 8)]
        elif t == "table":
            cols, rows = b["columns"], b["rows"]
            data = [[Paragraph(md(str(c)), S["hcell"]) for c in cols]] + [[Paragraph(md("" if v is None else str(v)), S["cell"]) for v in r] for r in rows]
            n = len(cols)
            first = min(0.34, max(0.18, 1.6 / n))
            widths = [W * first] + [W * (1 - first) / (n - 1)] * (n - 1) if n > 1 else [W]
            tb = Table(data, colWidths=widths, repeatRows=1)
            style = [("BACKGROUND", (0, 0), (-1, 0), navy), ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
                     ("VALIGN", (0, 0), (-1, -1), "TOP"), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F6FA")])]
            for i in b.get("highlight", []):
                style.append(("BACKGROUND", (0, i + 1), (-1, i + 1), colors.HexColor("#FDE9E7")))
            tb.setStyle(TableStyle(style))
            parts = [Paragraph(md(b["label"]), S["cap"]), tb]
            if b.get("source"):
                parts.append(Paragraph("Source: " + md(b["source"]), S["note"]))
            story += [KeepTogether(parts) if len(rows) < 25 else parts[0], *([] if len(rows) < 25 else parts[1:]), Spacer(1, 8)]
        elif t == "image":
            wcm = b.get("width_cm", 16)
            img = Image(str(b["path"]), width=wcm * cm, height=wcm * cm * b.get("aspect", 0.42))
            parts = [img, Paragraph(md(b["label"]), S["figcap"])]
            if b.get("source"):
                parts.append(Paragraph("Source: " + md(b["source"]), S["note"]))
            story += [KeepTogether(parts), Spacer(1, 6)]
        elif t == "pagebreak":
            story.append(PageBreak())

    def deco(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#666666"))
        if header_text:
            canvas.drawString(1.8 * cm, A4[1] - 1.1 * cm, header_text)
        canvas.drawString(1.8 * cm, 0.9 * cm, footer_text)
        canvas.drawRightString(A4[0] - 1.8 * cm, 0.9 * cm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=1.8 * cm, rightMargin=1.8 * cm, topMargin=1.6 * cm, bottomMargin=1.6 * cm,
                            title=next((b["text"] for b in blocks if b["t"] == "title"), "Report"), author="SAHAYAK-AI")
    doc.build(story, onFirstPage=deco, onLaterPages=deco)
    return path
