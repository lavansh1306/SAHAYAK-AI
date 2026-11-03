"""Pipeline orchestrator. Every uploaded file becomes a run that passes through 7 visible stages:

  1 Ingest  2 Profile & match  3 Clean & standardise  4 Validate  5 Load to warehouse  6 Update dictionary  7 Ready

A run pauses after stage 2 ('needs review') when something cannot be decided safely - an ambiguous date format,
a column whose type is uncertain, or a file that partly matches an existing table. The user confirms on the
Pipeline page and the run continues. Every stage records status, time taken, a summary and a detailed log."""
from __future__ import annotations

import hashlib
import json
import threading
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd

from ..config import STATE_DIR, app_settings, fy_start
from ..util import clean as jclean, group, slug
from . import clean as C
from .dictionary import build_entry
from .profile import clean_name, profile_column, unit_column, wide_layout
from .readers import RawFile, RawTable, read_file
from .validate import apply_relations, validate

STAGES = [("ingest", "Ingest"), ("profile", "Profile & match"), ("clean", "Clean & standardise"), ("validate", "Validate"),
          ("load", "Load to warehouse"), ("dictionary", "Update dictionary"), ("ready", "Ready")]
RUNS_PATH = STATE_DIR / "pipeline_runs.json"
RUNS: dict[str, dict] = {}
RAW: dict[str, RawFile] = {}
_lock = threading.RLock()
PIPE_LOCK = threading.Lock()  # one load at a time keeps the warehouse consistent


def _load_runs() -> None:
    if RUNS:
        return
    try:
        for r in json.loads(RUNS_PATH.read_text(encoding="utf-8")):
            if r.get("status") in ("running", "queued"):
                r["status"], r["message"] = "failed", "Interrupted (server restarted). Upload the file again."
            if r.get("status") == "needs_review":
                r["status"], r["message"] = "failed", "Waiting for review when the server restarted. Upload the file again."
            RUNS[r["id"]] = r
    except Exception:
        pass


def _save() -> None:
    with _lock:
        items = sorted(RUNS.values(), key=lambda r: r["created"])[-200:]
        try:
            RUNS_PATH.write_text(json.dumps(jclean(items), default=str), encoding="utf-8")
        except Exception:
            pass


def list_runs() -> list[dict]:
    _load_runs()
    return sorted(RUNS.values(), key=lambda r: r["created"], reverse=True)


def get_run(rid: str) -> dict | None:
    _load_runs()
    return RUNS.get(rid)


def _stage(run: dict, key: str) -> dict:
    return next(s for s in run["stages"] if s["key"] == key)


class _Timer:
    def __init__(self, run, key):
        self.run, self.st = run, _stage(run, key)

    def __enter__(self):
        self.t0 = time.time()
        self.st["status"] = "running"
        self.run["status"] = "running"
        _save()
        return self.st

    def __exit__(self, et, ev, tb):
        self.st["ms"] = int((time.time() - self.t0) * 1000)
        if et is not None:
            self.st["status"] = "failed"
            self.st["summary"] = f"{et.__name__}: {ev}"
            self.st["details"].append({"level": "error", "detail": "".join(traceback.format_exception(et, ev, tb))[-1800:]})
            self.run["status"] = "failed"
            self.run["message"] = f"Stage '{self.st['name']}' failed: {ev}"
            _save()
            return True
        if self.st["status"] == "running":
            self.st["status"] = "done"
        self.run["updated"] = datetime.now().isoformat(timespec="seconds")
        _save()
        return False


def _preview(df: pd.DataFrame, n: int = 6) -> dict:
    h = df.head(n).copy()
    return {"columns": [str(c) for c in h.columns],
            "rows": [[None if (v is None or (isinstance(v, float) and pd.isna(v)) or v is pd.NaT) else str(v)[:40] for v in r] for r in h.astype(object).values.tolist()]}


