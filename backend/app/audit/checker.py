"""Check a manually prepared Excel summary against the loaded data, cell by cell - for any data.

For each sheet: find the header and the row-label column, work out which table/grouping the row labels belong to,
then for every numeric column find the definition (measure x aggregation x period) that reproduces most cells.
Cells that disagree are flagged with a likely cause (unit slip, digit typo, stale value from another period, value
from another row, broken total formula, missing row). Returns findings and an annotated copy of the workbook."""
from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

import numpy as np
import openpyxl
import pandas as pd
from openpyxl.comments import Comment
from openpyxl.styles import PatternFill

from ..config import OUTPUT_DIR, fy_start
from ..pipeline.profile import TOTAL_RE, _month_from_text
from ..query import engine as E
from ..query.answer import result
from ..query.periods import add_months, describe, fy_start_year, month_long, parse_period, today
from ..query.understand import expand, score_col, words
from ..reports.builder import MONEY
from ..util import group

RED = PatternFill("solid", fgColor="F8CBAD")
AMBER = PatternFill("solid", fgColor="FFE699")


def _num(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _decs(v: float) -> int:
    s = repr(float(v))
    if "e" in s:
        return 6
    frac = s.split(".")[1] if "." in s else ""
    frac = frac.rstrip("0")
    return min(len(frac), 6)


def _close(draft: float, true: float) -> bool:
    if true is None or np.isnan(true):
        return False
    d = _decs(draft)
    return abs(draft - true) <= max(0.51 * 10 ** (-d), 1e-9) + 1e-9 or (abs(true) > 1000 and abs(draft - true) / abs(true) < 5e-6)


def _guess_period(texts: list[str]) -> str | None:
    for t in texts:
        t = str(t or "")
        for m in re.finditer(r"([A-Za-z]{3,9})[\s_\-']*(\d{4}|\d{2})\b", t):
            mm = _month_from_text(f"{m.group(1)} {m.group(2)}")
            if mm:
                return mm
        p = parse_period(t.lower(), today())
        if p and p.kind == "month":
            return p.start
    return None


def _diagnose(found: float, true: float, other_periods: dict, other_rows: dict) -> str:
    if true:
        r = found / true
        for k in (10, 100, 1000, 1e5, 1e7, 0.1, 0.01, 0.001, 1e-5, 1e-7):
            if abs(r / k - 1) < 0.002:
                return f"unit slip - value is {k:g} times the correct figure (typed in the wrong unit?)"
    for lab, v in other_periods.items():
        if v is not None and _close(found, v):
            return f"stale value - matches {lab} instead"
    for lab, v in other_rows.items():
        if v is not None and _close(found, v):
            return f"value from another row - matches {lab}"
    fs, ts = f"{abs(found):.2f}".rstrip("0").rstrip("."), f"{abs(true):.2f}".rstrip("0").rstrip(".")
    if len(fs) == len(ts):
        if sorted(fs) == sorted(ts):
            return "digits swapped while typing"
        diff = sum(a != b for a, b in zip(fs, ts))
        if diff <= 2:
            return f"typing error ({diff} digit{'s' if diff > 1 else ''} different)"
    if true:
        p = (found / true - 1) * 100
        return f"{abs(p):.1f}% {'higher' if p > 0 else 'lower'} than the data (old rate, wrong filter or a manual adjustment?)"
    return "does not match the data"


def _sheet_frame(ws):
    rows = list(ws.iter_rows(values_only=False))
    if not rows:
        return None
    hdr_i = None
    for i, r in enumerate(rows[:15]):
        vals = [c.value for c in r if c.value is not None and str(c.value).strip()]
        texts = [v for v in vals if isinstance(v, str) and _num(v) is None]
        if len(vals) >= 2 and len(texts) >= 2 and i + 1 < len(rows):
            nxt = rows[i + 1]
            if sum(_num(c.value) is not None for c in nxt) >= 1:
                hdr_i = i
                break
    if hdr_i is None:
        return None
    hdr = [str(c.value).strip() if c.value is not None else "" for c in rows[hdr_i]]
    body = rows[hdr_i + 1:]
    lab_col = None
    for j in range(len(hdr)):
        vals = [r[j].value for r in body if j < len(r) and r[j].value is not None]
        if vals and sum(isinstance(v, str) and _num(v) is None for v in vals) >= 0.7 * len(vals):
            lab_col = j
            break
    if lab_col is None:
        return None
    titles = [" ".join(str(c.value) for c in r if c.value is not None) for r in rows[:hdr_i]]
    return hdr, body, lab_col, titles


def audit_file(store, path: Path, name: str | None = None) -> dict:
    name = name or path.name
    wb = openpyxl.load_workbook(path, data_only=False)
    out_path = OUTPUT_DIR / f"checked_{datetime.now():%Y%m%d_%H%M%S}_{Path(name).stem}.xlsx"
    shutil.copy(path, out_path)
    wb_out = openpyxl.load_workbook(out_path)
    findings, cells, checked = [], [], 0
    used = None
    for ws in wb.worksheets:
        fr = _sheet_frame(ws)
        if not fr:
            continue
        hdr, body, lab_col, titles = fr
        labels, total_row = {}, None
        for r in body:
            if lab_col >= len(r):
                continue
            v = r[lab_col].value
            if v is None or not str(v).strip():
                continue
            if TOTAL_RE.match(str(v)):
                total_row = r
                continue
            if len(str(v)) > 60 or not any(_num(c.value) is not None or (isinstance(c.value, str) and c.value.startswith("=")) for k2, c in enumerate(r) if k2 != lab_col):
                continue  # notes / remarks lines
            labels[str(v).strip()] = r
        if not labels:
            continue
        # which table / grouping column do the row labels belong to?
        cands = []
        low = {k.lower(): k for k in labels}
        for t in store.usable():
            for d in t.dims():
                vals = {str(x).lower() for x in (t.cols[d].get("values") or [])}
                ov = sum(1 for k in low if k in vals) / len(low)
                if ov >= 0.6:
                    cands.append((ov, t, d))
        if not cands:
            findings.append({"item": ws.title, "cell": "-", "found": "-", "correct": "-", "issue": "row labels do not match any loaded table - cannot check this sheet", "definition": ""})
            continue
        cands.sort(key=lambda x: -x[0])
        M0 = _guess_period([ws.title, name] + titles + hdr)
        num_cols = [j for j, h in enumerate(hdr) if j != lab_col and h and sum(_num(r[j].value) is not None for r in labels.values() if j < len(r)) >= 0.6 * len(labels)]
        for j in num_cols:
            h = hdr[j]
            best = None
            hw0 = expand(words(h))
            ranked = sorted(cands, key=lambda c: -(c[0] + max([score_col(hw0, c[1], m, h.lower()) for m in c[1].measures()] or [0])))
            for ov, t, d in ranked[:3]:
                last = (t.entry.get("time_range") or [None, None])[1]
                M = M0 or (str(last)[:7] if last else None)
                periods = {}
                if t.time and M:
                    S = fy_start()
                    fy0 = fy_start_year(M)
                    fys = f"{fy0:04d}-{S:02d}"
                    periods = {month_long(M): {"start": M, "end": M, "label": month_long(M), "kind": "month"},
                               f"{describe(fys, M)} (year to date)": {"start": fys, "end": M, "label": describe(fys, M), "kind": "range"},
                               month_long(add_months(M, -1)): {"start": add_months(M, -1), "end": add_months(M, -1), "label": "", "kind": "month"},
                               month_long(add_months(M, -12)): {"start": add_months(M, -12), "end": add_months(M, -12), "label": "", "kind": "month"}}
                periods["all data"] = None
                hw = expand(words(h))
                ms = sorted(t.measures(), key=lambda c: -score_col(hw, t, c, h.lower()))[:6]
                if not ms:
                    continue
                for plab, per in periods.items():
                    try:
                        x = E.run(store, {"table": t.key, "measures": [{"col": c} for c in ms], "group": [d], "grain": None, "filters": [], "having": [], "period": per,
                                          "compare": None, "sort": None, "limit": None, "share": False, "chart": "auto", "mode": "aggregate"})
                    except E.PlanError:
                        continue
                    res = x["res"].set_index(d)
                    res.index = res.index.astype(str).str.lower()
                    for i, mi in enumerate(x["measures"]):
                        for fac, ulab in _unit_factors(mi, h):
                            truth = {k: (float(res.at[k.lower(), f"m{i}"]) * fac if k.lower() in res.index and pd.notna(res.at[k.lower(), f"m{i}"]) else None) for k in labels}
                            hits = sum(1 for k, r in labels.items() if j < len(r) and _num(r[j].value) is not None and truth[k] is not None and _close(_num(r[j].value), truth[k]))
                            pref = (score_col(hw, t, mi["col"], h.lower()) > 0) * 0.5 + (plab == month_long(M) if M else 0) * 0.3
                            if best is None or hits + pref > best[0]:
                                best = (hits + pref, hits, t, d, mi, plab, per, fac, ulab, truth, x, dict(periods))
            if not best or best[1] == 0:
                findings.append({"item": f"{ws.title}!{h}", "cell": "-", "found": "-", "correct": "-", "issue": "no calculation on the loaded data reproduces this column - cannot check it", "definition": ""})
                continue
            _, hits, t, d, mi, plab, per, fac, ulab, truth, x, periods = best
            used = used or (t, plab)
            definition = f"{mi['text']}{' in ' + ulab if ulab else ''}, {plab}, by {t.cols[d].get('label', d).lower()} ({t.label})"
            # other periods / rows for diagnosis
            others = {}
            extra = {}
            if t.time and per and per.get("end"):
                S = fy_start()
                for back in range(1, 7):
                    mm = add_months(per["end"], -back)
                    extra[month_long(mm)] = {"start": mm, "end": mm, "label": "", "kind": "month"}
                    fys = f"{fy_start_year(mm):04d}-{S:02d}"
                    if fys <= mm:
                        extra[f"year to date up to {month_long(mm)}"] = {"start": fys, "end": mm, "label": "", "kind": "range"}
            for olab, oper in [(k, v) for k, v in ({**(periods if t.time else {}), **extra}).items() if k != plab]:
                try:
                    ox = E.run(store, {**x["plan"], "measures": [{"col": mi["col"]}], "period": oper})
                    rr = ox["res"].set_index(d)
                    rr.index = rr.index.astype(str).str.lower()
                    others[olab] = rr["m0"] * fac
                except Exception:
                    continue
            for k, r in labels.items():
                if j >= len(r):
                    continue
                c = r[j]
                fv = _num(c.value)
                if fv is None:
                    continue
                checked += 1
                tv = truth.get(k)
                ok = tv is not None and _close(fv, tv)
                cells.append({"Sheet": ws.title, "Cell": c.coordinate, "Row": k, "Column": h, "In draft": fv, "From data": None if tv is None else round(tv, 4), "OK": "Yes" if ok else "NO"})
                if not ok and tv is not None:
                    op = {lab: (float(s.get(k.lower())) if k.lower() in s.index else None) for lab, s in others.items()}
                    orow = {kk: truth[kk] for kk in labels if kk != k and truth.get(kk) is not None}
                    issue = _diagnose(fv, tv, op, orow)
                    findings.append({"item": f"{k} - {h}", "cell": f"{ws.title}!{c.coordinate}", "found": fv, "correct": round(tv, 4), "issue": issue, "definition": definition})
                    oc = wb_out[ws.title][c.coordinate]
                    oc.fill = RED
                    oc.comment = Comment(f"SAHAYAK-AI: data gives {group(tv, 2)}. Likely: {issue}. Definition: {definition}", "SAHAYAK-AI")
            # total row
            if total_row is not None and j < len(total_row) and total_row[j].value is not None:
                tc = total_row[j]
                checked += 1
                rows_idx = sorted(r[j].row for r in labels.values() if j < len(r))
                v = tc.value
                issue = None
                if isinstance(v, str) and v.startswith("="):
                    m = re.match(r"=SUM\(([A-Z]+)(\d+):([A-Z]+)(\d+)\)", v.replace(" ", "").upper())
                    if m:
                        lo, hi = int(m.group(2)), int(m.group(4))
                        miss = [i for i in rows_idx if not lo <= i <= hi]
                        if miss:
                            names = [k for k, r in labels.items() if r[j].row in miss]
                            issue = f"SUM range {m.group(1)}{lo}:{m.group(3)}{hi} leaves out row(s) {miss} ({', '.join(names)}) - total understated"
                            correct = f"=SUM({m.group(1)}{rows_idx[0]}:{m.group(1)}{rows_idx[-1]})"
                else:
                    sv = sum(_num(r[j].value) or 0 for r in labels.values() if j < len(r))
                    fv = _num(v)
                    if fv is not None and not _close(fv, sv):
                        issue = f"total ({group(fv, 2)}) is not the sum of the rows ({group(sv, 2)})"
                        correct = round(sv, 4)
                if issue:
                    findings.append({"item": f"TOTAL - {h}", "cell": f"{ws.title}!{tc.coordinate}", "found": v, "correct": correct, "issue": issue, "definition": ""})
                    oc = wb_out[ws.title][tc.coordinate]
                    oc.fill = RED
                    oc.comment = Comment(f"SAHAYAK-AI: {issue}", "SAHAYAK-AI")
            # members in the data but missing from the draft
            if j == num_cols[0]:
                res = x["res"]
                have = {k.lower() for k in labels}
                for mem, val in zip(res[d].astype(str), res[f"m{x['measures'].index(mi)}"] if f"m{x['measures'].index(mi)}" in res.columns else res["m0"]):
                    if mem.lower() not in have and pd.notna(val) and val != 0:
                        findings.append({"item": f"{mem} row", "cell": "-", "found": "missing", "correct": f"{group(float(val) * fac, 2)}", "issue": "row missing from the draft - left out of the summary",
                                         "definition": definition})
    wb_out.save(out_path)
    if checked == 0 and not findings:
        return result("audit", f"Could not check {name}", ["I could not find a summary table (a header row, a column of row names and number columns) in this file."], type="clarify")
    fdf = pd.DataFrame(findings)
    errs = [f for f in findings if f["cell"] != "-" or f["found"] == "missing"]
    ans = [f"Checked **{checked} cells** in {name} against the loaded data" + (f" ({used[1]})" if used else "") + f": **{len(errs)} problem(s)** found."]
    for i, f in enumerate(errs[:12], 1):
        fv = group(f["found"], _decs(f["found"])) if isinstance(f["found"], float) else f["found"]
        cv = group(f["correct"], min(_decs(f["correct"]), 2)) if isinstance(f["correct"], float) else f["correct"]
        ans.append(f"{i}. **{f['item']}** ({f['cell']}): draft {fv}, data {cv} - {f['issue']}.")
    if errs:
        ans.append("Wrong cells are highlighted red in the checked copy, with a comment giving the correct value.")
    else:
        ans.append("Every checked cell matches the data.")
    return result("audit", f"Check of {name}", ans,
                  kpis=[{"label": "Cells checked", "value": checked, "display": str(checked)}, {"label": "Problems", "value": len(errs), "display": str(len(errs)), "tone": "bad" if errs else "good"}],
                  table={"columns": ["Item", "Cell", "In draft", "Correct", "Likely cause", "How the correct value was computed"],
                         "rows": [[f["item"], f["cell"], f["found"], f["correct"], f["issue"], f["definition"]] for f in findings], "caption": "Problems found", "total_rows": len(findings)} if findings else None,
                  tables=[{"columns": list(cells[0].keys()), "rows": [list(c.values()) for c in cells], "caption": "Cell-by-cell comparison", "total_rows": len(cells)}] if cells else [],
                  files=[{"name": out_path.name, "label": "Checked copy (.xlsx)"}], sources=[{"file": name, "origin": "uploaded draft"}])


def _unit_factors(mi: dict, header: str) -> list[tuple[float, str]]:
    """The draft may show money in another unit than the data; try the unit named in the header too."""
    out = [(1.0, "")]
    u = mi.get("unit") or ""
    if mi.get("unit_kind") == "money" and u in MONEY and mi.get("agg") not in ("ratio", "mean"):
        h = header.lower()
        for word, target in (("crore", "Rs crore"), ("lakh", "Rs lakh"), ("thousand", "Rs thousand"), ("'000", "Rs thousand")):
            if word in h and target != u:
                out.insert(0, (MONEY[u] / MONEY[target], target))
    return out


def audit_latest(store) -> dict:
    if not store.drafts:
        return result("audit", "Upload the draft first", ["Upload the manual report (Excel) with **Check a manual report** on the Reports page, then ask again."], type="clarify")
    name, d = sorted(store.drafts.items(), key=lambda kv: kv[1].get("loaded_at", ""))[-1]
    return audit_file(store, Path(d["path"]), name)
