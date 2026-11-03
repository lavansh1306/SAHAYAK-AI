"""Stage 6 - Data dictionary & catalogue.

Built automatically from every loaded table (what each column is, how it adds up, units, time range, values), plus
links between tables and any edits the user makes on the Data page (labels, roles, synonyms, custom metrics).
Everything else in the app - questions, dashboards, insights, reports - reads only this dictionary."""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pandas as pd

from ..util import nice_label

KEEP_COL_FIELDS = ("name", "original", "label", "type", "role", "agg", "unit", "unit_kind", "scale", "ratio", "date_order", "excel_serial", "mismatch",
                   "higher_is_better")


def column_entry(spec: dict, s: pd.Series) -> dict:
    e = {k: spec.get(k) for k in KEEP_COL_FIELDS if spec.get(k) is not None}
    e.setdefault("label", nice_label(spec["name"]))
    nn = s.dropna()
    e["filled"] = int(len(nn))
    e["rows"] = int(len(s))
    if e.get("role") in ("dimension", "id") or e.get("type") in ("category", "boolean", "fy_label"):
        vc = nn.astype(str).value_counts()
        e["card"] = int(len(vc))
        e["values"] = vc.index[:300].tolist() if len(vc) <= 2000 else []
        e["top"] = [[k, int(v)] for k, v in vc.head(8).items()]
    if e.get("type") == "number" and len(nn):
        e.update(min=float(nn.min()), max=float(nn.max()), mean=float(nn.mean()))
        e["integer"] = bool(((nn - nn.round()).abs() < 1e-9).all())
    if e.get("type") in ("date", "datetime") and len(nn):
        e.update(min=str(pd.Timestamp(nn.min())), max=str(pd.Timestamp(nn.max())))
    if e.get("type") in ("month", "year") and len(nn):
        e.update(min=str(nn.min()), max=str(nn.max()))
    return e


def grain_of(df: pd.DataFrame, tcol: str | None, ttype: str | None) -> str:
    if not tcol or tcol not in df.columns:
        return "none"
    if ttype == "month":
        return "month"
    if ttype == "year":
        return "year"
    d = pd.to_datetime(df[tcol], errors="coerce").dropna().drop_duplicates().sort_values()
    if len(d) < 3:
        return "day"
    step = d.diff().median()
    if step <= pd.Timedelta(hours=1):
        return "hour" if step >= pd.Timedelta(minutes=30) else "minute"
    if step < pd.Timedelta(days=1):
        return "hour"
    if step <= pd.Timedelta(days=1):
        return "day"
    if step <= pd.Timedelta(days=8):
        return "week" if (d.dt.day == 1).mean() < 0.9 else "month"
    return "month"


def build_entry(key: str, label: str, df: pd.DataFrame, cols: dict[str, dict], tcol: str | None, keys: list[str], extra: dict) -> dict:
    data_cols = [c for c in df.columns if not c.startswith("_")]
    centries = {c: column_entry(cols.get(c, {"name": c, "type": "text", "role": "text"}), df[c]) for c in data_cols}
    ttype = centries.get(tcol, {}).get("type") if tcol else None
    e = {"key": key, "label": label, "rows": int(len(df)), "time": tcol, "time_type": ttype, "grain": grain_of(df, tcol, ttype), "keys": keys,
         "columns": centries}
    if tcol and tcol in df.columns and df[tcol].notna().any():
        e["time_range"] = [str(centries[tcol].get("min", ""))[:19], str(centries[tcol].get("max", ""))[:19]]
    if "_source" in df.columns:
        e["lineage"] = {str(k): int(v) for k, v in df["_source"].value_counts().items()}
    e.update(extra)
    return e


# ------------------------------------------------------------------------------------- overrides
def overrides_path(root: Path) -> Path:
    return Path(root) / "dictionary_overrides.json"


