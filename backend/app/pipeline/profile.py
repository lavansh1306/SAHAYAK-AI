"""Stage 2 - Profile: work out what every column is, with no fixed schema.

For each column: type (number, date, datetime, month, year, financial-year label, category, id, text, boolean),
role (measure, dimension, time, id, text), how it adds up (sum, average, latest value, ratio), unit and scale,
and a confidence. Also detects 'wide' layouts (one column per month/quarter) and per-row unit columns."""
from __future__ import annotations

import re
from datetime import datetime

import numpy as np
import pandas as pd

NA_TEXT = {"", "na", "n/a", "n.a.", "nan", "null", "none", "-", "--", "—", "–", "nil", "#n/a", "#value!", "#ref!", "#div/0!", "?",
           "not available", "missing", "blank", "nr", "n.r."}
MON = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
       "january": 1, "february": 2, "march": 3, "april": 4, "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
       "november": 11, "december": 12}
MON_ALT = "|".join(sorted(MON, key=len, reverse=True))
TOTAL_RE = re.compile(r"^\s*(grand\s*|sub[-\s]*)?totals?\b|^\s*(all|overall)\s*(areas?|segments?|regions?|zones?|items?)?\s*$|^\s*sum\s*$", re.I)

HINT_DATE = re.compile(r"(^|_)(date|dt|day|time|timestamp|ts|datetime|created|updated|opened|closed|start|end|posted|due|paid_on|on|at|when|"
                       r"start_time|end_time|reading_time|txn_date)(_|$)", re.I)
HINT_MONTH = re.compile(r"(^|_)(month|mon|period|yyyymm|billing_month|bill_month|mnth)(_|$)", re.I)
HINT_YEAR = re.compile(r"(^|_)(year|yr|calendar_year)(_|$)", re.I)
SERIAL = re.compile(r"^(sl|s_?no|sr_?no|sl_?no|serial|serial_no|row|row_no|row_number|index|idx|no|c_no|sn|s_n)$", re.I)
COUNTISH = re.compile(r"(events?|count|incidents?|breach(es)?|number_of|no_of|trips|visits|calls|tickets|complaints|defects|errors|failures|alarms)", re.I)
HINT_ID = re.compile(r"(^|_)(id|code|no|num|number|ref|key|uid|uuid|account|acct|meter|serial|pin|pincode|zip|phone|mobile|ca|consumer_no|"
                     r"invoice|ticket|trip|txn|transaction|order|registration|reg|vehicle_no|bp)(_|$)", re.I)
NONADD = re.compile(r"(price|rate|tariff|(^|_)per(_|$)|_per_|avg|average|mean|pct|percent|percentage|ratio|share|score|rating|temp|temperature|"
                    r"pressure|speed|kmph|kph|velocity|(^|_)level|index|efficiency|factor|density|odorant|concentration|utili[sz]ation|margin|"
                    r"yield|(^|_)age(_|$)|(^|_)lat(itude)?(_|$)|(^|_)lon(gitude)?(_|$)|_bar$|(^|_)bar_|psi|(^|_)ph(_|$)|humidity|voltage|frequency|"
                    r"scmh|per_hour|(^|_)flow|(^|_)ce(_|$)|realisation|realization|unit_cost|cost_per)", re.I)
SEMI = re.compile(r"(active|connections|subscribers|(^|_)balance|stock|inventory|headcount|outstanding|closing|opening|capacity|installed|"
                  r"population|(^|_)members|no_of_customers|customer_count|customers$|consumers$|fleet_size|strength)", re.I)
FLOW_WORDS = re.compile(r"(new|added|addition|net_add|disconnect|gross|sold|sales|billed|collected)", re.I)

