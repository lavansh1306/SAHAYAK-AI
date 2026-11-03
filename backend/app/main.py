"""SAHAYAK-AI backend (FastAPI). Starts with NO data: everything comes through the ingestion pipeline."""
from __future__ import annotations

import asyncio
import io
import json
import re
import shutil
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import LANDING_DIR, OUTPUT_DIR, PROJECT_DIR, STATE_DIR, app_settings, save_settings
from .store import STORE, examples, init_store
from .util import clean


async def _rules_loop():
    from .alerts.engine import run_rules
    while True:
        await asyncio.sleep(15 * 60)
        try:
            if not STORE.is_empty():
                run_rules(STORE)
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_store()
    task = asyncio.create_task(_rules_loop())
    yield
    task.cancel()


app = FastAPI(title="SAHAYAK-AI", version="3.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_methods=["*"], allow_headers=["*"])


def _safe(name: str) -> str:
    n = Path(name or "upload").name
    return re.sub(r"[^A-Za-z0-9._() -]+", "_", n)[:180] or "upload"


class AskIn(BaseModel):
    question: str
    session_id: str = "default"


class FeedbackIn(BaseModel):
    id: str
    rating: int
    comment: str = ""
    question: str = ""


class ReportIn(BaseModel):
    kind: str = "report"           # report | infographic
    tables: list[str] = []
    period_end: str | None = None
    unit: str | None = None
    filters: dict[str, list[str]] = {}
    sections: list[str] = []
    include: list[str] = []
    formats: list[str] = ["pdf", "docx"]
    title: str | None = None
    session_id: str = "default"


class DeckIn(BaseModel):
    topic: str = ""
    slides: int | None = None
    use_template: bool = False
    session_id: str = "default"


class RuleIn(BaseModel):
    table: str
    measure: str
    agg: str | None = None
    group: str = ""
    grain: str = "month"
    op: str = ">"
    amber: float | None = None
    red: float | None = None
    window: str = "latest"
    owner: str = ""
    name: str = ""


class Decision(BaseModel):
    origin: str
    action: str = "load"            # load | skip
    target: str | None = None       # 'new' or an existing table key
    name: str | None = None
    types: dict[str, str] | None = None
    roles: dict[str, str] | None = None
    date_order: dict[str, str] | None = None
    time: str | None = None
    mapping: dict[str, str] | None = None


class ResolveIn(BaseModel):
    decisions: list[Decision]


class ColumnEdit(BaseModel):
    label: str | None = None
    role: str | None = None
    agg: str | None = None
    unit: str | None = None
    synonyms: list[str] | None = None
    hidden: bool | None = None
    higher_is_better: bool | None = None


class TableEdit(BaseModel):
    label: str | None = None
    description: str | None = None
    time: str | None = None
    columns: dict[str, ColumnEdit] = {}


class MetricIn(BaseModel):
    name: str
    label: str
    expr: str
    unit: str = ""
    unit_kind: str = ""


# ------------------------------------------------------------------------------------- status
@app.get("/api/health")
def health():
    from . import llm
    cs = app_settings()
    return {"ok": True, "empty": STORE.is_empty(), "tables": len(STORE.tables), "documents": len(STORE.documents), "llm": llm.status(),
            "company": cs.get("company", ""), "time": datetime.now().isoformat(timespec="seconds")}


@app.get("/api/settings")
def get_settings():
    return app_settings()


@app.post("/api/settings")
def set_settings(body: dict):
    allowed = {"company", "company_short", "fiscal_year_start_month", "number_style", "default_money_unit", "pipeline"}
    s = save_settings({k: v for k, v in body.items() if k in allowed})
    STORE.reload()
    return s


@app.post("/api/llm/test")
def llm_test():
    from . import llm
    t = llm.chat([{"role": "user", "content": "Reply with the single word: ready"}], max_tokens=10, purpose="ping")
    return {"ok": t is not None, "reply": t, "status": llm.status()}


@app.get("/api/examples")
def get_examples():
    return {"groups": [] if STORE.is_empty() else examples(STORE), "empty": STORE.is_empty()}


@app.get("/api/catalog")
def catalog():
    c = STORE.catalog
    return clean({"empty": c.get("empty", True), "tables": c.get("tables", []), "documents": c.get("documents", []), "templates": c.get("templates", []),
                  "links": STORE.links, "latest": c.get("latest", {})})


# ------------------------------------------------------------------------------------- overview dashboard
@app.get("/api/overview")
def overview():
    from .pipeline.runner import list_runs
    from .query import engine as E
    from .query.periods import add_months, month_long
    from .util import fmt_value, pct
    import numpy as np
    import pandas as pd
    out = {"empty": STORE.is_empty(), "runs": [{k: r.get(k) for k in ("id", "file", "status", "created", "message")} for r in list_runs()[:6]], "cards": [], "alerts": {"red": 0, "amber": 0, "top": []},
           "tables": [{"key": t.key, "label": t.label, "rows": len(t.df), "status": t.status(), "range": t.entry.get("time_range")} for t in STORE.tables.values()],
           "documents": [d.name for d in STORE.documents], "pins": _pins()}
    if STORE.is_empty():
        return out
    for t in sorted([t for t in STORE.usable() if t.measures()], key=lambda t: -t.importance())[:6]:
        card = {"table": t.key, "label": t.label, "tiles": [], "trend": None, "breakdown": None}
        tr = t.entry.get("time_range")
        M = None
        if tr and t.time_type in ("month", "date", "datetime"):
            M = str(tr[1])[:7]
            if t.time_type != "month":
                d = pd.Timestamp(str(tr[1])[:10])
                if d.day < (d + pd.offsets.MonthEnd(0)).day - 2:
                    M = add_months(M, -1)
        base = {"table": t.key, "group": [], "grain": None, "filters": [], "having": [], "compare": None, "sort": None, "limit": None, "share": False, "chart": "auto", "mode": "aggregate"}
        for c in t.measures()[:3]:
            try:
                per = {"start": M, "end": M, "label": month_long(M), "kind": "month"} if M else None
                x = E.run(STORE, {**base, "measures": [{"col": c}], "period": per, "compare": "previous_year" if per else None})
                mi = x["measures"][0]
                v = x["res"]["m0"].iloc[0]
                g = x["res"]["g0"].iloc[0] if "g0" in x["res"].columns else np.nan
                bad = False
                from .query.understand import bad_direction
                bad = bad_direction(t, c)
                card["tiles"].append({"label": mi["text"], "value": fmt_value(v, mi.get("unit", ""), mi.get("unit_kind", "")), "period": x["period"]["label"] if x.get("period") else "all data",
                                      "change": pct(g) if pd.notna(g) else None, "tone": "" if pd.isna(g) else ("bad" if (g > 0) == bad and abs(g) > 0.5 else "good"),
                                      "ask": f"{mi['text']} by month"})
            except Exception:
                continue
        m0 = t.measures()[0]
        if t.time:
            try:
                per = {"start": add_months(M, -11), "end": M, "label": "", "kind": "range"} if M else None
                g = "month" if M else ("day" if t.entry.get("grain") in ("day", "hour") else None)
                if g:
                    x = E.run(STORE, {**base, "measures": [{"col": m0}], "grain": g, "period": per})
                    card["trend"] = {"type": "line", "title": f"{x['measures'][0]['text']} per {g}", "x": [str(v) for v in x["res"]["__bucket"]],
                                     "series": [{"name": x["measures"][0]["text"], "data": [round(float(v), 3) for v in x["res"]["m0"]]}]}
            except Exception:
                pass
        d = next((c for c in t.dims() if t.cols[c].get("type") != "fy_label" and c != "unit_reported" and t.cols[c].get("card", 0) <= 40), None)
        if d:
            try:
                per = {"start": M, "end": M, "label": month_long(M), "kind": "month"} if M else None
                x = E.run(STORE, {**base, "measures": [{"col": m0}], "group": [d], "period": per, "sort": "desc", "limit": 10})
                card["breakdown"] = {"type": "bar", "title": f"{x['measures'][0]['text']} by {t.cols[d]['label'].lower()}" + (f", {x['period']['label']}" if x.get("period") else ""),
                                     "x": [str(v) for v in x["res"][d]], "series": [{"name": x["measures"][0]["text"], "data": [round(float(v), 3) for v in x["res"]["m0"]]}]}
            except Exception:
                pass
        out["cards"].append(card)
    try:
        from .alerts.engine import evaluate
        A = evaluate(STORE)
        out["alerts"] = {"red": sum(a["severity"] == "Red" for a in A), "amber": sum(a["severity"] == "Amber" for a in A),
                         "top": [{k: a[k] for k in ("severity", "alert", "entity", "period", "value")} for a in A[:6]]}
    except Exception:
        pass
    return clean(out)


PINS = STATE_DIR / "pins.json"


def _pins() -> list:
    try:
        return json.loads(PINS.read_text(encoding="utf-8"))
    except Exception:
        return []


@app.post("/api/pins")
def add_pin(body: dict):
    p = _pins()
    q = str(body.get("question", "")).strip()
    if q and q not in [x["question"] for x in p]:
        p.append({"question": q, "title": body.get("title", q), "ts": datetime.now().isoformat(timespec="seconds")})
        PINS.write_text(json.dumps(p[-12:]), encoding="utf-8")
    return {"pins": _pins()}


@app.delete("/api/pins")
def del_pin(question: str):
    p = [x for x in _pins() if x["question"] != question]
    PINS.write_text(json.dumps(p), encoding="utf-8")
    return {"pins": p}


# ------------------------------------------------------------------------------------- pipeline
@app.get("/api/pipeline/schema")
def pipeline_schema():
    from .pipeline.runner import STAGES
    return {"stages": [{"key": k, "name": n} for k, n in STAGES],
            "types": ["number", "date", "datetime", "month", "year", "category", "id", "text", "boolean", "fy_label", "ignore"],
            "tables": [{"key": t.key, "label": t.label} for t in STORE.tables.values()]}


@app.post("/api/pipeline/upload")
async def pipeline_upload(files: list[UploadFile] = File(...), mode: str = Form("merge")):
    from .pipeline.runner import submit
    if mode not in ("merge", "replace"):
        raise HTTPException(400, "mode must be merge or replace")
    runs = []
    for uf in files:
        name = _safe(uf.filename)
        dest = LANDING_DIR / f"{datetime.now():%Y%m%d_%H%M%S_%f}__{name}"
        with open(dest, "wb") as f:
            shutil.copyfileobj(uf.file, f)
        r = submit(dest, name, mode=mode, background=True)
        runs.append({"id": r["id"], "file": name, "status": r["status"], "message": r["message"]})
    return {"runs": runs}


@app.get("/api/pipeline/runs")
def runs(n: int = 50):
    from .pipeline.runner import list_runs
    return clean({"runs": [{k: r.get(k) for k in ("id", "file", "status", "created", "updated", "message", "mode", "size")} |
                           {"stages": [{"key": s["key"], "name": s["name"], "status": s["status"], "ms": s["ms"]} for s in r["stages"]]} for r in list_runs()[:n]]})


@app.get("/api/pipeline/runs/{rid}")
def run_detail(rid: str):
    from .pipeline.runner import get_run
    r = get_run(rid)
    if r is None:
        raise HTTPException(404, "run not found")
    return JSONResponse(clean({k: v for k, v in r.items() if k != "path"}))


@app.post("/api/pipeline/runs/{rid}/resolve")
def run_resolve(rid: str, body: ResolveIn):
    from .pipeline.runner import resolve
    try:
        r = resolve(rid, [d.model_dump() for d in body.decisions], background=True)
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e))
    return {"id": r["id"], "status": r["status"]}


