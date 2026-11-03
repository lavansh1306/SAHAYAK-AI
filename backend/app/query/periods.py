"""Deterministic time-period parsing (Indian financial year: April-March)."""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date

from ..config import fy_start, settings

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}
MON_ABBR = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10,
            "nov": 11, "dec": 12}
MON_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MON_RE = r"\b(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)\b"


def mnum(s: str) -> int:
    s = s.lower()
    return MONTHS.get(s) or MON_ABBR[s]


def ym(y: int, m: int) -> str:
    while m > 12:
        y, m = y + 1, m - 12
    while m < 1:
        y, m = y - 1, m + 12
    return f"{y:04d}-{m:02d}"


def add_months(s: str, n: int) -> str:
    return ym(int(s[:4]), int(s[5:7]) + n)


def month_label(s: str) -> str:
    return f"{MON_NAMES[int(s[5:7]) - 1]}-{s[2:4]}"


def month_long(s: str) -> str:
    return f"{list(MONTHS)[int(s[5:7]) - 1].capitalize()} {s[:4]}"


def fy_start_year(month: str) -> int:
    y, m = int(month[:4]), int(month[5:7])
    return y if m >= fy_start() else y - 1


def fy_label(start_year: int) -> str:
    if fy_start() == 1:
        return f"FY{start_year}"
    return f"FY{start_year}-{str(start_year + 1)[2:]}"


def months_between(a: str, b: str) -> list[str]:
    out, cur = [], a
    while cur <= b:
        out.append(cur)
        cur = add_months(cur, 1)
    return out


def today() -> date:
    if settings.as_of_date:
        return date.fromisoformat(settings.as_of_date)
    return date.today()


@dataclass(frozen=True)
class Period:
    start: str
    end: str
    label: str
    kind: str = "range"
    relative: bool = False

    def months(self) -> list[str]:
        return months_between(self.start, self.end)

    def shift_years(self, n: int) -> "Period":
        s, e = add_months(self.start, 12 * n), add_months(self.end, 12 * n)
        lab = self.label
        if self.kind in ("fy", "h1", "h2", "quarter"):
            fy = fy_start_year(s)
            lab = {"fy": fy_label(fy), "h1": f"H1 {fy_label(fy)}", "h2": f"H2 {fy_label(fy)}"}.get(self.kind, lab)
            if self.kind == "quarter":
                q = ((int(s[5:7]) - fy_start()) % 12) // 3 + 1
                lab = f"Q{q} {fy_label(fy)}"
        else:
            lab = describe(s, e)
        return Period(s, e, lab, self.kind, self.relative)

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "label": self.label, "kind": self.kind}


def describe(s: str, e: str) -> str:
    if s == e:
        return month_long(s)
    if s[:4] == e[:4]:
        return f"{MON_NAMES[int(s[5:7]) - 1]}-{MON_NAMES[int(e[5:7]) - 1]} {s[:4]}"
    return f"{month_label(s)} to {month_label(e)}"


def fy_period(start_year: int, relative=False) -> Period:
    s = ym(start_year, fy_start())
    return Period(s, add_months(s, 11), fy_label(start_year), "fy", relative)


def half_period(start_year: int, half: int, relative=False) -> Period:
    s = ym(start_year, fy_start() + (0 if half == 1 else 6))
    return Period(s, add_months(s, 5), f"H{half} {fy_label(start_year)}", f"h{half}", relative)


def quarter_period(start_year: int, q: int, relative=False) -> Period:
    s = ym(start_year, fy_start() + 3 * (q - 1))
    return Period(s, add_months(s, 2), f"Q{q} {fy_label(start_year)}", "quarter", relative)


def _year4(y: str) -> int:
    y = int(y)
    return y + 2000 if y < 100 else y


def _latest_occurrence(m: int, t: date) -> int:
    """Year of the latest month m that is not after today."""
    return t.year if m <= t.month else t.year - 1