# unit tokens in column names -> (label, kind, factor to the kind's base unit)
UNIT_RULES = [
    (re.compile(r"mg_?per_?m3|mg/m3|mg_m3", re.I), "mg/m3", "concentration", 1.0),
    (re.compile(r"(rs|inr|₹|rupee)\W*(in\W*)?(crores?|cr)\b|\bcrores?\b|(^|_)cr$", re.I), "Rs crore", "money", 1e7),
    (re.compile(r"\b(lakhs?|lacs?)\b|(^|_)(lakhs?|lacs?)(_|$)", re.I), "Rs lakh", "money", 1e5),
    (re.compile(r"(rs|inr|₹)\W*(in\W*)?('000|000s|thousands?|k)\b", re.I), "Rs thousand", "money", 1e3),
    (re.compile(r"(rs|inr|₹)\W*(in\W*)?(millions?|mn)\b", re.I), "Rs million", "money", 1e6),
    (re.compile(r"(^|[_\s(])(rs|inr|rupees?)([_\s.)]|$)|₹", re.I), "Rs", "money", 1.0),
    (re.compile(r"mmscm", re.I), "MMSCM", "volume", 1e6),
    (re.compile(r"('000|000s|thousand|(^|_)k|(^|_)m)\W*_?scm|(^|_)mscm|tscm", re.I), "'000 SCM", "volume", 1e3),
    (re.compile(r"scmh|scm_?/_?h|scm_per_hour", re.I), "SCM/h", "flow", 1.0),
    (re.compile(r"(^|[_\s(])(scm|sm3|m3)([_\s)]|$)", re.I), "SCM", "volume", 1.0),
    (re.compile(r"(^|[_\s(])(pct|percent|percentage)([_\s)]|$)|%", re.I), "%", "percent", 1.0),
    (re.compile(r"(^|[_\s(])kg([_\s)]|$)|kilograms?", re.I), "kg", "mass", 1.0),
    (re.compile(r"(^|[_\s(])(hours|hrs|hr)([_\s)]|$)", re.I), "hours", "time", 1.0),
    (re.compile(r"(^|[_\s(])(minutes|mins|min)([_\s)]|$)", re.I), "minutes", "time", 1.0),
    (re.compile(r"(^|[_\s(])days([_\s)]|$)", re.I), "days", "time", 1.0),
    (re.compile(r"(^|[_\s(])km([_\s)]|$)", re.I), "km", "distance", 1.0),
    (re.compile(r"(^|[_\s(])(bar|barg|psi|kpa)([_\s)]|$)", re.I), "bar", "pressure", 1.0),
    (re.compile(r"(^|[_\s(])(kwh|mwh)([_\s)]|$)", re.I), "kWh", "energy", 1.0),
]
# per-row unit values (a 'Unit' column)
ROW_UNITS = [
    (re.compile(r"^\s*mmscm\s*$", re.I), "volume", 1e6), (re.compile(r"^\s*('000|000|thousand|k|m)\s*scm\s*$|^\s*mscm\s*$", re.I), "volume", 1e3),
    (re.compile(r"^\s*(scm|sm3|m3)\s*$", re.I), "volume", 1.0), (re.compile(r"^\s*(crores?|cr|rs\.?\s*crores?)\s*$", re.I), "money", 1e7),
    (re.compile(r"^\s*(lakhs?|lacs?|rs\.?\s*lakhs?)\s*$", re.I), "money", 1e5), (re.compile(r"^\s*(rs\.?|inr|rupees?)\s*$", re.I), "money", 1.0),
    (re.compile(r"^\s*(kg|kgs)\s*$", re.I), "mass", 1.0), (re.compile(r"^\s*(tonnes?|tons?|mt)\s*$", re.I), "mass", 1e3),
]


def clean_name(s: str) -> str:
    s = str(s).strip().replace("%", " pct ").replace("₹", " rs ").replace("'000", " thousand ").replace("#", " no ")
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    s = re.sub(r"_+", "_", s)
    if not s:
        s = "column"
    if s[0].isdigit():
        s = "c_" + s
    return s[:60]