# ------------------------------------------------------------------------------------ matching
def _similarity(cols_new: dict[str, dict], entry: dict) -> tuple[float, dict]:
    from rapidfuzz import fuzz
    old = entry["columns"]
    mapping, hits = {}, 0
    for c, p in cols_new.items():
        if p.get("role") == "ignore":
            continue
        if c in old:
            mapping[c] = c
            hits += 1
            continue
        best, bs = None, 0
        for oc, oe in old.items():
            if oc in mapping.values():
                continue
            s = fuzz.token_set_ratio(c.replace("_", " "), oc.replace("_", " "))
            if s > bs and (oe.get("type") == p.get("type") or {oe.get("type"), p.get("type")} <= {"category", "id"}):
                best, bs = oc, s
        if best and bs >= 88:
            mapping[c] = best
            hits += 1
    n_new = len([p for p in cols_new.values() if p.get("role") != "ignore"])
    denom = max(n_new, len(old))
    return (hits / denom if denom else 0.0), mapping


def _match_table(cols: dict, name: str, store) -> dict:
    best = {"action": "new", "key": None, "similarity": 0.0, "mapping": {}}
    for k, t in store.tables.items():
        sim, mp = _similarity(cols, t.entry)
        if slug(name) == k:
            sim = min(1.0, sim + 0.1)
        if sim > best["similarity"]:
            best = {"action": "merge", "key": k, "label": t.label, "similarity": round(sim, 3), "mapping": mp}
    return best


def _new_key(name: str, store, taken: set) -> str:
    base = slug(name, 40) or "table"
    k, i = base, 2
    while k in store.tables or k in taken:
        k, i = f"{base}_{i}", i + 1
    return k


# ------------------------------------------------------------------------------------ stages
def _ingest(run, rf: RawFile):
    with _Timer(run, "ingest") as st:
        if rf.kind == "document":
            st["summary"] = "Document received - text and sections will be indexed for questions and checks"
        elif rf.kind == "template":
            st["summary"] = "PowerPoint template received - used when you ask for a deck in your template"
        else:
            if not rf.tables:
                raise ValueError("No table with data was found in the file")
            st["summary"] = f"{len(rf.tables)} table(s) found: " + "; ".join(f"{t.origin} ({group(len(t.df))} rows x {t.df.shape[1]} columns)" for t in rf.tables)
            for t in rf.tables:
                st["details"].append({"level": "info", "detail": f"{t.origin}: header at row {t.header_row or 1}; columns: {', '.join(map(str, t.df.columns))[:400]}"})
            for n in rf.notes[:5]:
                st["details"].append({"level": "info", "detail": f"Note kept from the file: {n[:200]}"})


