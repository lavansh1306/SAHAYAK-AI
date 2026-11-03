"""PowerPoint decks from whatever is loaded: a review deck or a deck on any topic you ask for ("make 5 slides on the
gas loss problem in <area>"). Native, editable charts; speaker notes with numbers and sources; your corporate
template when uploaded; and edits by request (change chart type, add/remove/rename slides) that touch only those slides."""
from __future__ import annotations

import copy
import re
import zipfile
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.util import Emu, Inches, Pt

from ..config import OUTPUT_DIR, app_settings
from ..query import engine as E
from ..query.answer import result
from ..query.periods import add_months, describe, month_long, today
from ..query.understand import normalise, rules_plan
from ..util import fmt_value, pct

NAVY, ACCENT, RED, GREY = RGBColor(0x1F, 0x4E, 0x79), RGBColor(0xF1, 0x8F, 0x01), RGBColor(0xC7, 0x3E, 0x1D), RGBColor(0x55, 0x55, 0x55)
CT = {"line": XL_CHART_TYPE.LINE_MARKERS, "bar": XL_CHART_TYPE.COLUMN_CLUSTERED, "column": XL_CHART_TYPE.COLUMN_CLUSTERED,
      "barh": XL_CHART_TYPE.BAR_CLUSTERED, "pie": XL_CHART_TYPE.PIE, "doughnut": XL_CHART_TYPE.DOUGHNUT}


def S(kind, title, bullets=None, chart=None, notes="", table=None, subtitle=""):
    return {"kind": kind, "title": title, "bullets": bullets or [], "chart": chart, "notes": notes, "table": table, "subtitle": subtitle}


def C(ctype, categories, series: dict, title="", number_format="#,##0", y_title=""):
    return {"type": ctype, "categories": [str(c) for c in categories],
            "series": [{"name": k, "values": [None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v) for v in vs]} for k, vs in series.items()],
            "title": title, "number_format": number_format, "y_title": y_title}


def _potx_to_pptx(path: Path) -> Path:
    if path.suffix.lower() != ".potx":
        return path
    out = OUTPUT_DIR / "_charts" / (path.stem + "_as_pptx.pptx")
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "[Content_Types].xml":
                data = data.replace(b"application/vnd.openxmlformats-officedocument.presentationml.template.main+xml",
                                    b"application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml")
            zout.writestr(item, data)
    return out


def _layout(prs, *names, fallback=0):
    for n in names:
        for l in prs.slide_layouts:
            if l.name.lower() == n.lower():
                return l
    return prs.slide_layouts[min(fallback, len(prs.slide_layouts) - 1)]


def _clear_unused_placeholders(slide):
    for ph in list(slide.placeholders):
        if ph.placeholder_format.idx != 0 and ph.has_text_frame and not ph.text_frame.text.strip():
            ph._element.getparent().remove(ph._element)