def label_for(original: str, name: str) -> str:
    from ..util import nice_label
    o = str(original).strip()
    if re.fullmatch(r"[a-z0-9_]+", o):
        return nice_label(o)
    return o[:1].upper() + o[1:]


def is_na(v) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and np.isnan(v):
        return True
    if v is pd.NaT:
        return True
    if isinstance(v, str) and v.strip().lower() in NA_TEXT:
        return True
    return False


# ---------------------------------------------------------------------------------- parsers
_NUM = re.compile(r"^\(?\s*[-+−]?\s*(?:rs\.?|inr|₹|\$|usd)?\s*[-+−]?\s*(\d{1,3}(?:,\d{2,3})+|\d+)?(\.\d+)?\s*(%|cr|crores?|lakhs?|lacs?)?\s*\)?$", re.I)


def parse_numbers(s: pd.Series) -> tuple[pd.Series, pd.Series, dict]:
    """-> values (float), bad mask (present but unreadable), info {pct, currency}."""
    info = {"pct": 0, "currency": 0, "suffix": 0}
    if pd.api.types.is_bool_dtype(s):
        return s.astype(float), pd.Series(False, index=s.index), info
    if pd.api.types.is_numeric_dtype(s):
        v = pd.to_numeric(s, errors="coerce").astype(float)
        return v, pd.Series(False, index=s.index), info
    out = np.full(len(s), np.nan)
    bad = np.zeros(len(s), dtype=bool)
    for i, x in enumerate(s.tolist()):
        if is_na(x):
            continue
        if isinstance(x, bool):
            out[i] = float(x)
            continue
        if isinstance(x, (int, float, np.integer, np.floating)):
            out[i] = float(x)
            continue
        if isinstance(x, (pd.Timestamp, datetime)):
            bad[i] = True
            continue
        t = str(x).strip().replace(" ", " ")
        m = _NUM.match(t)
        if not m or (m.group(1) is None and m.group(2) is None):
            bad[i] = True
            continue
        num = float((m.group(1) or "0").replace(",", "") + (m.group(2) or ""))
        neg = t.startswith("(") and t.endswith(")") or re.match(r"^\(?\s*[-−]", t) or re.search(r"(rs\.?|inr|₹|\$)\s*[-−]", t, re.I)
        if neg:
            num = -num
        if m.group(3):
            if m.group(3) == "%":
                info["pct"] += 1
            else:
                info["suffix"] += 1
        if re.search(r"rs\.?|inr|₹|\$", t, re.I):
            info["currency"] += 1
        out[i] = num
    return pd.Series(out, index=s.index), pd.Series(bad, index=s.index), info


def _month_from_text(t: str) -> str | None:
    t = t.strip().lower().replace("’", "'")
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})", t)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{4})", t)
    if m and 1 <= int(m.group(1)) <= 12:
        return f"{m.group(2)}-{int(m.group(1)):02d}"
    m = re.fullmatch(rf"({MON_ALT})[\s\-/.,']*'?(\d{{4}}|\d{{2}})", t)
    if m:
        y = int(m.group(2))
        y = y + 2000 if y < 100 else y
        return f"{y}-{MON[m.group(1)]:02d}"
    m = re.fullmatch(rf"(\d{{4}})[\s\-/.,]*({MON_ALT})", t)
    if m:
        return f"{m.group(1)}-{MON[m.group(2)]:02d}"
    m = re.fullmatch(r"(20\d{2}|19\d{2})(0[1-9]|1[0-2])", t)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return None


