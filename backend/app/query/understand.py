"""Turn a question into a query PLAN using only the data dictionary (no fixed tables or column names).

1. Rules: match words to columns / tables / values in the dictionary, plus time, grouping, ranking, comparison words.
2. If the rules are not confident and the LLM is available, the LLM writes the plan (JSON) from the dictionary.
3. Every plan is validated against the dictionary (columns exist, values exist, sums of percentages forbidden).
The plan is then executed by code (engine.py) - the LLM never produces numbers."""
from __future__ import annotations

import re
from datetime import date
from functools import lru_cache
from pathlib import Path

import yaml
from rapidfuzz import fuzz, process

from ..pipeline.dictionary import short
from .periods import MON_RE, Period, add_months, describe, fy_period, fy_start_year, month_long, parse_fy, parse_period, today, ym

STOP = set("""a an the of in on at to for from by with and or is are was were be been it its this that these those as what which who whom how why
when where do does did our we you your i me my there their they them than then so not any can could should would will shall may might into
about over under up down out off vs give show tell list display please kindly get find me us all much many number numbers data
much figure figures details detail kya hai please also just only what's whats let lets see""".split())
UNIT_TOK = {"rs", "inr", "lakh", "lac", "crore", "cr", "scm", "mmscm", "kg", "km", "pct", "percent", "percentage", "hours", "hrs", "minutes",
            "mins", "min", "days", "bar", "scmh", "kwh", "mt", "thousand", "per", "no", "c"}
GROUP_WORDS = r"(?:by|per|each|every|across|for each|for every|in each|wise|-wise|split by|broken down by|breakdown by|grouped by)"
GRAIN_WORDS = [("hour", r"\b(hourly|per hour|by hour|each hour|hour[- ]?wise|hour by hour)\b"), ("day", r"\b(daily|per day|by day|each day|day[- ]?wise|day by day|days)\b"),
               ("week", r"\b(weekly|per week|by week|each week|week[- ]?wise)\b"), ("month", r"\b(monthly|per month|by month|each month|month[- ]?wise|month by month|every month|months|mahine)\b"),
               ("quarter", r"\b(quarterly|per quarter|by quarter|each quarter|quarter[- ]?wise|quarters)\b"),
               ("fy", r"\b(by (financial|fiscal) year|fy[- ]?wise|each (financial|fiscal) year|financial years|per fy|by fy)\b"),
               ("year", r"\b(yearly|annual|annually|per year|by year|each year|year[- ]?wise|year by year|years)\b")]