def _profile(run, rf: RawFile, store) -> bool:
    """Returns True when the run can continue without the user."""
    cfg = app_settings()["pipeline"]
    with _Timer(run, "profile") as st:
        if rf.kind != "table":
            st["summary"] = "Not a table - nothing to profile"
            return True
        run["tables"], needs, taken = [], False, set()
        for t in rf.tables:
            log = C.Log()
            df = t.df
            transforms = []
            wide = wide_layout(df)
            if wide:
                uc0 = unit_column(df[wide["id_columns"]])
                vname = "value"
                if uc0:
                    from .profile import ROW_UNITS
                    kinds = [k for v in df[uc0].dropna().astype(str) for rx, k, _ in ROW_UNITS if rx.match(v)]
                    if kinds:
                        vname = {"volume": "volume", "money": "amount", "mass": "quantity"}.get(max(set(kinds), key=kinds.count), "value")
                wide["value_name"] = vname
                df = C.melt_wide(df, wide, log)
                transforms.append({"kind": "wide_to_long", **wide})
            names, seen = {}, set()
            for c in df.columns:
                n = clean_name(c)
                while n in seen:
                    n = n + "_2"
                seen.add(n)
                names[c] = n
            ucol = unit_column(df)
            cols = {}
            for orig, n in names.items():
                cols[n] = profile_column(orig, n, df[orig])
            if ucol:
                transforms.append({"kind": "row_units", "column": names[ucol]})
                cols[names[ucol]].update(role="dimension", type="category")
            tcol = C.primary_time(list(cols.values()))
            match = _match_table(cols, t.name, store)
            review = []
            if match["similarity"] >= cfg["same_table_similarity"]:
                target = {"action": "merge", "key": match["key"], "label": match["label"], "similarity": match["similarity"], "mapping": match["mapping"]}
                if store.tables[match["key"]].entry.get("time"):
                    tcol = next((nc for nc, oc in match["mapping"].items() if oc == store.tables[match["key"]].entry["time"]), tcol)
            elif match["similarity"] >= cfg["review_table_similarity"]:
                target = {"action": "new", "key": _new_key(t.name, store, taken), "label": t.name[:1].upper() + t.name[1:], "similarity": match["similarity"],
                          "suggest": {"key": match["key"], "label": match["label"], "mapping": match["mapping"]}}
                review.append(f"Looks similar to existing table '{match['label']}' ({match['similarity']:.0%} of columns match). Add the rows to it, or keep as a new table?")
            else:
                target = {"action": "new", "key": _new_key(t.name, store, taken), "label": t.name[:1].upper() + t.name[1:], "similarity": match["similarity"]}
            taken.add(target["key"])
            for n, p in cols.items():
                if p.get("date_ambiguous"):
                    review.append(f"'{p['original']}': every date has day and month 12 or below, so day/month order cannot be told from the data. Confirm the format.")
                if p.get("confidence", 1) < cfg["auto_accept_confidence"] and p.get("role") != "ignore":
                    review.append(f"'{p['original']}' read as {p['type']} with only {p['confidence']:.0%} confidence. Confirm the type.")
            entry = {"origin": t.origin, "name": t.name, "rows_raw": int(len(t.df)), "preview": _preview(t.df), "transforms": transforms,
                     "columns": [cols[n] for n in cols], "time": tcol, "unit_col": names.get(ucol) if ucol else None, "target": target,
                     "review": review, "status": "review" if review else "auto", "profile_log": log.items}
            run["tables"].append(entry)
            needs |= bool(review)
            kinds = {}
            for p in cols.values():
                kinds[p["role"]] = kinds.get(p["role"], 0) + 1
            st["details"].append({"level": "info", "detail": f"{t.origin}: " + ", ".join(f"{v} {k}" for k, v in kinds.items()) + (f"; time column '{tcol}'" if tcol else "; no time column")})
            for p in cols.values():
                st["details"].append({"level": "info", "detail": f"  {p['original']} -> {p['name']}: {p['type']}, {p['role']}" + (f", adds up by {p['agg']}" if p.get("agg") else "")
                                      + (f", unit {p['unit']}" if p.get("unit") else "") + (f" (confidence {p['confidence']:.0%})" if p.get("confidence", 1) < 1 else "")})
            st["details"].append({"level": "info" if target["action"] == "merge" else "info",
                                  "detail": (f"{t.origin}: same layout as table '{target['label']}' ({target['similarity']:.0%} column match) - rows will be added to it"
                                             if target["action"] == "merge" else f"{t.origin}: new table '{target['label']}'")})
            for r in review:
                st["details"].append({"level": "warn", "detail": f"{t.origin}: {r}"})
            for i in log.items:
                st["details"].append({"level": i["level"], "detail": f"{t.origin}: {i['detail']}"})
        st["summary"] = "; ".join(f"{e['origin']} -> {'add to ' + e['target']['label'] if e['target']['action'] == 'merge' else 'new table ' + e['target']['label']}"
                                  for e in run["tables"])
        if needs:
            st["status"] = "review"
        return not needs


def _decided_cols(e: dict) -> list[dict]:
    d = e.get("decision") or {}
    out = []
    for p in e["columns"]:
        p = dict(p)
        if (d.get("types") or {}).get(p["name"]):
            nt = d["types"][p["name"]]
            p["type"] = nt
            p["role"] = {"number": "measure", "date": "time", "datetime": "time", "month": "time", "year": "time", "id": "id", "text": "text",
                         "ignore": "ignore"}.get(nt, "dimension")
            if nt == "number" and not p.get("agg"):
                p["agg"] = "sum"
            p["confidence"] = 1.0
        if (d.get("roles") or {}).get(p["name"]):
            p["role"] = d["roles"][p["name"]]
        if (d.get("date_order") or {}).get(p["name"]):
            p["date_order"] = d["date_order"][p["name"]]
            p["date_ambiguous"] = False
        out.append(p)
    return out