def parse_months(s: pd.Series, allow_dates: bool = True) -> tuple[pd.Series, pd.Series]:
    out, bad = [], []
    for x in s.tolist():
        if is_na(x):
            out.append(None)
            bad.append(False)
            continue
        if isinstance(x, (pd.Timestamp, datetime)) and allow_dates:
            out.append(f"{x.year:04d}-{x.month:02d}")
            bad.append(False)
            continue
        if isinstance(x, (int, np.integer)) and 190001 <= int(x) <= 210012 and 1 <= int(x) % 100 <= 12:
            out.append(f"{int(x) // 100}-{int(x) % 100:02d}")
            bad.append(False)
            continue
        t = str(x)
        m = _month_from_text(t)
        if m is None and allow_dates:
            m2 = re.fullmatch(r"(\d{4})-(\d{2})-01( 00:00(:00)?)?", t.strip())
            if m2:
                m = f"{m2.group(1)}-{m2.group(2)}"
        out.append(m)
        bad.append(m is None)
    return pd.Series(out, index=s.index, dtype=object), pd.Series(bad, index=s.index)


_DMY = re.compile(r"^\s*(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2}|\d{4})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?\s*(am|pm)?)?\s*$", re.I)
_ISO = re.compile(r"^\s*(\d{4})[/.\-](\d{1,2})[/.\-](\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?)?\s*(z|[+-]\d{2}:?\d{2})?\s*$", re.I)


def date_order(s: pd.Series) -> dict:
    """Decide dd/mm vs mm/dd from the evidence in the column."""
    a_gt, b_gt, n = 0, 0, 0
    for x in s.dropna().astype(str).head(20000):
        m = _DMY.match(x)
        if not m:
            continue
        n += 1
        a, b = int(m.group(1)), int(m.group(2))
        a_gt += a > 12
        b_gt += b > 12
    if n == 0:
        return {"order": None, "ambiguous": False, "n": 0}
    if a_gt and not b_gt:
        return {"order": "dayfirst", "ambiguous": False, "n": n}
    if b_gt and not a_gt:
        return {"order": "monthfirst", "ambiguous": False, "n": n}
    if a_gt and b_gt:
        return {"order": "mixed", "ambiguous": True, "n": n}
    return {"order": "dayfirst", "ambiguous": True, "n": n}  # all parts <= 12: cannot tell


def parse_datetimes(s: pd.Series, order: str | None = None, excel_serial: bool = False) -> tuple[pd.Series, pd.Series, dict]:
    """-> datetime64 values, bad mask, info {order, ambiguous, has_time}."""
    info = date_order(s) if s.dtype == object else {"order": None, "ambiguous": False, "n": 0}
    if order:
        info = {**info, "order": order, "ambiguous": False}
    dayfirst = info.get("order") in ("dayfirst", None, "mixed")
    out = np.empty(len(s), dtype="datetime64[ns]")
    out[:] = np.datetime64("NaT")
    bad = np.zeros(len(s), dtype=bool)
    vals = s.tolist()
    cache: dict = {}
    for i, x in enumerate(vals):
        if is_na(x):
            continue
        if isinstance(x, (pd.Timestamp, datetime, np.datetime64)):
            out[i] = np.datetime64(pd.Timestamp(x).to_datetime64())
            continue
        if isinstance(x, (int, float, np.integer, np.floating)) and not isinstance(x, bool):
            if excel_serial and 20000 <= float(x) <= 80000:
                out[i] = np.datetime64(pd.Timestamp("1899-12-30") + pd.Timedelta(days=float(x)))
            elif 1e9 <= float(x) <= 4e9:  # unix seconds
                out[i] = np.datetime64(pd.Timestamp(int(x), unit="s"))
            else:
                bad[i] = True
            continue
        t = str(x).strip()
        if t in cache:
            v = cache[t]
        else:
            v = None
            m = _ISO.match(t)
            if m:
                try:
                    v = pd.Timestamp(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0))
                except ValueError:
                    v = None
            else:
                m = _DMY.match(t)
                if m:
                    a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    y = y + 2000 if y < 100 else y
                    d, mo = (a, b) if dayfirst else (b, a)
                    if info.get("order") == "mixed" and (a > 12 or b > 12):
                        d, mo = (a, b) if a > 12 else (b, a)
                    hh = int(m.group(4) or 0)
                    if m.group(7) and m.group(7).lower() == "pm" and hh < 12:
                        hh += 12
                    try:
                        v = pd.Timestamp(y, mo, d, hh, int(m.group(5) or 0), int(m.group(6) or 0))
                    except ValueError:
                        v = None
                else:
                    try:
                        v = pd.to_datetime(t, dayfirst=dayfirst, errors="coerce")
                        if v is not pd.NaT and pd.notna(v):
                            v = pd.Timestamp(v).tz_localize(None) if getattr(v, "tzinfo", None) else pd.Timestamp(v)
                        else:
                            v = None
                    except Exception:
                        v = None
            cache[t] = v
        if v is None:
            bad[i] = True
        else:
            out[i] = np.datetime64(v.to_datetime64())
    res = pd.Series(out, index=s.index)
    ok = res.dropna()
    info["has_time"] = bool(len(ok)) and bool(((ok.dt.hour != 0) | (ok.dt.minute != 0)).any())
    return res, pd.Series(bad, index=s.index), info