@lru_cache(maxsize=1)
def vocab() -> dict:
    try:
        return yaml.safe_load((Path(__file__).resolve().parents[1] / "vocabulary.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def stem(t: str) -> str:
    if len(t) > 4 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 4 and t.endswith("es") and t[-3] in "sxz":
        return t[:-2]
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


def normalise(q: str) -> str:
    s = " " + q.lower().replace("’", "'").replace("–", "-") + " "
    for k, v in sorted((vocab().get("hinglish") or {}).items(), key=lambda kv: -len(kv[0])):
        s = re.sub(rf"(?<![a-z]){re.escape(k)}(?![a-z])", f" {v} " if v else " ", s)
    s = re.sub(r"(\w)-wise\b", r"\1 wise", s)
    return re.sub(r"\s+", " ", s).strip()


def words(s: str) -> list[str]:
    return [stem(t) for t in re.findall(r"[a-z0-9%]+", s.lower()) if t not in STOP]


def expand(ws: list[str]) -> set[str]:
    """Add vocabulary canonical words for synonyms found in the text."""
    out = set(ws)
    text = " " + " ".join(ws) + " "
    for canon, syns in (vocab().get("synonyms") or {}).items():
        for sy in [canon] + list(syns or []):
            sw = " ".join(stem(x) for x in re.findall(r"[a-z0-9%]+", str(sy).lower()))
            if sw and f" {sw} " in text:
                out.add(stem(canon))
                out |= {stem(x) for x in str(canon).split()}
    return out


# ------------------------------------------------------------------------------------- column matching
def col_words(t, c: str) -> tuple[set[str], set[str]]:
    e = t.cols[c]
    raw = re.findall(r"[a-z0-9%]+", (c.replace("_", " ") + " " + e.get("label", "") + " " + " ".join(e.get("synonyms") or [])).lower())
    ws = {stem(x) for x in raw if x not in STOP}
    unit = {w for w in ws if w in UNIT_TOK or w == "%"}
    core = ws - unit - {"id", "code", "total", "amount_", "no"}
    if not core:
        core = ws - {"id"}
    return core, unit


def _match(qw: set[str], w: str) -> bool:
    if w in qw:
        return True
    if len(w) >= 5:
        return any(len(x) >= 5 and fuzz.ratio(w, x) >= 88 for x in qw)
    return False


def score_col(qw: set[str], t, c: str, qtext: str) -> float:
    core, unit = col_words(t, c)
    e = t.cols.get(c, {})
    phrase_hit = 0.0
    for ph in list(e.get("synonyms") or []) + [re.sub(r"\s*\(.*?\)", "", e.get("label", ""))]:
        pw = {stem(x) for x in re.findall(r"[a-z0-9]+", str(ph).lower()) if x not in STOP}
        if pw and pw <= qw:
            phrase_hit = max(phrase_hit, 2.0 + 0.3 * len(pw))
    m = [w for w in core if _match(qw, w)]
    if not m and not phrase_hit:
        return 0.0
    s = max(2.0 * len(m) / max(len(core), 1) + 0.4 * len(m), phrase_hit)
    uq = {w for w in qw if w in UNIT_TOK} | ({"pct"} if re.search(r"%|percent|percentage|\bpct\b|\brate\b", qtext) else set())
    if uq & unit:
        s += 0.6
    if ("pct" in unit) and not (uq & {"pct", "percent", "percentage"}):
        s -= 0.3
    return s


def table_words(t) -> set[str]:
    return {stem(x) for x in re.findall(r"[a-z0-9]+", (t.key + " " + t.label).lower()) if x not in STOP}


# ------------------------------------------------------------------------------------- values in the question
def find_values(qn: str, store, tables: list | None = None) -> list[tuple[str, str, str, str]]:
    """-> [(table, column, value, matched_text)] longest matches first, no overlaps."""
    vals = store.catalog.get("values", {})
    toks = re.findall(r"[a-z0-9]+", qn)
    used = [False] * len(toks)
    out = []
    keys = None
    for n in (5, 4, 3, 2, 1):
        for i in range(0, len(toks) - n + 1):
            if any(used[i:i + n]):
                continue
            g = " ".join(toks[i:i + n])
            if g in STOP or len(g) < 2 or re.fullmatch(r"\d+", g) or re.fullmatch(MON_RE.replace(r"\b", ""), g):
                continue
            hits = vals.get(g)
            if not hits and n <= 3 and len(g) >= 5:
                keys = keys or list(vals.keys())
                m = process.extractOne(g, keys, scorer=fuzz.ratio, score_cutoff=92)
                if m:
                    hits = vals.get(m[0])
            if hits:
                hits = [h for h in hits if tables is None or h[0] in tables]
                if not hits:
                    continue
                for h in hits:
                    out.append((h[0], h[1], h[2], g))
                for k in range(i, i + n):
                    used[k] = True
    return out


# ------------------------------------------------------------------------------------- day-level periods
DAY_RE = r"(\d{1,2})(?:st|nd|rd|th)?[\s\-/]*(" + MON_RE[3:-3] + r")[\s\-/,']*(\d{2,4})?"


def parse_days(text: str, t: date) -> dict | None:
    s = text.lower()
    from .periods import mnum

    def mk(dd, mm, yy):
        y = int(yy) if yy else t.year
        y = y + 2000 if y < 100 else y
        try:
            d = date(y, mnum(mm), int(dd))
        except ValueError:
            return None
        if not yy and d > t:
            d = date(y - 1, d.month, d.day)
        return d
    rng = re.search(r"(?:between|from)\s+" + DAY_RE + r"\s+(?:and|to|till|until|-)\s+" + DAY_RE, s)
    if rng:
        a = mk(rng.group(1), rng.group(2), rng.group(3) or rng.group(6))
        b = mk(rng.group(4), rng.group(5), rng.group(6))
        if a and b:
            return {"d0": a.isoformat(), "d1": b.isoformat(), "label": f"{a:%d-%b-%Y} to {b:%d-%b-%Y}"}
    iso = re.findall(r"\b(20\d{2})-(\d{2})-(\d{2})\b", s)
    if len(iso) >= 2:
        a, b = date(*map(int, iso[0])), date(*map(int, iso[1]))
        return {"d0": a.isoformat(), "d1": b.isoformat(), "label": f"{a:%d-%b-%Y} to {b:%d-%b-%Y}"}
    if len(iso) == 1:
        a = date(*map(int, iso[0]))
        return {"d0": a.isoformat(), "d1": a.isoformat(), "label": f"{a:%d-%b-%Y}"}
    one = re.search(r"\bon\s+" + DAY_RE, s) or re.search(r"\b" + DAY_RE + r"\b", s)
    if one and one.group(1):
        a = mk(one.group(1), one.group(2), one.group(3))
        if a:
            return {"d0": a.isoformat(), "d1": a.isoformat(), "label": f"{a:%d-%b-%Y}"}
    m = re.search(r"\blast\s+(\d{1,3})\s+days\b", s)
    if m:
        from datetime import timedelta
        a = t - timedelta(days=int(m.group(1)) - 1)
        return {"d0": a.isoformat(), "d1": t.isoformat(), "label": f"last {m.group(1)} days", "relative": True}
    if re.search(r"\b(last|previous|pichle|pichhle)\s+week\b", s):
        from datetime import timedelta
        start = t - timedelta(days=t.weekday() + 7)
        return {"d0": start.isoformat(), "d1": (start + timedelta(days=6)).isoformat(), "label": "last week", "relative": True}
    if re.search(r"\byesterday\b|\bkal\b", s):
        from datetime import timedelta
        a = t - timedelta(days=1)
        return {"d0": a.isoformat(), "d1": a.isoformat(), "label": f"{a:%d-%b-%Y}", "relative": True}
    if re.search(r"\btoday\b|\baaj\b", s):
        return {"d0": t.isoformat(), "d1": t.isoformat(), "label": f"{t:%d-%b-%Y}", "relative": True}
    return None


# ------------------------------------------------------------------------------------- rules parser
COUNT_RE = re.compile(r"\b(how many|number of|no\.? of|count of|count|kitne|total number)\b")
AVG_RE = re.compile(r"\b(average|avg|mean|typical)\b")
SUM_RE = re.compile(r"\b(total|sum|overall|cumulative|altogether|combined|kul)\b")
MAX_RE = re.compile(r"\b(highest|most|maximum|max|peak|largest|biggest|top|best|greatest|leading|record)\b")
MIN_RE = re.compile(r"\b(lowest|least|minimum|min|smallest|bottom|fewest|weakest)\b")
WORST_RE = re.compile(r"\b(worst|poorest)\b")
BEST_RE = re.compile(r"\b(best|strongest)\b")
TREND_RE = re.compile(r"\b(trend|over time|time series|history|historical|progress|movement|how has .* (changed|moved)|growth path)\b")
SHARE_RE = re.compile(r"\b(share|mix|contribution|proportion|distribution|split|percentage of total|% of total|breakup|break-up)\b")
CMP_YEAR_RE = re.compile(r"\b(vs\.?|versus|compared? (to|with)|against|than)\s+(the\s+)?(same (period|month|time) )?(last|previous|prior|pichle|pichhle) (year|fy|financial year)\b|\byoy\b|year[- ]on[- ]year|\bgrowth\b|\bgrew\b|\bincrease since last year\b")
CMP_PERIOD_RE = re.compile(r"\b(vs\.?|versus|compared? (to|with)|against|than)\s+(the\s+)?(last|previous|prior) (month|quarter|week|period|day)\b|\bmom\b|month[- ]on[- ]month|\bqoq\b")
CHANGE_RE = re.compile(r"\b(change|changed|difference|increase|decrease|rise|fall|drop|grow|growth|improve|worsen|up or down)\b")
ROWS_RE = re.compile(r"^\s*(list|show me all|show all|display all|which are the|give me the list of|list out|show the list of|show)\b")
COND_RE = re.compile(r"\b(above|over|more than|greater than|exceeding|exceeds|exceeded|crossed|crosses|higher than|at least|>=|>|below|under|less than|lower than|at most|<=|<)\s*(rs\.?\s*)?(-?[\d,]*\.?\d+)\s*(%|percent|lakh|crore|cr|k)?")
TOPN_RE = re.compile(r"\b(top|bottom|best|worst|highest|lowest|first|last)\s+(\d{1,3})\b")
CHART_RE = re.compile(r"\b(pie|donut|doughnut|bar|column|line|table|tabular)\b")


def _grain(qn: str) -> str | None:
    for g, rx in GRAIN_WORDS:
        if re.search(rx, qn):
            return g
    return None


def bad_direction(t, c: str) -> bool:
    e = t.cols.get(c, {})
    if e.get("higher_is_better") is not None:
        return not e["higher_is_better"]
    ws = set(re.findall(r"[a-z]+", (c + " " + t.key).lower().replace("_", " ")))
    return bool(ws & set(vocab().get("higher_is_bad") or []))


def rules_plan(q: str, store, ctx: dict | None = None) -> dict:
    """Best-effort plan + confidence + what was understood (for the user)."""
    qn = normalise(q)
    qws = words(qn)
    qw = expand(qws)
    tables = store.usable()
    vals = find_values(qn, store)
    out = {"confidence": 0.0, "understood": [], "candidates": []}
    if not tables:
        return out
    count_intent = bool(COUNT_RE.search(qn))
    # --- score tables / measures
    best = []
    for t in tables:
        tw = table_words(t)
        tbonus = 0.8 if (tw & qw) else 0.0
        vbonus = 1.2 * len({(c, v) for tk, c, v, _ in vals if tk == t.key})
        ms = [(score_col(qw, t, c, qn), c) for c in t.measures()]
        ms = sorted([x for x in ms if x[0] > 0], reverse=True)
        dmatch = sum(1 for c in t.dims() + ([t.time] if t.time else []) if score_col(qw, t, c, qn) > 0)
        top = ms[0][0] if ms else 0.0
        score = top + tbonus + vbonus + 0.3 * dmatch
        if count_intent and not ms and tbonus:
            score += 1.5
        best.append({"t": t, "score": score, "ms": ms, "tbonus": tbonus, "vbonus": vbonus})
    best.sort(key=lambda x: -x["score"])
    out["candidates"] = [{"table": b["t"].key, "label": b["t"].label, "score": round(b["score"], 2)} for b in best[:4]]
    b0 = best[0]
    if b0["score"] <= 0:
        return out
    t = b0["t"]
    margin = b0["score"] - (best[1]["score"] if len(best) > 1 else 0)
    # --- measures
    measures = []
    if b0["ms"]:
        top = b0["ms"][0][0]
        chosen = [c for s, c in b0["ms"] if s >= max(1.2, top - 0.2)][:3]
        for c in chosen:
            measures.append({"col": c})
    how_many_tokens = re.search(r"\b(how many|number of|kitne|count of|count)\s+([a-z ]{2,40}?)(?=\s+(?:by|in|for|per|with|where|that|which|this|last|during|from|are|were|is|was|did|do|have|has|had|on|at|across|each|wise|between|since)\b|[?.,]|$)", qn)
    if count_intent and (not measures or (how_many_tokens and not any(score_col(set(words(how_many_tokens.group(2))), t, m["col"], how_many_tokens.group(2)) > 1.5 for m in measures))):
        target = None
        if how_many_tokens:
            hw = expand(words(how_many_tokens.group(2)))
            for c in t.ids() + t.dims():
                core, _ = col_words(t, c)
                if core & hw and c not in [v[1] for v in vals if v[0] == t.key]:
                    target = c
                    break
            if target and (table_words(t) & hw) and t.entry.get("keys") == [target]:
                target = None
        measures = [{"col": target, "agg": "count_distinct"}] if target else [{"col": "*", "agg": "count"}]
    if not measures:
        return out
    plan = {"table": t.key, "measures": measures, "group": [], "grain": None, "filters": [], "having": [], "period": None, "compare": None,
            "sort": None, "limit": None, "share": False, "chart": "auto", "mode": "aggregate"}
    linked_dims = _linked_dims(store, t)
    # --- filters from values mentioned
    fby: dict = {}
    own = {g for tk, c, v, g in vals if tk == t.key}
    for tk, c, v, g in vals:
        if tk == t.key or ((tk, c) in linked_dims and g not in own):
            fby.setdefault(c, set()).add(v)
    for c, vs in fby.items():
        plan["filters"].append({"col": c, "op": "in", "values": sorted(vs)})
    # --- grouping
    grain = _grain(qn)
    group = []
    dim_choices = t.dims() + [c for (_, c) in linked_dims]
    for m in re.finditer(rf"\b(?:which|what|top \d+|bottom \d+|{GROUP_WORDS})\s+([a-z0-9]+(?: [a-z0-9]+)?)", qn):
        phrase = m.group(1)
        c = _resolve_dim(t, phrase, dim_choices, store)
        if c and c not in group and c not in fby:
            group.append(c)
    for m in re.finditer(r"\b([a-z0-9]+(?: [a-z0-9]+)?) wise\b", qn):
        c = _resolve_dim(t, m.group(1), dim_choices, store)
        if c and c not in group:
            group.append(c)
    for m in re.finditer(r"\b([a-z0-9]+) (?:with|having|that had|that has|had|has) the (?:highest|lowest|most|least|best|worst|maximum|minimum)", qn):
        c = _resolve_dim(t, m.group(1), dim_choices, store)
        if c and c not in group:
            group.append(c)
    if re.search(r"\b(which|what) (month|day|date|week|quarter|year|hour)\b", qn) and t.time:
        grain = grain or re.search(r"\b(which|what) (month|day|date|week|quarter|year|hour)\b", qn).group(2).replace("date", "day")
    if TREND_RE.search(qn) and t.time and not grain:
        grain = "month" if t.entry.get("grain") in ("month", "day", "hour", "week", "minute") else "year"
    if grain and t.time:
        plan["grain"] = grain
    group = [c for c in group if not (c in t.cols and t.cols[c].get("type") == "fy_label" and t.time)]
    plan["group"] = group[:2]
    # --- period
    td = today()
    qp = CMP_YEAR_RE.sub(" ", qn) if CMP_YEAR_RE.search(qn) else CMP_PERIOD_RE.sub(" ", qn)
    qp = re.sub(r"\b(compared?|vs\.?|versus|against)\b.*\b(last|previous|prior) (year|fy|month|quarter|week)\b", " ", qp)
    days = parse_days(qp, td) if t.time_type in ("date", "datetime") else None
    per = None if days else parse_period(qp, td)
    if days:
        plan["period"] = {"start": days["d0"][:7], "end": days["d1"][:7], "d0": days["d0"], "d1": days["d1"], "label": days["label"], "kind": "days", "relative": days.get("relative", False)}
    elif per:
        plan["period"] = {"start": per.start, "end": per.end, "label": per.label, "kind": per.kind, "relative": per.relative}
    # --- comparison
    if CMP_PERIOD_RE.search(qn):
        plan["compare"] = "previous_period"
    elif CMP_YEAR_RE.search(qn) or (CHANGE_RE.search(qn) and re.search(r"last year|previous year|a year (ago|earlier)", qn)):
        plan["compare"] = "previous_year"
    # --- aggregation words
    for m in plan["measures"]:
        e = t.cols.get(m["col"], {})
        if m.get("agg"):
            continue
        if AVG_RE.search(qn) and e.get("agg") == "sum":
            g = grain or next((gg for gg, rx in GRAIN_WORDS if re.search(rx.replace("|days", "").replace("|months", "").replace("|years", ""), qn)), None)
            if re.search(r"\b(per|a|each|every)\s+(day|month|week|year|hour)\b|\b(daily|monthly|weekly|yearly|hourly)\b", qn) and not plan["grain"]:
                m["agg"] = "avg_per"
                m["grain"] = re.search(r"\b(?:per|a|each|every)\s+(day|month|week|year|hour)\b", qn).group(1) if re.search(r"\b(?:per|a|each|every)\s+(day|month|week|year|hour)\b", qn) else \
                    {"daily": "day", "monthly": "month", "weekly": "week", "yearly": "year", "hourly": "hour"}[re.search(r"\b(daily|monthly|weekly|yearly|hourly)\b", qn).group(1)]
            else:
                m["agg"] = "mean"
        elif re.search(r"\bmedian\b", qn) and e.get("role") == "measure":
            m["agg"] = "median"
        elif not group and not grain and e.get("role") == "measure":
            if re.search(r"\b(maximum|max|peak|highest|largest|biggest)\b", qn) and not re.search(r"\bwhich\b|\bwhen\b", qn):
                m["agg"] = "max"
            elif re.search(r"\b(minimum|min|lowest|smallest)\b", qn) and not re.search(r"\bwhich\b|\bwhen\b", qn):
                m["agg"] = "min"
    if re.search(r"\b(when|which (day|month|date|week|year|hour))\b", qn) and t.time and not group and (MAX_RE.search(qn) or MIN_RE.search(qn)) and not plan["grain"]:
        plan["grain"] = "day" if t.entry.get("grain") in ("day", "hour", "minute") else "month"
    # --- ranking
    mcol = plan["measures"][0]["col"]
    bad = bad_direction(t, mcol) if mcol and mcol != "*" else False
    tn = TOPN_RE.search(qn)
    if tn:
        w = tn.group(1)
        plan["limit"] = int(tn.group(2))
        plan["sort"] = "asc" if w in ("bottom", "lowest") or (w == "worst" and not bad) or (w == "best" and bad) else "desc"
    elif plan["group"] or (plan["grain"] and re.search(r"\b(which|when)\b", qn)):
        if MIN_RE.search(qn) or (WORST_RE.search(qn) and not bad) or (BEST_RE.search(qn) and bad):
            plan["sort"] = "asc"
        elif MAX_RE.search(qn) or WORST_RE.search(qn) or BEST_RE.search(qn) or plan["group"]:
            plan["sort"] = "desc"
    # --- conditions (having / row filters)
    for cm in COND_RE.finditer(qn):
        op_w, num, suf = cm.group(1), float(cm.group(3).replace(",", "")), cm.group(4)
        if plan["period"] and re.search(rf"\b{re.escape(cm.group(3))}\b", plan["period"].get("label", "")):
            continue
        op = ">" if op_w in ("above", "over", "more than", "greater than", "exceeding", "exceeds", "exceeded", "crossed", "crosses", "higher than", ">") else \
             ">=" if op_w in ("at least", ">=") else "<=" if op_w in ("at most", "<=") else "<"
        before = qn[:cm.start()]
        target = None
        for c in t.measures():
            if score_col(expand(words(before[-60:])), t, c, before[-60:]) > 1.0:
                target = c
        target = target or (mcol if mcol != "*" else None)
        if not target:
            continue
        if plan["group"] or plan["grain"]:
            plan["having"].append({"col": target, "op": op, "value": num})
        else:
            plan["filters"].append({"col": target, "op": op, "values": [num]})
    # --- share, chart, rows
    if SHARE_RE.search(qn) and (plan["group"] or True):
        plan["share"] = True
        if not plan["group"] and t.dims():
            plan["group"] = [t.dims()[0]]
    cm = CHART_RE.search(qn)
    if cm:
        plan["chart"] = {"donut": "pie", "doughnut": "pie", "column": "bar", "tabular": "table"}.get(cm.group(1), cm.group(1))
        if plan["chart"] == "pie":
            plan["share"] = True
            if not plan["group"] and t.dims():
                plan["group"] = [t.dims()[0]]
    if ROWS_RE.search(qn) and not (SUM_RE.search(qn) or AVG_RE.search(qn) or plan["group"] or plan["grain"] or COUNT_RE.search(qn) or TREND_RE.search(qn)):
        plan["mode"] = "rows"
    # --- confidence
    known = set()
    for m in plan["measures"]:
        if m["col"] and m["col"] != "*":
            known |= col_words(t, m["col"])[0] | col_words(t, m["col"])[1]
    for c in plan["group"]:
        if c in t.cols:
            known |= col_words(t, c)[0]
    for _, _, v, g in vals:
        known |= set(words(g))
    known |= table_words(t)
    filler = set(words("average total sum trend month monthly year last previous compare share pie bar line chart highest lowest top bottom which how many wise "
                        "change growth vs versus show list each per day daily week quarter fy financial fiscal current this since till between from to "
                        "most least best worst maximum minimum count number above below over under more less than percent percentage did happen there "
                        "breakdown split distribution mix contribution graph plot table weekly yearly quarterly hourly time over all record"))
    unknown = [w for w in qws if w not in known and w not in filler and not re.fullmatch(r"\d+|%|" + MON_RE.replace(r"\b", ""), w) and stem(w) not in known
               and not any(_match({w}, k) for k in known)]
    period_words = set(words(plan["period"]["label"])) if plan["period"] else set()
    unknown = [w for w in unknown if w not in period_words]
    conf = 0.55 + min(0.3, 0.15 * (b0["ms"][0][0] if b0["ms"] else 1.0)) + (0.1 if margin > 0.8 else 0.0) - 0.12 * len(unknown)
    out.update(plan=plan, confidence=round(max(0.0, min(1.0, conf)), 2), unknown=unknown, table=t.key, margin=round(margin, 2))
    return out


def _linked_dims(store, t) -> list[tuple[str, str]]:
    """(parent_table, column) pairs reachable through links (1-2 hops)."""
    out = []
    frontier = [t.key]
    seen = {t.key}
    for _ in range(2):
        nxt = []
        for k in frontier:
            for l in store.links:
                if l["child"] == k and l["parent"] not in seen:
                    p = store.tables.get(l["parent"])
                    if p:
                        out += [(p.key, c) for c in p.dims() if c not in t.cols]
                        nxt.append(p.key)
                        seen.add(p.key)
        frontier = nxt
    return out


def _resolve_dim(t, phrase: str, choices: list[str], store) -> str | None:
    pw = expand(words(phrase))
    if not pw:
        return None
    best, bs = None, 0.0
    if re.search(r"\b(fy|financial|fiscal)\b", phrase):
        choices = [c for c in choices if not (c in t.cols and t.cols[c].get("type") == "fy_label" and t.time)]
    for c in choices:
        tt = t if c in t.cols else next((store.tables[k] for k in store.tables if c in store.tables[k].cols and store.tables[k].cols[c].get("role") == "dimension"), None)
        if not tt:
            continue
        core, _ = col_words(tt, c)
        hit = [w for w in core if _match(pw, w)]
        if hit:
            s = len(hit) / max(len(core), 1) + (0.5 if phrase.split()[0].startswith(next(iter(hit))[:4]) else 0)
            if s > bs:
                best, bs = c, s
    return best


# ------------------------------------------------------------------------------------- LLM planner
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "table": {"type": "string"},
        "measures": {"type": "array", "items": {"type": "object", "properties": {"column": {"type": "string"}, "agg": {"type": "string"}}, "required": ["column", "agg"], "additionalProperties": False}},
        "group_by": {"type": "array", "items": {"type": "string"}},
        "time_grain": {"type": "string"},
        "filters": {"type": "array", "items": {"type": "object", "properties": {"column": {"type": "string"}, "op": {"type": "string"}, "values": {"type": "array", "items": {"type": ["string", "number"]}}},
                                                 "required": ["column", "op", "values"], "additionalProperties": False}},
        "period_start": {"type": "string"}, "period_end": {"type": "string"},
        "compare": {"type": "string"}, "sort": {"type": "string"}, "limit": {"type": "integer"}, "chart": {"type": "string"}, "share": {"type": "boolean"},
        "mode": {"type": "string"}, "clarify": {"type": "string"},
    },
    "required": ["table", "measures", "group_by", "time_grain", "filters", "period_start", "period_end", "compare", "sort", "limit", "chart", "share", "mode", "clarify"],
    "additionalProperties": False,
}