def render(spec: dict, template: str | None = None, name: str | None = None) -> Path:
    use_tpl = bool(template)
    if use_tpl:
        prs = Presentation(str(_potx_to_pptx(Path(template))))
        sl = prs.slides._sldIdLst
        for sid in list(sl):
            prs.part.drop_rel(sid.rId)
            sl.remove(sid)
    else:
        prs = Presentation()
        prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    W, H = prs.slide_width, prs.slide_height
    L_TITLE = _layout(prs, "Title Slide", fallback=0)
    L_ONLY = _layout(prs, "Title Only", fallback=5)
    for i, sd in enumerate(spec["slides"], 1):
        if sd["kind"] == "title":
            s = prs.slides.add_slide(L_TITLE)
            s.shapes.title.text = sd["title"]
            sub = next((p for p in s.placeholders if p.placeholder_format.idx == 1), None)
            if sub is not None:
                sub.text = sd.get("subtitle", "")
            if not use_tpl:
                for r in s.shapes.title.text_frame.paragraphs[0].runs:
                    r.font.color.rgb, r.font.bold, r.font.size = NAVY, True, Pt(40)
                if sub is not None:
                    for r in sub.text_frame.paragraphs[0].runs:
                        r.font.color.rgb, r.font.size = GREY, Pt(18)
            _clear_unused_placeholders(s)
        else:
            s = prs.slides.add_slide(L_ONLY)
            s.shapes.title.text = sd["title"]
            if not use_tpl:
                tf = s.shapes.title
                tf.left, tf.top, tf.width, tf.height = Inches(0.5), Inches(0.3), W - Inches(1.0), Inches(0.9)
                for r in tf.text_frame.paragraphs[0].runs:
                    r.font.color.rgb, r.font.bold, r.font.size = NAVY, True, Pt(28)
                bar = s.shapes.add_shape(1, Inches(0.5), Inches(1.2), Inches(1.2), Emu(45720))
                bar.fill.solid()
                bar.fill.fore_color.rgb = ACCENT
                bar.line.fill.background()
            _clear_unused_placeholders(s)
            top = Inches(1.45)
            body_h = H - top - Inches(0.7)
            ch, bl, tb = sd.get("chart"), sd.get("bullets") or [], sd.get("table")
            if ch:
                cw = int(W * 0.60) if bl else W - Inches(1.0)
                cd = CategoryChartData()
                cd.categories = ch["categories"]
                for se in ch["series"]:
                    cd.add_series(se["name"], se["values"], number_format=ch.get("number_format", "#,##0"))
                ctype = CT.get(ch["type"], XL_CHART_TYPE.COLUMN_CLUSTERED)
                if ch["type"] == "line" and len(ch["categories"]) > 40:
                    ctype = XL_CHART_TYPE.LINE
                gf = s.shapes.add_chart(ctype, Inches(0.5), top, cw, body_h, cd)
                chart = gf.chart
                chart.has_title = True
                chart.chart_title.text_frame.text = ch.get("title", "")
                chart.chart_title.text_frame.paragraphs[0].runs[0].font.size = Pt(13)
                chart.font.size = Pt(10)
                if ch["type"] in ("pie", "doughnut"):
                    chart.has_legend = True
                    chart.legend.position = XL_LEGEND_POSITION.RIGHT
                    chart.legend.include_in_layout = False
                    pl = chart.plots[0]
                    pl.has_data_labels = True
                    pl.data_labels.number_format = "0.0%"
                    pl.data_labels.number_format_is_linked = False
                    pl.data_labels.show_percentage, pl.data_labels.show_value = True, False
                else:
                    chart.has_legend = len(ch["series"]) > 1
                    if chart.has_legend:
                        chart.legend.position = XL_LEGEND_POSITION.BOTTOM
                        chart.legend.include_in_layout = False
                    if len(ch["categories"]) <= 12 and ch["type"] in ("bar", "column", "barh"):
                        pl = chart.plots[0]
                        pl.has_data_labels = True
                        pl.data_labels.number_format = ch.get("number_format", "#,##0")
                        pl.data_labels.number_format_is_linked = False
                        pl.data_labels.font.size = Pt(9)
                    if not use_tpl and ch["type"] in ("bar", "column", "barh") and len(ch["series"]) == 1:
                        ser = chart.plots[0].series[0]
                        ser.format.fill.solid()
                        ser.format.fill.fore_color.rgb = NAVY
                left_b = Inches(0.5) + cw + Inches(0.3)
            else:
                left_b = Inches(0.5)
            if tb:
                rows, cols = len(tb["rows"]) + 1, len(tb["columns"])
                gt = s.shapes.add_table(rows, cols, Inches(0.5), top, W - Inches(1.0), min(body_h, Inches(0.36) * rows)).table
                for j, c in enumerate(tb["columns"]):
                    gt.cell(0, j).text = str(c)
                for r_i, r in enumerate(tb["rows"], 1):
                    for j, v in enumerate(r):
                        gt.cell(r_i, j).text = "" if v is None else str(v)
                for r_i in range(rows):
                    for j in range(cols):
                        for p in gt.cell(r_i, j).text_frame.paragraphs:
                            for run in p.runs:
                                run.font.size = Pt(11 if rows <= 8 else 9)
            if bl:
                bw = (W - left_b - Inches(0.5)) if ch else W - Inches(1.0)
                box = s.shapes.add_textbox(left_b, top + Inches(0.1), bw, body_h)
                tf = box.text_frame
                tf.word_wrap = True
                for k, b in enumerate(bl):
                    p = tf.paragraphs[0] if k == 0 else tf.add_paragraph()
                    p.text = "• " + b
                    p.space_after = Pt(10)
                    for r in p.runs:
                        r.font.size = Pt(16 if not ch else 14)
                        if not use_tpl:
                            r.font.color.rgb = RGBColor(0x22, 0x22, 0x22)
            if not use_tpl:
                ft = s.shapes.add_textbox(Inches(0.5), H - Inches(0.45), W - Inches(1.0), Inches(0.3)).text_frame
                ft.text = f"{app_settings().get('company_short', '')} | SAHAYAK-AI | figures computed from uploaded source data".strip(" |")
                ft.paragraphs[0].runs[0].font.size = Pt(8)
                ft.paragraphs[0].runs[0].font.color.rgb = GREY
                num = s.shapes.add_textbox(W - Inches(1.0), H - Inches(0.45), Inches(0.6), Inches(0.3)).text_frame
                num.text = str(i)
                num.paragraphs[0].runs[0].font.size = Pt(9)
        s.notes_slide.notes_text_frame.text = sd.get("notes") or sd["title"]
    name = name or f"Deck_{spec['kind']}_{datetime.now():%Y%m%d_%H%M%S}.pptx"
    out = OUTPUT_DIR / name
    prs.save(str(out))
    return out