# ------------------------------------------------------------------------------------- data dictionary & warehouse
@app.get("/api/tables")
def tables():
    return clean({"tables": [{**t.summary(), "versions": t.entry.get("versions", []), "validation": t.entry.get("validation", []), "notes": t.entry.get("notes", [])}
                             for t in STORE.tables.values()],
                  "documents": [d.summary() for d in STORE.documents], "templates": list(STORE.templates), "drafts": list(STORE.drafts), "links": STORE.links,
                  "empty": STORE.is_empty()})


@app.get("/api/tables/{key}")
def table_detail(key: str):
    t = STORE.tables.get(key)
    if t is None:
        raise HTTPException(404, "table not found")
    return clean({**t.entry, "label": t.label, "summary": t.summary()})


@app.get("/api/tables/{key}/preview")
def preview(key: str, n: int = 50):
    t = STORE.tables.get(key)
    if t is None:
        raise HTTPException(404, "table not found")
    df = t.df.head(min(n, 500))
    return clean({"columns": [str(c) for c in df.columns], "rows": [[clean(v) for v in r] for r in df.astype(object).values.tolist()], "total": len(t.df)})


@app.get("/api/tables/{key}/download")
def download(key: str):
    t = STORE.tables.get(key)
    if t is None:
        raise HTTPException(404, "table not found")
    buf = io.StringIO()
    t.df.to_csv(buf, index=False)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{key}_clean.csv"'})