def dictionary_text(store, keys: list[str] | None = None, max_tables: int = 6) -> str:
    lines = []
    ts = [store.tables[k] for k in (keys or []) if k in store.tables] or store.usable()
    for t in ts[:max_tables]:
        tr = t.entry.get("time_range") or ["", ""]
        lines.append(f"TABLE {t.key} -- {t.label}; {len(t.df)} rows" + (f"; time column {t.time} ({t.time_type}) from {tr[0][:10]} to {tr[1][:10]}" if t.time else ""))
        for c, e in t.cols.items():
            if e.get("hidden") or e.get("role") == "ignore":
                continue
            d = f"  {c} [{e.get('role')}, {e.get('type')}" + (f", adds up by {e.get('agg')}" if e.get("agg") else "") + (f", unit {e.get('unit')}" if e.get("unit") else "") + "]"
            if e.get("label") and e["label"].lower() != c.replace("_", " "):
                d += f" label '{e['label']}'"
            if e.get("synonyms"):
                d += f" also called {', '.join(e['synonyms'])}"
            if e.get("role") == "dimension" and e.get("values"):
                d += f" values: {', '.join(map(str, e['values'][:12]))}" + (" ..." if len(e["values"]) > 12 else "")
            lines.append(d)
    for l in store.links[:10]:
        lines.append(f"LINK {l['child']}.{l['child_col']} -> {l['parent']}.{l['parent_col']}")
    return "\n".join(lines)