def _clean(run, rf: RawFile, store) -> dict:
    cfg = app_settings()["pipeline"]
    out = {}
    with _Timer(run, "clean") as st:
        if rf.kind != "table":
            st["summary"] = "Nothing to clean"
            return out
        for e, t in zip(run["tables"], rf.tables):
            if e.get("status") == "skip":
                continue
            log = C.Log()
            df = t.df
            for tr in e["transforms"]:
                if tr["kind"] == "wide_to_long":
                    df = C.melt_wide(df, tr, C.Log())
            names, seen = {}, set()
            for c in df.columns:
                n = clean_name(c)
                while n in seen:
                    n = n + "_2"
                seen.add(n)
                names[c] = n
            df = df.rename(columns=names)
            cols = _decided_cols(e)
            target = e["target"]
            existing = store.tables[target["key"]].entry if target["action"] == "merge" and target["key"] in store.tables else None
            mapping = (target.get("mapping") or {}) if existing else {}
            if mapping:
                ren = {a: b for a, b in mapping.items() if a != b and b}
                if ren:
                    df = df.rename(columns=ren)
                    for p in cols:
                        if p["name"] in ren:
                            p["name"] = ren[p["name"]]
                    log.add("columns", "Columns matched to the existing table: " + ", ".join(f"{a} -> {b}" for a, b in ren.items()), "info")
                for p in cols:  # keep the existing table's types for shared columns
                    oe = existing["columns"].get(p["name"])
                    if oe and oe.get("type") and oe["type"] != p["type"] and not (e.get("decision") or {}).get("types", {}).get(p["name"]):
                        p.update(type=oe["type"], role=oe.get("role", p["role"]), agg=oe.get("agg", p.get("agg")))
            tcol = (e.get("decision") or {}).get("time") or e.get("time")
            if mapping and tcol in mapping:
                tcol = mapping[tcol] or tcol
            if existing and existing.get("time"):
                tcol = existing["time"] if existing["time"] in df.columns else tcol
            ucol = e.get("unit_col")
            pre = {}
            if ucol and ucol in df.columns:
                mcols = [p["name"] for p in cols if p["type"] == "number" and p["name"] in df.columns]
                for p in cols:
                    if p["name"] in mcols:
                        from .profile import parse_numbers
                        v, bad, _ = parse_numbers(df[p["name"]])
                        if bad.any():
                            log.add("invalid", f"'{p['name']}': {int(bad.sum())} value(s) could not be read ({C._examples(df[p['name']][bad].tolist())}) - left blank, not guessed", "warn", p["name"], int(bad.sum()))
                            pre[p["name"]] = int(bad.sum())
                        df[p["name"]] = v
                df, uinfo = C.apply_row_units(df, ucol, mcols, log)
                for p in cols:
                    if p["name"] == ucol:
                        p["name"] = "unit_reported"
                    if p["name"] in mcols and uinfo:
                        kind = uinfo["unit_kind"]
                        p.update(unit=uinfo["unit"], unit_kind=kind, scale={"volume": 1.0, "money": 1e5, "mass": 1.0}[kind])
            clean_df, clog, stats = C.standardise(df, cols, {"time": tcol, "date_order": (e.get("decision") or {}).get("date_order") or {}}, existing, cfg)
            if pre:
                stats["invalid"] = {**pre, **stats.get("invalid", {})}
            log.items += clog.items
            colmap = {p["name"]: p for p in cols if p["name"] in clean_df.columns}
            ex_keys = (existing or {}).get("keys") or []
            missing_keys = [k for k in ex_keys if k not in clean_df.columns]
            if existing and missing_keys:
                stats["missing_keys"] = missing_keys
                log.add("columns", f"This file has no {', '.join(missing_keys)} column(s), which table '{existing.get('label')}' uses to identify rows", "error")
            keys = [k for k in ex_keys if k in clean_df.columns] if ex_keys and not missing_keys else C.detect_keys(clean_df, list(colmap.values()), tcol)
            out[id(t)] = {"df": clean_df, "cols": colmap, "time": tcol, "keys": keys, "stats": stats}
            e["clean"] = {"rows_in": stats["rows_in"], "rows_out": stats["rows_out"], "log": log.items, "keys": keys, "time": tcol}
            e["preview_clean"] = _preview(clean_df)
            for i in log.items:
                st["details"].append({"level": i["level"], "detail": f"{e['origin']}: {i['detail']}"})
        st["summary"] = "; ".join(f"{e['origin']}: {group(e['clean']['rows_in'])} -> {group(e['clean']['rows_out'])} rows, {len(e['clean']['log'])} change(s) logged"
                                  for e in run["tables"] if e.get("clean")) or "nothing to clean"
        if any(i["level"] == "warn" for e in run["tables"] for i in (e.get("clean") or {}).get("log", [])):
            st["status"] = "warn"
    return out