@app.post("/api/tables/{key}/edit")
def edit_table(key: str, body: TableEdit):
    from .pipeline.dictionary import load_overrides, save_overrides
    t = STORE.tables.get(key)
    if t is None:
        raise HTTPException(404, "table not found")
    ov = load_overrides(STORE.root)
    e = ov.setdefault(key, {})
    if body.label:
        e["label"] = body.label
    if body.description is not None:
        e["description"] = body.description
    if body.time is not None:
        if body.time and t.cols.get(body.time, {}).get("role") != "time":
            raise HTTPException(400, "That column is not a date/time column")
        e["time"] = body.time
    for c, ce in body.columns.items():
        if c not in t.cols:
            raise HTTPException(400, f"Unknown column {c}")
        patch = {k: v for k, v in ce.model_dump().items() if v is not None}
        if patch.get("role") and patch["role"] not in ("measure", "dimension", "id", "text", "time", "ignore"):
            raise HTTPException(400, "role must be measure, dimension, id, text, time or ignore")
        if patch.get("agg") and patch["agg"] not in ("sum", "mean", "last", "min", "max", "median", "ratio"):
            raise HTTPException(400, "agg must be sum, mean, last, min, max, median or ratio")
        e.setdefault("columns", {}).setdefault(c, {}).update(patch)
    save_overrides(STORE.root, ov)
    STORE.reload()
    return {"ok": True}