FY_RE = re.compile(r"^\s*(fy|f\.y\.)?\s*'?(\d{2,4})\s*[-/–]\s*'?(\d{2,4})\s*$|^\s*fy\s*'?(\d{2,4})\s*$", re.I)
BOOL_T = {"yes", "no", "y", "n", "true", "false", "t", "f"}


def unit_from_name(name: str) -> tuple[str, str, float]:
    for rx, label, kind, factor in UNIT_RULES:
        if rx.search(name):
            return label, kind, factor
    return "", "", 1.0


# ---------------------------------------------------------------------------------- profiling
def profile_column(original: str, name: str, s: pd.Series) -> dict:
    nn = s[[not is_na(x) for x in s.tolist()]] if s.dtype == object else s.dropna()
    n, total = len(nn), len(s)
    p = {"name": name, "original": str(original), "label": label_for(original, name), "n": total, "filled": n,
         "nulls": total - n, "type": "text", "role": "text", "agg": None, "unit": "", "unit_kind": "", "scale": 1.0, "confidence": 1.0, "notes": []}
    if n == 0:
        p.update(type="empty", role="ignore", confidence=1.0)
        return p
    sample = nn.head(5000)
    nm = name.lower()
    uniq = nn.astype(str).nunique()
    p["unique"] = int(uniq)
    p["sample"] = [str(x)[:40] for x in nn.astype(str).drop_duplicates().head(6).tolist()]
    is_dt = pd.api.types.is_datetime64_any_dtype(s) or sample.map(lambda x: isinstance(x, (pd.Timestamp, datetime))).mean() > 0.9
    num_v, num_bad, num_info = parse_numbers(sample)
    num_rate = float(num_v.notna().sum()) / max(len(sample), 1)
    if is_dt:
        dv = pd.to_datetime(nn, errors="coerce")
        all_first = bool(len(dv.dropna())) and (dv.dropna().dt.day == 1).all() and not ((dv.dropna().dt.hour != 0).any())
        if all_first and (HINT_MONTH.search(nm) or dv.dropna().dt.to_period("M").nunique() == dv.dropna().nunique()) and dv.dropna().nunique() >= 2 and not HINT_DATE.search(nm):
            p.update(type="month", role="time")
        else:
            has_t = bool(((dv.dropna().dt.hour != 0) | (dv.dropna().dt.minute != 0)).any())
            p.update(type="datetime" if has_t else "date", role="time")
        return p
    if SERIAL.match(nm):
        p.update(type="id", role="ignore", notes=["serial / row number column - not used for analysis"])
        return p
    if pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s):
        v = num_v.dropna()
        ints = bool(len(v)) and bool(np.all(np.abs(v - np.round(v)) < 1e-9))
        if ints and len(v) >= 10 and v.is_unique and float(v.min()) in (0.0, 1.0) and float(v.max() - v.min()) == len(v) - 1 and not HINT_DATE.search(nm):
            p.update(type="id", role="ignore", notes=["looks like a row number (1, 2, 3 ...) - not used for analysis"])
            return p
        if HINT_DATE.search(nm) and ints and v.between(20000, 80000).all():
            p.update(type="date", role="time", excel_serial=True)
            return p
        if HINT_YEAR.search(nm) and ints and v.between(1950, 2100).all():
            p.update(type="year", role="time")
            return p
        if HINT_MONTH.search(nm) and ints and v.between(190001, 210012).all():
            p.update(type="month", role="time")
            return p
        if ints and HINT_ID.search(nm) and (uniq / n > 0.3 or uniq > 50):
            p.update(type="id", role="id" if uniq > 200 else "dimension")
            return p
        return _number_profile(p, nm, v, num_info, original)
    # ---- object column: try parsers on the sample
    txt = sample.astype(str).str.strip()
    mon_v, mon_bad = parse_months(sample)
    mon_rate = float(mon_v.notna().sum()) / len(sample)
    iso_or_dmy = txt.str.match(_ISO.pattern + "|" + _DMY.pattern, case=False).mean()
    word_dates = txt.str.contains(rf"\b\d{{1,2}}[\s\-/](?:{MON_ALT})[\s\-/,]+\d{{2,4}}\b|\b(?:{MON_ALT})\s+\d{{1,2}},?\s+\d{{4}}\b", case=False, regex=True).mean()
    fy_rate = txt.str.match(FY_RE.pattern, case=False).mean()
    bool_rate = txt.str.lower().isin(BOOL_T).mean()
    if mon_rate >= 0.9 and not txt.str.match(r"^\d{4}-\d{2}-\d{2}", na=False).mean() > 0.5 and num_rate < 0.9:
        p.update(type="month", role="time", confidence=round(mon_rate, 3))
        return p
    if (iso_or_dmy + word_dates) >= 0.85 and num_rate < 0.5:
        dv, dbad, dinfo = parse_datetimes(sample)
        rate = float(dv.notna().sum()) / len(sample)
        if rate >= 0.85:
            all_first = (dv.dropna().dt.day == 1).all() and not dinfo.get("has_time")
            if all_first and HINT_MONTH.search(nm):
                p.update(type="month", role="time", confidence=round(rate, 3))
            else:
                p.update(type="datetime" if dinfo.get("has_time") else "date", role="time", confidence=round(rate, 3),
                         date_order=dinfo.get("order"), date_ambiguous=bool(dinfo.get("ambiguous")))
            return p
    if num_rate >= 0.85:
        v = num_v.dropna()
        ints = bool(len(v)) and bool(np.all(np.abs(v - np.round(v)) < 1e-9))
        if ints and HINT_ID.search(nm) and (uniq / n > 0.3 or uniq > 50) and not num_info["pct"]:
            p.update(type="id", role="id" if uniq > 200 else "dimension", confidence=round(num_rate, 3))
            return p
        if HINT_YEAR.search(nm) and ints and v.between(1950, 2100).all():
            p.update(type="year", role="time")
            return p
        p = _number_profile(p, nm, v, num_info, original)
        p["confidence"] = round(num_rate, 3)
        return p
    if fy_rate >= 0.9:
        p.update(type="fy_label", role="dimension", confidence=round(fy_rate, 3))
        return p
    if bool_rate >= 0.95:
        p.update(type="boolean", role="dimension", confidence=round(bool_rate, 3))
        return p
    avg_len = txt.str.len().mean()
    spaces = txt.str.contains(r"\s").mean()
    ratio = uniq / n
    if avg_len > 40 or (ratio > 0.8 and spaces > 0.6 and avg_len > 18):
        p.update(type="text", role="text")
    elif (ratio >= 0.95 and n >= 20) or (HINT_ID.search(nm) and (ratio > 0.5 or uniq > 200)) or (uniq > 1000 and txt.str.fullmatch(r"[A-Za-z0-9_\-/.]+").mean() > 0.95):
        p.update(type="id", role="id" if uniq > 200 else "dimension")
    else:
        p.update(type="category", role="dimension")
        if 0.5 <= num_rate < 0.85:
            p["confidence"] = round(1 - num_rate, 3)
            p["notes"].append(f"{num_rate:.0%} of values look like numbers - check whether this column should be a number")
    return p