def load_overrides(root: Path) -> dict:
    try:
        return json.loads(overrides_path(root).read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_overrides(root: Path, o: dict) -> None:
    overrides_path(root).write_text(json.dumps(o, indent=1), encoding="utf-8")


EDITABLE = {"label", "role", "agg", "unit", "unit_kind", "synonyms", "hidden", "higher_is_better", "description"}


def merged_entry(auto: dict, ov: dict) -> dict:
    e = json.loads(json.dumps(auto))
    if not ov:
        return e
    for k in ("label", "description", "time"):
        if ov.get(k):
            e[k] = ov[k]
    for c, patch in (ov.get("columns") or {}).items():
        if c in e["columns"]:
            e["columns"][c].update({k: v for k, v in patch.items() if k in EDITABLE})
            if patch.get("agg") and patch["agg"] != "ratio":
                e["columns"][c].pop("ratio", None)
    if ov.get("metrics"):
        e["metrics"] = {**e.get("metrics", {}), **ov["metrics"]}
    return e


# ------------------------------------------------------------------------------------- metrics
AGG_FUNCS = {"sum", "avg", "mean", "count", "count_distinct", "min", "max", "last", "median"}


def parse_metric(expr: str, columns: dict) -> tuple[bool, str, list[tuple[str, str]]]:
    """Validate a custom metric like 'sum(paid_amount) / sum(billed_amount) * 100'. -> (ok, message, [(func, col)])."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        return False, f"Formula has a syntax error: {e.msg}", []
    used = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in AGG_FUNCS:
                return False, f"Only these functions are allowed: {', '.join(sorted(AGG_FUNCS))}", []
            if len(node.args) != 1 or not isinstance(node.args[0], ast.Name):
                return False, "Each function takes one column name, e.g. sum(amount)", []
            col = node.args[0].id
            if col not in columns and not (node.func.id == "count" and col in ("rows", "row", "records")):
                return False, f"Unknown column '{col}'", []
            used.append((node.func.id, col))
        elif isinstance(node, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Name, ast.Load, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd, ast.Constant)):
            if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
                return False, "Only numbers are allowed as constants", []
            if isinstance(node, ast.Name) and not any(node is c.args[0] or node is c.func for c in ast.walk(tree) if isinstance(c, ast.Call)):
                return False, f"'{node.id}' must be inside a function, e.g. sum({node.id})", []
        else:
            return False, f"'{type(node).__name__}' is not allowed in a formula", []
    if not used:
        return False, "The formula must use at least one column, e.g. sum(amount)", []
    return True, "ok", used


# ------------------------------------------------------------------------------------- links between tables
def find_links(tables: dict) -> list[dict]:
    """child.col -> parent.col when (almost) every child value exists in a parent column whose values are unique."""
    links = []
    cand = {}
    for k, t in tables.items():
        for c, e in t.entry["columns"].items():
            if e.get("type") in ("id", "category") and e.get("role") in ("id", "dimension") and not e.get("hidden"):
                cand.setdefault(k, []).append(c)
    for pk, pcols in cand.items():
        pdf = tables[pk].df
        pkeys = tables[pk].entry.get("keys") or []
        for pc in pcols:
            pv = pdf[pc].dropna().astype(str)
            if len(pv) == 0 or pv.duplicated().any():
                continue
            if pkeys and pkeys != [pc]:
                continue  # the parent column must identify the parent's rows
            pset = set(pv)
            for ck, ccols in cand.items():
                if ck == pk:
                    continue
                for cc in ccols:
                    singular = re.sub(r"(ies)$", "y", re.sub(r"(?<!s)s$", "", pk))
                    named_after = pc in ("id", "code", "key") and cc in (pk, singular, f"{singular}_id", f"{singular}_code")
                    if cc != pc and not named_after and not (cc.endswith(pc) or pc.endswith(cc) or re.sub(r"_?id$", "", cc) == re.sub(r"_?id$", "", pc)):
                        continue
                    cv = tables[ck].df[cc].dropna().astype(str).unique()
                    if len(cv) < 2:
                        continue
                    cover = sum(v in pset for v in cv) / len(cv)
                    if cover >= 0.9:
                        links.append({"child": ck, "child_col": cc, "parent": pk, "parent_col": pc, "coverage": round(cover, 3),
                                      "text": f"{tables[ck].label}.{cc} -> {tables[pk].label}.{pc} ({cover:.0%} of values found)"})
    return links


# ------------------------------------------------------------------------------------- importance & examples
def measure_rank(c: dict) -> float:
    s = 0.0
    k = c.get("unit_kind")
    s += {"money": 5, "volume": 4, "mass": 3, "percent": 2, "time": 1.5}.get(k, 1)
    s += {"sum": 2, "ratio": 1.5, "last": 1, "mean": 0.5}.get(c.get("agg"), 0)
    s += c.get("filled", 0) / max(c.get("rows", 1), 1)
    if re.search(r"(revenue|sales|amount|value|volume|sold|billed|collected|paid|quantity|qty|consumption|cost|profit|income)", c["name"]):
        s += 2
    if c.get("hidden") or c.get("role") != "measure":
        s -= 100
    return s


def dim_rank(c: dict) -> float:
    card = c.get("card", 0)
    if c.get("hidden") or c.get("role") != "dimension" or card < 2:
        return -100
    s = 3.0 if 2 <= card <= 15 else (1.5 if card <= 60 else 0.2)
    if re.search(r"(area|region|zone|segment|category|type|station|route|district|city|state|branch|product|plant|department)", c["name"]):
        s += 2
    if c.get("type") == "fy_label" or c["name"] in ("unit_reported",):
        s -= 5
    return s


def short(label: str) -> str:
    return re.sub(r"\s*\(.*?\)\s*", " ", label).strip().lower()