def _validate(run, rf: RawFile, cleaned: dict) -> None:
    from ..query.periods import today
    cfg = app_settings()["pipeline"]
    with _Timer(run, "validate") as st:
        for e, t in zip(run["tables"], rf.tables):
            if id(t) not in cleaned:
                continue
            c = cleaned[id(t)]
            checks, rels, quality, q = validate(c["df"], c["cols"], c["time"], c["keys"], c["stats"], cfg, fy_start(), pd.Timestamp(today()).to_pydatetime())
            notes = apply_relations(c["cols"], rels)
            c.update(checks=checks, rels=rels, quality=quality, quarantined=q, notes=notes)
            e["validation"] = {"checks": checks, "quality": quality, "quarantined": q, "relations": [r["formula"] for r in rels], "notes": notes}
            for ch in checks:
                if ch["status"] != "pass":
                    st["details"].append({"level": "warn" if ch["status"] == "warn" else "error", "detail": f"{e['origin']}: {ch['check']} - {ch['detail']}"})
            for r in rels:
                st["details"].append({"level": "info", "detail": f"{e['origin']}: formula found - {r['formula']}"})
            for n in notes:
                st["details"].append({"level": "info", "detail": f"{e['origin']}: {n}"})
        st["summary"] = "; ".join(f"{e['origin']}: quality {e['validation']['quality']:.0f}%" + (" - QUARANTINED" if e["validation"]["quarantined"] else "")
                                  for e in run["tables"] if e.get("validation")) or "nothing to validate"
        if any(e.get("validation", {}).get("quarantined") for e in run["tables"]) or any(ch["status"] == "warn" for e in run["tables"] for ch in e.get("validation", {}).get("checks", [])):
            st["status"] = "warn"


def _load(run, rf: RawFile, cleaned: dict, store) -> list[str]:
    from ..documents import load_document
    touched = []
    with _Timer(run, "load") as st:
        if rf.kind == "document":
            doc = load_document(Path(run["path"]), run["file"])
            dest = store.wh.put_file("documents", run["file"], Path(run["path"]), {"kind": doc.kind, "passages": len(doc.passages)})
            Path(str(dest) + ".json").write_text(json.dumps(doc.to_json()), encoding="utf-8")
            st["summary"] = f"Document stored: {len(doc.passages)} passages, {len(doc.sections)} sections indexed"
            if len(doc.text.strip()) < 50:
                st["status"] = "warn"
                st["details"].append({"level": "warn", "detail": "Very little text could be read (scanned PDF?). Questions about it will not work well."})
            return touched
        if rf.kind == "template":
            store.wh.put_file("templates", run["file"], Path(run["path"]))
            st["summary"] = "Template stored"
            return touched
        msgs = []
        for e, t in zip(run["tables"], rf.tables):
            if id(t) not in cleaned:
                continue
            c = cleaned[id(t)]
            if c["quarantined"]:
                msgs.append(f"{e['origin']}: NOT loaded (quarantined)")
                continue
            target = e["target"]
            key = target["key"]
            mode = run["mode"]
            existing = store.tables.get(key) if target["action"] == "merge" else None
            res = store.wh.write(key, c["df"], c["keys"], mode if existing else "replace", run["id"], run["file"])
            merged = store.wh.read(key)
            data = merged.drop(columns=[x for x in merged.columns if x.startswith("_")])
            specs = dict(existing.entry["columns"]) if existing else {}
            for n, p in c["cols"].items():
                if n not in specs or not existing:
                    specs[n] = p
                elif p.get("agg") == "ratio" and specs[n].get("agg") != "ratio":
                    specs[n] = {**specs[n], "agg": "ratio", "ratio": p.get("ratio")}
            tcol = (existing.entry.get("time") if existing else None) or c["time"]
            # whole-table checks after the merge (coverage, formulas, quality)
            from ..query.periods import today
            checks, rels, quality, _ = validate(data, specs, tcol, c["keys"], {"rows_in": len(data)}, app_settings()["pipeline"], fy_start(),
                                                pd.Timestamp(today()).to_pydatetime())
            notes = apply_relations(specs, rels)
            file_warn = [ch for ch in c["checks"] if ch["status"] != "pass" and ch["check"].startswith(("Readable", "Time values"))]
            label = existing.label if existing else target.get("label") or e["name"]
            entry = build_entry(key, label, merged, specs, tcol, c["keys"],
                                {"validation": checks + [dict(ch, detail=f"latest file: {ch['detail']}") for ch in file_warn],
                                 "relations": rels, "quality": quality, "status": "warn" if any(ch["status"] != "pass" for ch in checks) else "ok",
                                 "notes": notes + [f"{t2['kind']} transform applied" for t2 in e["transforms"]], "origin": e["origin"]})
            store.wh.set_info(key, entry=jclean(entry), label=label)
            e["load"] = {**res, "table": key, "label": label}
            touched.append(key)
            msgs.append(f"{label}: +{group(res['inserted'])} new, {group(res['updated'])} already present/updated -> {group(res['rows_after'])} rows (version {res['version']})")
        st["summary"] = "; ".join(msgs) or "nothing loaded"
        st["details"] = [{"level": "warn" if "NOT loaded" in m else "info", "detail": m} for m in msgs]
        if any("NOT loaded" in m for m in msgs):
            st["status"] = "warn"
    return touched