def _number_profile(p: dict, nm: str, v: pd.Series, info: dict, original: str) -> dict:
    unit, kind, factor = unit_from_name(f"{original} {nm}")
    if not unit and info.get("pct", 0) >= 0.5 * max(len(v), 1):
        unit, kind, factor = "%", "percent", 1.0
    if not unit and info.get("currency", 0) >= 0.5 * max(len(v), 1):
        unit, kind, factor = "Rs", "money", 1.0
    agg = "sum"
    if COUNTISH.search(nm) and kind not in ("percent",) and not re.search(r"(rate|pct|percent|ratio|avg|average|per_)", nm):
        pass
    elif kind in ("percent", "pressure", "flow", "concentration") or NONADD.search(nm):
        agg = "mean"
    elif SEMI.search(nm) and not FLOW_WORDS.search(nm):
        agg = "last"
    p.update(type="number", role="measure", agg=agg, unit=unit, unit_kind=kind, scale=factor)
    if len(v):
        p.update(min=float(v.min()), max=float(v.max()), integer=bool(np.all(np.abs(v - np.round(v)) < 1e-9)))
    return p


# ---------------------------------------------------------------------------------- table level
def unit_column(df: pd.DataFrame) -> str | None:
    for c in df.columns:
        if re.fullmatch(r"(units?|uom|unit[_ ]of[_ ]measure(ment)?|measure|unit[_ ]name)", str(c).strip(), re.I):
            vals = [str(x) for x in df[c].dropna().unique()[:30]]
            if vals and sum(any(rx.match(v) for rx, _, _ in ROW_UNITS) for v in vals) >= 0.8 * len(vals):
                return c
    return None