def llm_plan(q: str, store, cand_tables: list[str], prev: dict | None = None) -> dict | None:
    from .. import llm
    if not llm.available():
        return None
    td = today()
    sys_msg = ("You convert a business question into a JSON query plan over the tables below. Use ONLY listed tables and columns. "
               "agg is one of sum, mean, count, count_distinct, min, max, median. Use column '*' with agg 'count' to count rows. "
               "Never sum percentages, rates or prices: use mean for them. group_by lists dimension columns. time_grain is none, hour, day, week, month, quarter, year or fy "
               f"(fy = financial year starting in month {fy_start_month()}). period_start / period_end are 'YYYY-MM' or 'YYYY-MM-DD' or ''. "
               f"Today is {td.isoformat()}. compare is none, previous_period or previous_year. sort is desc, asc or none. limit 0 means no limit. "
               "chart is auto, bar, line, pie or table. mode is aggregate or rows (rows = list matching records). Filter values must be copied exactly from the listed values. "
               "If the question cannot be answered from these tables, set clarify to a short question for the user and table to ''.\n\n" + dictionary_text(store, cand_tables))
    msgs = [{"role": "system", "content": sys_msg}]
    if prev:
        msgs.append({"role": "user", "content": f"Previous question: {prev.get('question', '')}"})
    msgs.append({"role": "user", "content": q})
    obj = llm.chat_json(msgs, PLAN_SCHEMA, purpose="query_plan", max_tokens=600)
    return obj