def _dictionary(run, store, touched: list[str]) -> None:
    with _Timer(run, "dictionary") as st:
        store.reload()
        n_links = [l for l in store.links if l["child"] in touched or l["parent"] in touched]
        parts = []
        for k in touched:
            t = store.tables.get(k)
            if t:
                parts.append(f"{t.label}: {len(t.measures())} measures, {len(t.dims())} groupings" + (f", time {t.entry.get('time_range', ['', ''])[0][:10]} to {t.entry.get('time_range', ['', ''])[1][:10]}" if t.time else ""))
        st["summary"] = "; ".join(parts) or f"{len(store.tables)} table(s), {len(store.documents)} document(s) available"
        for l in n_links:
            st["details"].append({"level": "info", "detail": f"Link found: {l['text']}"})
        for k in touched:
            t = store.tables.get(k)
            if t:
                for n in t.entry.get("notes", []):
                    st["details"].append({"level": "info", "detail": f"{t.label}: {n}"})
        try:
            from ..alerts.engine import run_rules
            run_rules(store)
        except Exception:
            pass


def _ready(run, rf: RawFile, store, touched: list[str]) -> None:
    from ..store import examples
    with _Timer(run, "ready") as st:
        if rf.kind == "document":
            st["summary"] = f"Ask questions about {run['file']}, or ask to check its numbers against the data"
        elif rf.kind == "template":
            st["summary"] = "Ask e.g. 'make a deck on ... in our template'"
        else:
            ex = [i for g in examples(store) for i in g["items"] if any(store.tables.get(k) and store.tables[k].label == g["name"] for k in touched)]
            st["summary"] = ("Try: " + " | ".join(ex[:3])) if ex else "Loaded"
    qd = [e for e in run["tables"] if (e.get("validation") or {}).get("quarantined")]
    loaded = [e for e in run["tables"] if e.get("load")]
    if qd and not loaded:
        run["status"] = "quarantined"
    else:
        run["status"] = "succeeded_with_warnings" if (qd or any(s["status"] == "warn" for s in run["stages"])) else "succeeded"
    run["message"] = f"{len(qd)} table(s) quarantined - not loaded" if qd else "Done"
    _save()


# ------------------------------------------------------------------------------------ public
def _execute(run: dict, store, resume: bool = False, auto_accept: bool = False) -> dict:
    try:
        rf = RAW.get(run["id"]) or read_file(Path(run["path"]), run["file"])
        RAW[run["id"]] = rf
        if not resume:
            _ingest(run, rf)
            if run["status"] == "failed":
                return run
            ok = _profile(run, rf, store)
            if run["status"] == "failed":
                return run
            if not ok:
                if auto_accept:
                    st = _stage(run, "profile")
                    st["status"] = "warn"
                    st["details"].append({"level": "warn", "detail": "Uncertain items were accepted on the best guess automatically"})
                    for e in run["tables"]:
                        if e["status"] == "review":
                            e["status"] = "accepted"
                else:
                    run["status"], run["message"] = "needs_review", "Please confirm the highlighted items before loading"
                    _save()
                    return run
        with PIPE_LOCK:
            cleaned = _clean(run, rf, store)
            if run["status"] == "failed":
                return run
            _validate(run, rf, cleaned)
            if run["status"] == "failed":
                return run
            touched = _load(run, rf, cleaned, store)
            if run["status"] == "failed":
                return run
            _dictionary(run, store, touched)
            if run["status"] == "failed":
                return run
            _ready(run, rf, store, touched)
    except Exception as ex:  # pragma: no cover - defensive
        run["status"], run["message"] = "failed", f"{type(ex).__name__}: {ex}"
        _save()
    finally:
        if run["status"] != "needs_review":
            RAW.pop(run["id"], None)
    return run