def parse_fy(text: str, t: date | None = None) -> int | None:
    """Return FY start year if text mentions a specific / relative financial year."""
    t = t or today()
    s = text.lower()
    m = re.search(r"\b(?:fy|f\.y\.|financial year|fiscal year|fiscal)\s*'?(\d{2,4})\s*[-/–]\s*'?(\d{2,4})\b", s)
    if m:
        return _year4(m.group(1))
    m = re.search(r"\b(20\d{2})\s*[-–/]\s*(\d{2})\b", s)
    if m and int(m.group(2)) == (int(m.group(1)) + 1) % 100:
        return int(m.group(1))
    m = re.search(r"\bfy\s*'?(\d{2,4})\b", s)
    if m:
        return _year4(m.group(1)) - 1  # FY26 = year ending March 2026
    cur = fy_start_year(ym(t.year, t.month))
    last = re.search(r"\b(last|previous|prev|pichhle|pichle)\s+(financial\s+|fiscal\s+)?(year|fy|saal)\b", s)
    this = re.search(r"\b(this|current|is)\s+(financial\s+|fiscal\s+)?(year|fy|saal)\b|\bytd\b|year to date|so far this year", s)
    if last and this:
        return cur if this.start() < last.start() else cur - 1
    if last:
        return cur - 1
    if this:
        return cur
    return None


def parse_period(text: str, t: date | None = None) -> Period | None:
    t = t or today()
    s = " " + text.lower().replace("’", "'").replace("–", "-") + " "
    cur_m = ym(t.year, t.month)
    cur_fy = fy_start_year(cur_m)

    # --- halves -------------------------------------------------------------------
    hm = re.search(r"\b(h1|h2|first half|1st half|second half|2nd half)\b", s)
    if hm:
        half = 1 if hm.group(1) in ("h1", "first half", "1st half") else 2
        fy = parse_fy(s, t)
        cur_fy0 = fy_start_year(cur_m)
        if fy == cur_fy0 - 1 and re.search(r"(compared? (to|with)|\bcompare\b.*\bwith|\bvs\.?|\bversus|\bthan|\bover|\bagainst)\s+(the\s+)?(same (period|time) )?(last|previous|prev) (financial |fiscal )?year", s):
            fy = cur_fy0  # 'last year' is the comparison base, not the period asked about
        rel = fy is None or bool(re.search(r"this|last|previous|current", s))
        return half_period(fy if fy is not None else cur_fy, half, rel)
    # --- quarters -----------------------------------------------------------------
    qm = re.search(r"\bq([1-4])\b", s)
    if qm:
        fy = parse_fy(s, t)
        return quarter_period(fy if fy is not None else cur_fy, int(qm.group(1)), fy is None)
    if re.search(r"\b(last|previous|prev|pichhle|pichle)\s+quarter\b", s):
        q_start = ym(t.year, t.month - ((t.month - fy_start()) % 3))
        ps = add_months(q_start, -3)
        fy = fy_start_year(ps)
        q = ((int(ps[5:7]) - fy_start()) % 12) // 3 + 1
        return quarter_period(fy, q, True)
    if re.search(r"\b(this|current)\s+quarter\b", s):
        q_start = ym(t.year, t.month - ((t.month - fy_start()) % 3))
        fy = fy_start_year(q_start)
        q = ((int(q_start[5:7]) - fy_start()) % 12) // 3 + 1
        return quarter_period(fy, q, True)
    # --- explicit month ranges ----------------------------------------------------
    rm = re.search(MON_RE + r"\s*'?(\d{2,4})?\s*(?:to|and|-|till|until|through|thru)\s*" + MON_RE + r"\s*'?(\d{2,4})?\b", s)
    if rm:
        m1, y1, m2, y2 = mnum(rm.group(1)), rm.group(2), mnum(rm.group(3)), rm.group(4)
        if y2:
            y2i = _year4(y2)
        elif y1:
            y2i = _year4(y1) + (1 if m2 < m1 else 0)
        else:
            y2i = _latest_occurrence(m2, t)
        y1i = _year4(y1) if y1 else (y2i if m1 <= m2 else y2i - 1)
        a, b = ym(y1i, m1), ym(y2i, m2)
        return Period(a, b, describe(a, b), "range", not (y1 or y2))
    # --- since / up to -------------------------------------------------------------
    sm = re.search(r"\b(?:since|from)\s+" + MON_RE + r"\s*'?(\d{2,4})?", s)
    if sm:
        m1 = mnum(sm.group(1))
        y1 = _year4(sm.group(2)) if sm.group(2) else _latest_occurrence(m1, t)
        a = ym(y1, m1)
        return Period(a, "9999-12", f"since {month_label(a)}", "since", False)
    um = re.search(r"\b(?:up ?to|upto|till|until|through|to end of|by end of)\s+" + MON_RE + r"\s*'?(\d{2,4})?", s)
    if um:
        m1 = mnum(um.group(1))
        y1 = _year4(um.group(2)) if um.group(2) else _latest_occurrence(m1, t)
        b = ym(y1, m1)
        return Period("0000-01", b, f"up to {month_label(b)}", "upto", False)
    # --- FY --------------------------------------------------------------------------
    fy = parse_fy(s, t)
    if fy is not None:
        explicit = bool(re.search(r"\b(fy|f\.y\.|financial year|fiscal year|fiscal)\s*'?\d|\b20\d{2}\s*[-/]\s*\d{2}\b", s))
        return fy_period(fy, relative=not explicit)
    # --- relative months --------------------------------------------------------------
    nm = re.search(r"\blast\s+(\d{1,2})\s+months\b", s)
    if nm:
        n = int(nm.group(1))
        b = add_months(cur_m, -1)
        return Period(add_months(b, -(n - 1)), b, f"last {n} months", "range", True)
    if re.search(r"\b(last|previous|prev|pichhle|pichle)\s+(month|mahine|mahina)\b", s):
        p = add_months(cur_m, -1)
        return Period(p, p, month_long(p), "month", True)
    if re.search(r"\b(this|current|is)\s+(month|mahine|mahina)\b", s):
        return Period(cur_m, cur_m, month_long(cur_m), "month", True)
    # --- single month -----------------------------------------------------------------
    mm = re.search(r"\b(20\d{2})-(0[1-9]|1[0-2])\b", s)
    if mm:
        p = f"{mm.group(1)}-{mm.group(2)}"
        return Period(p, p, month_long(p), "month")
    mm = re.search(MON_RE + r"[\s\-,/']*(\d{4}|\d{2})\b", s)
    if mm:
        p = ym(_year4(mm.group(2)), mnum(mm.group(1)))
        return Period(p, p, month_long(p), "month")
    mm = re.search(MON_RE, s)
    if mm and (mm.group(1) != "may" or re.search(r"\b(in|of|for|during|since)\s+may\b", s)):
        m1 = mnum(mm.group(1))
        p = ym(_latest_occurrence(m1, t), m1)
        return Period(p, p, month_long(p), "month", True)
    cm = re.search(r"\b(?:in|calendar year|cy)\s*(20\d{2})\b", s)
    if cm:
        y = int(cm.group(1))
        return Period(f"{y}-01", f"{y}-12", f"calendar {y}", "year")
    return None