def fy_start_month() -> int:
    from ..config import fy_start
    return fy_start()


def from_llm(obj: dict, store) -> tuple[dict | None, str]:
    """LLM JSON -> internal plan, validated. Returns (plan, error)."""
    if not obj:
        return None, "no plan"
    if obj.get("clarify") and not obj.get("table"):
        return None, "clarify:" + obj["clarify"]
    t = store.get(obj.get("table", ""))
    if not t:
        return None, f"unknown table {obj.get('table')}"
    linked = {c for _, c in _linked_dims(store, t)}
    allc = set(t.cols) | linked
    ms = []
    for m in obj.get("measures") or []:
        c, a = m.get("column"), (m.get("agg") or "").lower()
        if c in ("*", "", None) and a == "count":
            ms.append({"col": "*", "agg": "count"})
            continue
        if c not in t.cols:
            return None, f"unknown column {c}"
        e = t.cols[c]
        if a in ("sum", "total") and e.get("agg") in ("mean", "ratio"):
            a = e["agg"]
        if a in ("mean", "avg", "average") and e.get("agg") == "ratio":
            a = "ratio"
        if a not in ("sum", "mean", "count", "count_distinct", "min", "max", "median", "ratio", "last"):
            a = e.get("agg") or "sum"
        if e.get("role") != "measure" and a not in ("count", "count_distinct"):
            a = "count_distinct"
        ms.append({"col": c, "agg": a})
    if not ms:
        return None, "no measure"
    group = [g for g in (obj.get("group_by") or []) if g in allc and g != t.time][:2]
    filters = []
    for f in obj.get("filters") or []:
        c = f.get("column")
        if c not in allc:
            return None, f"unknown filter column {c}"
        op = {"=": "in", "==": "in", "eq": "in", "in": "in", "!=": "not_in", "not_in": "not_in", ">": ">", "gt": ">", ">=": ">=", "gte": ">=", "<": "<", "lt": "<",
              "<=": "<=", "lte": "<=", "contains": "contains", "between": "between"}.get(str(f.get("op", "in")).lower(), "in")
        filters.append({"col": c, "op": op, "values": f.get("values") or []})
    grain = obj.get("time_grain") if obj.get("time_grain") in ("hour", "day", "week", "month", "quarter", "year", "fy") else None
    period = None
    ps, pe = (obj.get("period_start") or "").strip(), (obj.get("period_end") or "").strip()
    if ps or pe:
        ps = ps or pe
        pe = pe or ps
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", ps) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", pe):
            period = {"start": ps[:7], "end": pe[:7], "d0": ps, "d1": pe, "label": f"{ps} to {pe}" if ps != pe else ps, "kind": "days"}
        elif re.fullmatch(r"\d{4}-\d{2}", ps[:7]) and re.fullmatch(r"\d{4}-\d{2}", pe[:7]):
            period = {"start": ps[:7], "end": pe[:7], "label": describe(ps[:7], pe[:7]), "kind": "range"}
    plan = {"table": t.key, "measures": ms, "group": group, "grain": grain if t.time else None, "filters": filters, "having": [], "period": period,
            "compare": obj.get("compare") if obj.get("compare") in ("previous_period", "previous_year") else None,
            "sort": obj.get("sort") if obj.get("sort") in ("asc", "desc") else None, "limit": int(obj["limit"]) if obj.get("limit") else None,
            "chart": obj.get("chart") if obj.get("chart") in ("bar", "line", "pie", "table") else "auto", "share": bool(obj.get("share")),
            "mode": "rows" if obj.get("mode") == "rows" else "aggregate"}
    return plan, ""


