"""Reports, decks, infographic, insights, alerts, decision support and the manual-report check."""
import zipfile
from pathlib import Path

import openpyxl
import pytest
from docx import Document
from pptx import Presentation

from app.config import OUTPUT_DIR


def test_report_pdf_and_word(store):
    from app.reports.builder import build
    r = build(store, {"formats": ["pdf", "docx"]})
    files = {Path(f["name"]).suffix: OUTPUT_DIR / f["name"] for f in r["files"]}
    assert ".pdf" in files and ".docx" in files
    import pdfplumber
    with pdfplumber.open(str(files[".pdf"])) as pdf:
        text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    assert "Table 1:" in text and "Source:" in text
    d = Document(str(files[".docx"]))
    assert len(d.tables) >= 1


def test_report_edit_unit_and_section(store):
    from app.reports.builder import build, edit_report
    r = build(store, {"formats": ["pdf"]})
    r2 = edit_report(store, r["report_spec"], "redo it in crore and add a section on the monthly trend of records")
    assert r2["type"] == "answer" and "crore" in " ".join(r2["answer"])


def test_infographic(store):
    from app.reports.builder import infographic
    r = infographic(store)
    assert any(f["name"].endswith(".png") for f in r["files"])


def test_decks_and_edit(store):
    from app.decks.builder import _deliver, edit_deck, overview_deck
    spec = overview_deck(store, 8)
    out = _deliver(spec)
    p = OUTPUT_DIR / out["files"][0]["name"]
    prs = Presentation(str(p))
    assert len(prs.slides) == len(spec["slides"]) <= 8
    assert all(s.notes_slide.notes_text_frame.text.strip() for s in prs.slides)
    has_chart = any(sh.has_chart for s in prs.slides for sh in s.shapes)
    assert has_chart
    e = edit_deck(store, out["deck_spec"], "rename slide 2 to Summary")
    assert e["type"] == "answer" and "Summary" in " ".join(e["outline"])


def test_insights_and_actions(store):
    from app.decision.support import top_actions
    from app.insights.engine import run_insights
    fs = run_insights(store)
    assert all(f["title"] and f["detail"] and f["source"] for f in fs)
    assert top_actions(store)["type"] == "answer"


def test_alert_rule(store):
    from app.alerts.engine import evaluate_rule, save_rule
    t = next(t for t in store.usable() if t.measures())
    r = save_rule({"table": t.key, "measure": t.measures()[0], "op": ">", "red": -1e18, "grain": "month" if t.time else "none"}, store)
    assert evaluate_rule(store, r), "a limit below every value must trigger"


def test_manual_report_check_finds_planted_mistakes(store, tmp_path):
    """Build a summary sheet from the data itself, spoil two cells, and expect exactly those two to be flagged."""
    from app.audit.checker import audit_file
    from app.query import engine as E
    t = next(t for t in store.usable() if t.measures() and t.dims() and t.cols[t.measures()[0]]["agg"] == "sum" and 4 <= t.cols[t.dims()[0]].get("card", 0) <= 30)
    m, d = t.measures()[0], t.dims()[0]
    x = E.run(store, {"table": t.key, "measures": [{"col": m}], "group": [d], "grain": None, "filters": [], "having": [], "period": None, "compare": None,
                      "sort": "desc", "limit": None, "share": False, "chart": "auto", "mode": "aggregate"})
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.append([d, m])
    for i, (k, v) in enumerate(zip(x["res"][d], x["res"]["m0"])):
        val = round(float(v), 2)
        if i == 0:
            val = round(val * 100, 2)          # unit slip
        if i == 1:
            val = round(val + 7.0, 2)          # typing error
        ws.append([str(k), val])
    p = tmp_path / "manual_summary.xlsx"
    wb.save(p)
    r = audit_file(store, p)
    rows = r["table"]["rows"] if r.get("table") else []
    flagged = [row for row in rows if row[1] != "-"]
    assert len(flagged) == 2, rows
    assert "unit slip" in flagged[0][4] or "unit slip" in flagged[1][4]
    out = OUTPUT_DIR / r["files"][0]["name"]
    assert out.exists()


def test_api_basics():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        h = c.get("/api/health").json()
        assert h["ok"] and not h["empty"]
        assert c.get("/api/files/..%2F..%2Fstart.py").status_code == 404
        assert c.post("/api/reset").status_code == 400
        assert c.get("/api/overview").json()["cards"]
        tl = c.get("/api/tables").json()["tables"]
        assert tl
        k = tl[0]["key"]
        assert c.get(f"/api/tables/{k}/preview").status_code == 200
        assert c.get(f"/api/tables/{k}/download").status_code == 200