def fit_period(p: Period | None, available: list[str], default: Period | None = None) -> tuple[Period | None, list[str]]:
    """Clamp a requested period to the months available in a dataset. Returns (period, notes)."""
    notes: list[str] = []
    if not available:
        return None, ["No data available."]
    lo, hi = min(available), max(available)
    if p is None:
        return default, notes
    s, e = max(p.start, lo), min(p.end, hi)
    if s <= e:
        if (s, e) != (p.start, p.end) and p.kind not in ("since", "upto"):
            notes.append(f"Data available only for {describe(s, e)} within {p.label}.")
        lab = p.label if (s, e) == (p.start, p.end) else describe(s, e)
        if p.kind in ("since", "upto"):
            lab = describe(s, e)
        return replace(p, start=s, end=e, label=lab), notes
    if p.relative:
        n = len(p.months())
        e2 = hi
        if p.kind == "quarter":
            # latest complete FY quarter in data
            while (int(add_months(e2, 1)[5:7]) - fy_start()) % 3 != 0:
                e2 = add_months(e2, -1)
        s2 = add_months(e2, -(n - 1))
        q = p.kind
        lab = describe(s2, e2)
        if q == "quarter":
            fy = fy_start_year(s2)
            lab = f"Q{((int(s2[5:7]) - fy_start()) % 12) // 3 + 1} {fy_label(fy)}"
        notes.append(f"Latest data is {month_long(hi)}, so '{p.label}' was read as {lab}.")
        return Period(s2, e2, lab, q, True), notes
    notes.append(f"No data for {p.label}. Data covers {describe(lo, hi)}.")
    return None, notes