# ------------------------------------------------------------------------------------- follow-ups
FOLLOW_RE = re.compile(r"^\s*(and|what about|how about|same for|same but|now|only|just|also|instead|for|in|by|as a|as|show it|show that|make it|break (it|that) down|split (it|that))\b")


def followup(q: str, prev_plan: dict, store) -> dict | None:
    """Short follow-up questions change only part of the previous plan."""
    qn = normalise(q)
    if len(qn.split()) > 12 or not prev_plan:
        return None
    t = store.get(prev_plan["table"])
    if not t:
        return None
    looks = FOLLOW_RE.search(qn) or len(qn.split()) <= 5
    if not looks:
        return None
    p = {**prev_plan, "filters": list(prev_plan.get("filters", [])), "group": list(prev_plan.get("group", [])), "having": []}
    changed = False
    vals = [v for v in find_values(qn, store) if v[0] == t.key or (v[0], v[1]) in _linked_dims(store, t)]
    if vals:
        by = {}
        for _, c, v, _ in vals:
            by.setdefault(c, set()).add(v)
        p["filters"] = [f for f in p["filters"] if f["col"] not in by] + [{"col": c, "op": "in", "values": sorted(v)} for c, v in by.items()]
        changed = True
    per = parse_period(qn, today())
    days = parse_days(qn, today()) if t.time_type in ("date", "datetime") else None
    if days:
        p["period"] = {"start": days["d0"][:7], "end": days["d1"][:7], "d0": days["d0"], "d1": days["d1"], "label": days["label"], "kind": "days"}
        changed = True
    elif per:
        p["period"] = {"start": per.start, "end": per.end, "label": per.label, "kind": per.kind, "relative": per.relative}
        changed = True
    g = _grain(qn)
    if g and t.time:
        p["grain"] = g
        changed = True
    for m in re.finditer(rf"\b(?:{GROUP_WORDS}|which)\s+([a-z0-9 ]{{2,25}})", qn):
        c = _resolve_dim(t, m.group(1), t.dims() + [c for _, c in _linked_dims(store, t)], store)
        if c:
            p["group"] = [c]
            changed = True
    cm = CHART_RE.search(qn)
    if cm:
        p["chart"] = {"donut": "pie", "doughnut": "pie", "column": "bar", "tabular": "table"}.get(cm.group(1), cm.group(1))
        if p["chart"] == "pie":
            p["share"] = True
        changed = True
    if CMP_YEAR_RE.search(qn) or re.search(r"\blast year\b", qn) and re.search(r"compar|vs|versus|against", qn):
        p["compare"] = "previous_year"
        changed = True
    if not changed:
        # maybe a different measure in the same table
        ms = sorted([(score_col(expand(words(qn)), t, c, qn), c) for c in t.measures()], reverse=True)
        if ms and ms[0][0] >= 1.5:
            p["measures"] = [{"col": ms[0][1]}]
            changed = True
    return p if changed else None