@app.post("/api/tables/{key}/metrics")
def add_metric(key: str, body: MetricIn):
    from .pipeline.dictionary import load_overrides, parse_metric, save_overrides
    t = STORE.tables.get(key)
    if t is None:
        raise HTTPException(404, "table not found")
    ok, msg, _ = parse_metric(body.expr, t.cols)
    if not ok:
        raise HTTPException(400, msg)
    name = re.sub(r"[^a-z0-9_]+", "_", body.name.lower()).strip("_") or "metric"
    ov = load_overrides(STORE.root)
    ov.setdefault(key, {}).setdefault("metrics", {})[name] = {"label": body.label, "expr": body.expr, "unit": body.unit, "unit_kind": body.unit_kind}
    save_overrides(STORE.root, ov)
    STORE.reload()
    return {"ok": True, "name": name}


@app.delete("/api/tables/{key}/metrics/{name}")
def del_metric(key: str, name: str):
    from .pipeline.dictionary import load_overrides, save_overrides
    ov = load_overrides(STORE.root)
    ov.get(key, {}).get("metrics", {}).pop(name, None)
    save_overrides(STORE.root, ov)
    STORE.reload()
    return {"ok": True}


@app.post("/api/tables/{key}/rollback")
def rollback(key: str):
    ok = STORE.wh.rollback(key)
    STORE.reload()
    return {"ok": ok}


@app.delete("/api/tables/{key}")
def delete_table(key: str):
    STORE.wh.delete(key)
    STORE.reload()
    return {"ok": True}


@app.delete("/api/files-in-store/{kind}/{name}")
def delete_file(kind: str, name: str):
    if kind not in ("documents", "templates", "drafts"):
        raise HTTPException(400, "kind must be documents, templates or drafts")
    ok = STORE.wh.remove_file(kind, name)
    STORE.reload()
    return {"ok": ok}


@app.post("/api/reset")
def reset(confirm: str = ""):
    if confirm != "DELETE ALL DATA":
        raise HTTPException(400, "Send confirm='DELETE ALL DATA' to remove every table, document, run and output")
    STORE.wh.wipe()
    for folder in (LANDING_DIR, OUTPUT_DIR):
        for p in folder.glob("*"):
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
    for p in STATE_DIR.glob("*"):
        if p.is_file() and p.name != "settings.yaml":
            p.unlink()
    from .pipeline import runner
    runner.RUNS.clear()
    from .query import router
    router.SESSIONS.clear()
    STORE.reload()
    return {"ok": True, "empty": STORE.is_empty()}


# ------------------------------------------------------------------------------------- asking
@app.post("/api/ask")
def ask(body: AskIn):
    from .query.router import ask as _ask
    return JSONResponse(clean(_ask(body.question, body.session_id)))