PERIOD_HDR = re.compile(rf"^\s*((q[1-4]|quarter\s*[1-4]|h[12])\b.*|({MON_ALT})\b[\s\-'/]*(\d{{2,4}})?.*|(19|20)\d{{2}}(\s*[-/]\s*\d{{2,4}})?|fy\s*'?\d{{2,4}}.*|\d{{4}}-\d{{2}}(-\d{{2}})?)\s*$", re.I)
TOTAL_HDR = re.compile(r"^\s*(total|grand total|annual|annual total|ytd|year total|full year|sum|overall)\b", re.I)


def wide_layout(df: pd.DataFrame) -> dict | None:
    """One column per period (months, quarters, years) -> should be turned into rows."""
    cols = [str(c) for c in df.columns]
    per = [c for c in cols if PERIOD_HDR.match(c) and not TOTAL_HDR.match(c)]
    if len(per) < 3:
        return None
    numeric_share = []
    for c in per:
        v, bad, _ = parse_numbers(df[c])
        filled = df[c].map(lambda x: not is_na(x)).sum()
        numeric_share.append(float(v.notna().sum()) / max(int(filled), 1))
    if np.mean(numeric_share) < 0.6:
        return None
    totals = [c for c in cols if TOTAL_HDR.match(c)]
    ids = [c for c in cols if c not in per and c not in totals]
    if not ids:
        return None
    return {"period_columns": per, "total_columns": totals, "id_columns": ids}


def period_label(h: str) -> tuple[str, str]:
    """Header -> (value, kind) where kind is month / text."""
    m = _month_from_text(re.sub(r"\(.*?\)", "", h).strip())
    if m:
        return m, "month"
    m2 = re.match(r"^\s*(\d{4}-\d{2})-\d{2}", h)
    if m2:
        return m2.group(1), "month"
    return re.sub(r"\s+", " ", h).strip(), "text"