def submit(path: Path, name: str, mode: str = "merge", store=None, background: bool = True, auto_accept: bool = False) -> dict:
    from ..store import STORE
    store = store or STORE
    _load_runs()
    path = Path(path)
    run = {"id": uuid.uuid4().hex[:10], "file": name, "path": str(path), "size": path.stat().st_size,
           "sha256": hashlib.sha256(path.read_bytes()).hexdigest()[:16], "mode": mode, "status": "queued",
           "created": datetime.now().isoformat(timespec="seconds"), "updated": None, "message": "",
           "stages": [{"key": k, "name": n, "status": "pending", "ms": None, "summary": "", "details": []} for k, n in STAGES], "tables": []}
    with _lock:
        RUNS[run["id"]] = run
    _save()
    try:
        RAW[run["id"]] = read_file(path, name)
    except Exception as ex:
        st = _stage(run, "ingest")
        st["status"], st["summary"] = "failed", f"Could not open the file: {ex}"
        run["status"], run["message"] = "failed", f"Could not open the file: {ex}"
        _save()
        return run
    if background:
        threading.Thread(target=_execute, args=(run, store, False, auto_accept), daemon=True).start()
    else:
        _execute(run, store, False, auto_accept)
    return run


def resolve(run_id: str, decisions: list[dict], store=None, background: bool = True) -> dict:
    """decisions: [{origin, action: load|skip, target: 'new' | existing table key, name, types{}, roles{}, date_order{}, time, mapping{}}]"""
    from ..store import STORE
    store = store or STORE
    run = get_run(run_id)
    if run is None:
        raise KeyError(run_id)
    if run["status"] != "needs_review":
        raise ValueError("This run is not waiting for review")
    by = {d["origin"]: d for d in decisions}
    taken = set()
    for e in run["tables"]:
        d = by.get(e["origin"])
        if d is None:
            if e["status"] == "review":
                raise ValueError(f"No decision for table '{e['origin']}'")
            continue
        if d.get("action") == "skip":
            e["status"] = "skip"
            continue
        tg = d.get("target") or e["target"]["key"]
        if tg != "new" and tg in store.tables:
            sim_mapping = e["target"].get("mapping") or (e["target"].get("suggest") or {}).get("mapping") or {}
            e["target"] = {"action": "merge", "key": tg, "label": store.tables[tg].label, "similarity": e["target"].get("similarity", 0),
                           "mapping": {**sim_mapping, **{k: v for k, v in (d.get("mapping") or {}).items()}}}
        else:
            nm = (d.get("name") or e["target"].get("label") or e["name"]).strip()
            e["target"] = {"action": "new", "key": _new_key(nm, store, taken), "label": nm, "similarity": e["target"].get("similarity", 0)}
            taken.add(e["target"]["key"])
        valid_types = {"number", "date", "datetime", "month", "year", "category", "id", "text", "boolean", "fy_label", "ignore"}
        for c, tp in (d.get("types") or {}).items():
            if tp not in valid_types:
                raise ValueError(f"Unknown type '{tp}' for column '{c}'")
        e["decision"] = {k: d.get(k) for k in ("types", "roles", "date_order", "time")}
        e["status"] = "confirmed"
    st = _stage(run, "profile")
    st["details"].append({"level": "info", "detail": "Confirmed by the user: " + "; ".join(f"{e['origin']} -> {e['status']}" for e in run["tables"])})
    st["status"] = "done"
    run["status"] = "running"
    _save()
    if background:
        threading.Thread(target=_execute, args=(run, store, True, False), daemon=True).start()
    else:
        _execute(run, store, True, False)
    return run