@app.post("/api/feedback")
def feedback(body: FeedbackIn):
    with open(STATE_DIR / "feedback.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({**body.model_dump(), "ts": datetime.now().isoformat(timespec="seconds")}) + "\n")
    return {"ok": True}


@app.get("/api/insights")
def insights(scope: str = "all"):
    from .insights.engine import run_insights
    if STORE.is_empty():
        return {"findings": [], "empty": True}
    return clean({"findings": run_insights(STORE, scope), "empty": False})


@app.get("/api/actions")
def actions():
    from .decision.support import top_actions
    if STORE.is_empty():
        return {"empty": True}
    return JSONResponse(clean(top_actions(STORE)))


@app.get("/api/alerts")
def alerts():
    from .alerts.engine import evaluate, list_rules, notifications
    return clean({"alerts": [] if STORE.is_empty() else evaluate(STORE), "rules": list_rules(), "notifications": notifications()[-100:]})


@app.post("/api/alerts/rules")
def add_rule(body: RuleIn):
    from .alerts.engine import evaluate_rule, run_rules, save_rule
    try:
        r = save_rule(body.model_dump(), STORE)
    except ValueError as e:
        raise HTTPException(400, str(e))
    run_rules(STORE)
    return clean({"rule": r, "now": evaluate_rule(STORE, r)})


@app.delete("/api/alerts/rules/{rid}")
def del_rule(rid: str):
    from .alerts.engine import delete_rule
    return {"ok": delete_rule(rid)}


@app.post("/api/notifications/read")
def read_notes():
    from .alerts.engine import mark_read
    mark_read()
    return {"ok": True}


# ------------------------------------------------------------------------------------- reports, decks, audit
@app.post("/api/reports")
def reports(body: ReportIn):
    from .query.router import session
    from .reports.builder import build, infographic
    if STORE.is_empty():
        raise HTTPException(400, "Load data first (Pipeline page)")
    if body.kind == "infographic":
        return JSONResponse(clean(infographic(STORE)))
    spec = {k: v for k, v in body.model_dump().items() if v not in (None, [], {}, "") and k not in ("kind", "session_id")}
    r = build(STORE, spec)
    if r.get("report_spec"):
        session(body.session_id)["last_report_spec"] = r.pop("report_spec")
    return JSONResponse(clean(r))


@app.post("/api/decks")
def decks(body: DeckIn):
    from .decks.builder import _deliver, overview_deck, topic_deck
    from .query.router import session
    if STORE.is_empty():
        raise HTTPException(400, "Load data first (Pipeline page)")
    tpl = list(STORE.templates.values())[-1] if body.use_template and STORE.templates else None
    spec = topic_deck(STORE, body.topic, body.slides or 5, tpl) if body.topic.strip() else None
    if spec is None:
        spec = overview_deck(STORE, body.slides or 10, tpl)
    r = _deliver(spec)
    session(body.session_id)["last_deck_spec"] = r.pop("deck_spec")
    return JSONResponse(clean(r))


@app.post("/api/audit")
async def audit(file: UploadFile = File(...)):
    from .audit.checker import audit_file
    if STORE.is_empty():
        raise HTTPException(400, "Load the source data first - the check compares the draft with it")
    name = _safe(file.filename or "draft.xlsx")
    dest = LANDING_DIR / f"{datetime.now():%Y%m%d_%H%M%S}__{name}"
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    STORE.wh.put_file("drafts", name, dest)
    STORE.reload()
    return JSONResponse(clean(audit_file(STORE, dest, name)))


@app.get("/api/files")
def files():
    out = []
    for p in sorted(OUTPUT_DIR.glob("*"), key=lambda x: -x.stat().st_mtime):
        if p.is_file():
            out.append({"name": p.name, "size": p.stat().st_size, "modified": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")})
    return {"files": out}


@app.get("/api/files/{name}")
def get_file(name: str, inline: bool = False):
    p = (OUTPUT_DIR / name).resolve()
    if p.parent != OUTPUT_DIR.resolve() or not p.exists():
        raise HTTPException(404, "file not found")
    return FileResponse(p, filename=None if inline else p.name)


@app.get("/api/history")
def history(n: int = 50):
    p = STATE_DIR / "query_log.jsonl"
    if not p.exists():
        return {"items": []}
    return {"items": [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()[-n:]][::-1]}


DIST = PROJECT_DIR / "frontend" / "dist"
if DIST.exists():
    app.mount("/", StaticFiles(directory=str(DIST), html=True), name="ui")
