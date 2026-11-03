"""Generic insight detectors - run over every loaded table that has numbers and (ideally) a time column.

Trend, seasonality, step change, latest-period jump/drop per member, steady decline/rise per member, abnormal
day/hour windows, members far from their peers, concentration, new/stopped members, spikes in event counts,
plan-vs-actual gaps and data-quality problems. Each finding carries its numbers, chart data and source.
Nothing here knows what the data is about; the text is built from the data dictionary labels."""
from __future__ import annotations

import re
import threading

import numpy as np
import pandas as pd

from ..query import engine as E
from ..query.answer import result
from ..query.understand import bad_direction
from ..util import fmt_value, group, pct

_cache: dict = {}
_lock = threading.Lock()
SEV_W = {"high": 3.0, "medium": 2.0, "low": 1.0}


def _f(v, mi, d=None):
    return fmt_value(v, mi.get("unit", ""), mi.get("unit_kind", ""), d)


def _label(mi) -> str:
    return mi["label"]


def _lc(s: str) -> str:
    return s[:1].lower() + s[1:] if s and not s[:2].isupper() else s


def is_panel(t, dim: str | None) -> bool:
    """One row per time point per member (e.g. daily readings per station) - safe for per-member time checks."""
    k = t.entry.get("keys") or []
    return bool(t.time) and t.time in k and (dim is None or dim in k or len(k) == 2)


def is_event_table(t) -> bool:
    k = t.entry.get("keys") or []
    return len(k) == 1 and t.cols.get(k[0], {}).get("type") == "id"


def _series(t, df: pd.DataFrame, mi: dict, grain: str, dim: str | None = None) -> pd.DataFrame:
    """-> DataFrame [bucket, (dim), v] using the same aggregation rules as answers."""
    ts, month = E.time_parts(t, df)
    d = df.copy()
    d["__bucket"] = E.bucket(ts, month, grain)
    keys = ["__bucket"] + ([dim] if dim else [])
    if grain == "month" and t.time_type in ("date", "datetime") and ts.notna().any():
        last = ts.max()
        if last.day < (last + pd.offsets.MonthEnd(0)).day - 2:  # latest month is only partly covered
            d = d[d["__bucket"] < last.strftime("%Y-%m")]
            ts = ts.loc[d.index]
    r = E.aggregate(d, keys, mi, ts, None).rename("v").reset_index()
    return r.dropna(subset=["v"]).sort_values(keys)


def _mk(cat, title, detail, sev, impact, t, mi=None, entity="", period="", chart=None, extra=None) -> dict:
    return {"id": re.sub(r"[^a-z0-9]+", "-", f"{t.key}-{cat}-{mi['col'] if mi else ''}-{entity}-{period}".lower())[:90], "category": cat, "title": title,
            "detail": detail, "severity": sev, "impact": round(float(impact), 3), "table": t.key, "table_label": t.label, "measure": mi["col"] if mi else None,
            "measure_label": _label(mi) if mi else None, "entity": entity, "period": period, "chart": chart,
            "source": f"{t.label} ({', '.join(t.entry.get('sources', [])[:2])})", **(extra or {})}


def _grain_for(t) -> str | None:
    tr = t.entry.get("time_range")
    if not t.time or not tr:
        return None
    try:
        a, b = pd.Timestamp(str(tr[0])[:10]), pd.Timestamp(str(tr[1])[:10])
    except Exception:
        return None
    months = (b.year - a.year) * 12 + b.month - a.month + 1
    if months >= 6:
        return "month"
    if t.entry.get("grain") in ("day", "hour", "minute"):
        return "day"
    return None


def _measures(t) -> list[dict]:
    out = []
    for c in t.measures()[:5]:
        out.append(E.measure_info(t, {"col": c}))
    if t.time and (len(out) < 2 or t.ids()):
        out.append(E.measure_info(t, {"col": "*", "agg": "count"}))
    return out


def _dims(t) -> list[str]:
    ds = []
    for c in t.dims():
        e = t.cols[c]
        if 2 <= e.get("card", 0) <= 40 and e.get("type") != "fy_label" and c != "unit_reported":
            ds.append(c)
    return ds[:3]


# ------------------------------------------------------------------------------------- detectors
def trend(t, mi, s: pd.DataFrame, grain: str) -> list[dict]:
    y = s["v"].to_numpy(dtype=float)
    if len(y) < 6 or np.nanmean(np.abs(y)) == 0:
        return []
    x = np.arange(len(y))
    k, b0 = np.polyfit(x, y, 1)
    fit = k * x + b0
    ss = ((y - y.mean()) ** 2).sum()
    r2 = 1 - ((y - fit) ** 2).sum() / ss if ss else 0
    a, b = np.mean(y[:3]), np.mean(y[-3:])
    chg = (b / a - 1) * 100 if a else np.nan
    if r2 < 0.55 or not np.isfinite(chg) or abs(chg) < 10:
        return []
    up = chg > 0
    bad = bad_direction(t, mi["col"]) if mi["col"] not in (None, "*") else False
    sev = "medium" if abs(chg) >= 30 else "low"
    if bad and up:
        sev = "high" if abs(chg) >= 30 else "medium"
    xs = s["__bucket"].astype(str).tolist()
    per_step = ((y[-1] / y[0]) ** (1 / (len(y) - 1)) - 1) * 100 if y[0] > 0 and y[-1] > 0 else None
    det = [f"{_label(mi)} {'rose' if up else 'fell'} from about {_f(a, mi)} (first 3 {grain}s) to {_f(b, mi)} (last 3 {grain}s): **{pct(chg)}**.",
           f"The change is steady (straight-line fit explains {r2:.0%} of the movement)" + (f"; about {per_step:+.2f}% per {grain} compounded." if per_step is not None else ".")]
    return [_mk("trend", f"{_label(mi)} {'rising' if up else 'falling'} steadily ({pct(chg, 0)} over {len(y)} {grain}s)", det, sev, abs(chg) / 20, t, mi,
                period=f"{xs[0]} to {xs[-1]}", chart={"type": "line", "x": xs, "series": [{"name": _label(mi), "data": [round(v, 2) for v in y]}]})]


def seasonality(t, mi, s: pd.DataFrame) -> list[dict]:
    if len(s) < 24:
        return []
    y = pd.Series(s["v"].to_numpy(dtype=float), index=s["__bucket"].astype(str))
    ma = y.rolling(12, center=True, min_periods=12).mean()
    ratio = (y / ma).dropna()
    if len(ratio) < 12:
        return []
    moy = ratio.groupby(ratio.index.str[5:7]).mean()
    if len(moy) < 12:
        return []
    amp = moy.max() - moy.min()
    if amp < 0.12:
        return []
    names = {f"{i:02d}": n for i, n in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
    hi = moy.sort_values(ascending=False).index[:2].tolist()
    lo = moy.sort_values().index[:2].tolist()
    det = [f"Every year {_lc(_label(mi))} is highest in **{', '.join(names[m] for m in hi)}** ({(moy[hi[0]] - 1) * 100:+.0f}% vs the underlying trend) "
           f"and lowest in **{', '.join(names[m] for m in lo)}** ({(moy[lo[0]] - 1) * 100:+.0f}%).",
           f"Plan capacity, stock and targets month by month - a flat monthly target would look like a miss in the low months."]
    return [_mk("seasonality", f"Seasonal pattern in {_lc(_label(mi))} (swing of about {amp * 100:.0f}%)", det, "low", amp * 3, t, mi,
                chart={"type": "bar", "x": [names[m] for m in moy.index], "series": [{"name": "Index vs trend (1 = normal)", "data": [round(v, 3) for v in moy.values]}]})]


def step_change(t, mi, s: pd.DataFrame, member: str = "") -> list[dict]:
    y = s["v"].to_numpy(dtype=float)
    n = len(y)
    if n < 8:
        return []
    best = None
    x = np.arange(n)
    lin = np.polyval(np.polyfit(x, y, 1), x)
    sse_lin = ((y - lin) ** 2).sum()
    for k in range(3, n - 2):
        a, b = y[:k], y[k:]
        v = np.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b)) if len(a) > 1 and len(b) > 1 else 0
        if v == 0:
            tstat = np.inf if a.mean() != b.mean() else 0
        else:
            tstat = abs(b.mean() - a.mean()) / v
        sse = ((a - a.mean()) ** 2).sum() + ((b - b.mean()) ** 2).sum()
        if best is None or tstat > best[0]:
            best = (tstat, k, a.mean(), b.mean(), sse)
    if not best:
        return []
    tstat, k, ma, mb, sse = best
    rel = (mb / ma - 1) * 100 if ma else np.nan
    if tstat < 6 or not np.isfinite(rel) or abs(rel) < 4 or sse > 0.5 * sse_lin:
        return []
    xs = s["__bucket"].astype(str).tolist()
    who = f"{member}: " if member else ""
    det = [f"{who}{_label(mi)} averaged {_f(ma, mi)} up to {xs[k - 1]} and {_f(mb, mi)} from **{xs[k]}** onwards (**{pct(rel)}**).",
           "The jump happens at one point and then stays - typical of a price/tariff revision, a new site, a process change or a metering change."]
    return [_mk("step_change", f"{who}step change in {_lc(_label(mi))} from {xs[k]} ({pct(rel, 1)})", det, "medium" if abs(rel) >= 10 else "low",
                abs(rel) / 10, t, mi, entity=member, period=xs[k],
                chart={"type": "line", "x": xs, "series": [{"name": _label(mi), "data": [round(v, 3) for v in y]}]})]


def member_jumps(t, mi, s: pd.DataFrame, dim: str, grain: str) -> list[dict]:
    out = []
    if grain != "month":
        return out
    bad_hi = bad_direction(t, mi["col"]) if mi["col"] not in (None, "*") else False
    last_b = s["__bucket"].max()
    for mem, g in s.groupby(dim):
        g = g.sort_values("__bucket")
        if len(g) < 7 or g["__bucket"].iloc[-1] != last_b:
            continue
        y = g["v"].to_numpy(dtype=float)
        hist = y[-7:-1]
        med = np.median(hist)
        mad = np.median(np.abs(hist - med)) * 1.4826 or (abs(med) * 0.02 + 1e-9)
        for lag in (1, 2):
            v = y[-lag]
            z = (v - med) / mad
            chg = (v / med - 1) * 100 if med else np.nan
            if abs(z) >= 4 and np.isfinite(chg) and abs(chg) >= 20:
                up = chg > 0
                sev = "high" if (up == bad_hi or not up and not bad_hi) and abs(chg) >= 30 else "medium"
                xs = g["__bucket"].astype(str).tolist()
                det = [f"{_label(mi)} for **{mem}** was {_f(v, mi)} in {xs[-lag]} against a usual {_f(med, mi)} (median of the 6 {grain}s before): **{pct(chg)}**.",
                       ("It stayed at the new level in the latest period." if lag == 2 and abs(y[-1] / med - 1) * 100 >= 15 else "Check what changed for this one item.")]
                out.append(_mk("member_change", f"{mem}: {_lc(_label(mi))} {'jumped' if up else 'dropped'} {pct(abs(chg), 0, False)} in {xs[-lag]}", det, sev,
                               abs(chg) / 15, t, mi, entity=str(mem), period=xs[-lag],
                               chart={"type": "line", "x": xs[-18:], "series": [{"name": str(mem), "data": [round(x, 2) for x in y[-18:]]}]}))
                break
    return out


def member_drift(t, mi, s: pd.DataFrame, dim: str, grain: str) -> list[dict]:
    out = []
    bad_hi = bad_direction(t, mi["col"]) if mi["col"] not in (None, "*") else False
    if mi["agg"] not in ("ratio", "mean"):
        return out
    overall = s.groupby("__bucket")["v"].median()
    for mem, g in s.groupby(dim):
        g = g.sort_values("__bucket").tail(8)
        if len(g) < 5:
            continue
        y = g["v"].to_numpy(dtype=float)
        steps = np.diff(y)
        worse = (steps > 0) if bad_hi else (steps < 0)
        chg = (y[-1] / y[0] - 1) * 100 if y[0] else np.nan
        peers = overall.reindex(g["__bucket"]).to_numpy(dtype=float)
        peer_chg = (peers[-1] / peers[0] - 1) * 100 if peers[0] else 0
        if worse.mean() >= 0.8 and np.isfinite(chg) and abs(chg - peer_chg) >= 4 and ((chg > 0) == bad_hi):
            xs = g["__bucket"].astype(str).tolist()
            det = [f"{_label(mi)} for **{mem}** got worse in {int(worse.sum())} of the last {len(steps)} {grain}s: {_f(y[0], mi)} ({xs[0]}) -> {_f(y[-1], mi)} ({xs[-1]}), {pct(chg)}.",
                   f"Others moved {pct(peer_chg)} over the same time, so this is specific to {mem}."]
            out.append(_mk("deterioration", f"{mem}: {_lc(_label(mi))} getting worse every {grain} ({pct(chg, 1)})", det, "high" if abs(chg) >= 8 else "medium",
                           abs(chg) / 5, t, mi, entity=str(mem), period=f"{xs[0]} to {xs[-1]}",
                           chart={"type": "line", "x": xs, "series": [{"name": str(mem), "data": [round(v, 3) for v in y]}, {"name": "Median of all", "data": [round(v, 3) for v in peers]}]}))
    return out


def abnormal_windows(t, df: pd.DataFrame, mi, dim: str | None) -> list[dict]:
    """Runs of >= 3 abnormal days (or hours) per member, e.g. a leak or a meter fault."""
    if t.entry.get("grain") not in ("day", "hour", "minute") or not is_panel(t, dim):
        return []
    grain = "hour" if t.entry.get("grain") in ("hour", "minute") and mi["agg"] in ("mean", "ratio") else "day"
    s = _series(t, df, mi, grain, dim)
    out = []
    bad_hi = bad_direction(t, mi["col"]) if mi["col"] not in (None, "*") else None
    groups = s.groupby(dim) if dim else [("all", s)]
    for mem, g in groups:
        g = g.sort_values("__bucket")
        y = g["v"].to_numpy(dtype=float)
        if len(y) < 20:
            continue
        med = np.median(y)
        mad = np.median(np.abs(y - med)) * 1.4826
        if mad <= 0:
            continue
        z = (y - med) / mad
        for sign in ((1,) if bad_hi else (-1,) if bad_hi is False and mi["agg"] in ("mean", "ratio") and "pressure" not in str(mi["col"]) else (1, -1)):
            flag = (z * sign) >= 3.5
            runs, i = [], 0
            while i < len(flag):
                if flag[i]:
                    j = i
                    while j + 1 < len(flag) and (flag[j + 1] or (j + 2 < len(flag) and flag[j + 2]) or (j + 3 < len(flag) and flag[j + 3])):
                        j += 1
                    if j - i + 1 >= 3:
                        runs.append((i, j))
                    i = j + 1
                else:
                    i += 1
            for i, j in runs[:2]:
                xs = g["__bucket"].astype(str).tolist()
                avg = float(np.mean(y[i:j + 1]))
                chg = (avg / med - 1) * 100 if med else np.nan
                det = [f"{_label(mi)}{' for **' + str(mem) + '**' if dim else ''} was abnormal from **{xs[i]}** to **{xs[j]}** ({j - i + 1} {grain}s): "
                       f"average {_f(avg, mi)} vs a normal {_f(med, mi)} ({pct(chg)})."]
                extra = {}
                if mi["agg"] == "ratio" and mi.get("ratio"):
                    num, den, fac = mi["ratio"]["num"], mi["ratio"]["den"], mi["ratio"]["factor"]
                    w = df.copy()
                    ts, month = E.time_parts(t, w)
                    w["__bucket"] = E.bucket(ts, month, grain)
                    w = w[(w["__bucket"] >= xs[i]) & (w["__bucket"] <= xs[j])]
                    if dim:
                        w = w[w[dim].astype(str) == str(mem)]
                    excess = float(w[num].sum() - med / fac * w[den].sum())
                    ncol = t.cols.get(num, {})
                    if excess > 0:
                        det.append(f"Excess {ncol.get('label', num).lower()} over the window: about **{fmt_value(excess, ncol.get('unit', ''), ncol.get('unit_kind', ''), 0)}** above the normal rate.")
                        extra = {"excess": excess, "excess_col": num}
                det.append("A sudden, sustained change in one place usually means a physical fault (leak, meter, equipment) or a recording problem - investigate on site.")
                lo, hi = max(0, i - 10), min(len(xs), j + 11)
                out.append(_mk("anomaly_window", f"{mem if dim else _label(mi)}: abnormal {_lc(_label(mi))} {xs[i]} to {xs[j]}", det,
                               "high" if abs(chg) >= 50 or (j - i + 1) >= 7 else "medium", min(10, abs(chg) / 10 + (j - i + 1) / 5), t, mi,
                               entity=str(mem) if dim else "", period=f"{xs[i]} to {xs[j]}",
                               chart={"type": "line", "x": xs[lo:hi], "series": [{"name": str(mem), "data": [round(v, 3) for v in y[lo:hi]]}], "band": {"from": xs[i], "to": xs[j]}},
                               extra=extra))
    return out


def peer_outliers(t, df: pd.DataFrame, mi, dim: str) -> list[dict]:
    """A member far from its peers over the whole period (rates, averages, or per-record counts)."""
    d = df.copy()
    keys = [dim]
    if mi["agg"] in ("ratio", "mean"):
        r = E.aggregate(d, keys, mi, None).dropna()
        basis = "rate"
    elif mi["agg"] in ("sum",) and is_event_table(t) and len(d) / max(d[dim].nunique(), 1) >= 5:
        r = d.groupby(dim)[mi["col"]].sum() / d.groupby(dim).size()
        basis = "per record"
    else:
        return []
    if len(r) < 4:
        return []
    out = []
    for mem, v in r.items():
        others = r.drop(mem)
        med = others.median()
        mad = (others - med).abs().median() * 1.4826 or abs(med) * 0.05 or 1e-9
        z = (v - med) / mad
        rel = (v / med - 1) * 100 if med else np.nan
        if abs(z) >= 3.5 and np.isfinite(rel) and abs(rel) >= 25:
            bad_hi = bad_direction(t, mi["col"])
            worse = (rel > 0) == bad_hi
            tot = f" (total {_f(float(d.loc[d[dim] == mem, mi['col']].sum()), mi, 0)})" if basis == "per record" else ""
            det = [f"**{mem}**: {_f(v, mi)} {basis if basis != 'rate' else ''}{tot} vs {_f(med, mi)} for a typical {t.cols.get(dim, {}).get('label', dim).lower()} ({pct(rel, 0)}).".replace("  ", " "),
                   ("This is the worst in the group." if worse else "This is far better than the rest - check whether it is real or a recording issue.")]
            out.append(_mk("peer_outlier", f"{mem} stands out on {_lc(_label(mi))} ({pct(rel, 0)} vs peers)", det, "high" if worse and abs(rel) >= 100 else "medium" if worse else "low",
                           min(10, abs(rel) / 25), t, mi, entity=str(mem),
                           chart={"type": "bar", "x": [str(k) for k in r.sort_values(ascending=False).index[:15]],
                                  "series": [{"name": _label(mi) + (" per record" if basis == "per record" else ""), "data": [round(x, 3) for x in r.sort_values(ascending=False).values[:15]]}],
                                  "highlight": [str(mem)]}))
    return out


def concentration(t, df, mi, dim: str) -> list[dict]:
    if mi["agg"] not in ("sum", "count"):
        return []
    r = E.aggregate(df, [dim], mi, None).dropna().sort_values(ascending=False)
    if len(r) < 4 or r.sum() <= 0:
        return []
    sh = r / r.sum() * 100
    if sh.iloc[0] < 40:
        return []
    det = [f"**{r.index[0]}** accounts for **{sh.iloc[0]:.1f}%** of {_lc(_label(mi))}; the next is {r.index[1]} with {sh.iloc[1]:.1f}%.",
           "Results depend heavily on this one item - track it closely."]
    return [_mk("concentration", f"{r.index[0]} makes up {sh.iloc[0]:.0f}% of {_lc(_label(mi))}", det, "low", sh.iloc[0] / 40, t, mi, entity=str(r.index[0]),
                chart={"type": "pie", "x": [str(k) for k in r.index[:8]], "series": [{"name": _label(mi), "data": [round(x, 2) for x in r.values[:8]]}]})]


def new_members(t, s: pd.DataFrame, dim: str, mi) -> list[dict]:
    out = []
    allb = sorted(s["__bucket"].unique())
    if len(allb) < 6 or not is_panel(t, dim):
        return out
    first, last = allb[0], allb[-1]
    for mem, g in s.groupby(dim):
        b0, b1 = g["__bucket"].min(), g["__bucket"].max()
        span = allb.index(b1) - allb.index(b0) + 1
        if g["__bucket"].nunique() < 0.8 * span:
            continue
        if allb.index(b0) >= 2:
            out.append(_mk("new_member", f"New: {mem} appears from {b0}", [f"**{mem}** has data only from {b0} onwards. Growth figures that include it are not like-for-like."],
                           "low", 0.8, t, mi, entity=str(mem), period=str(b0)))
        if allb.index(b1) <= len(allb) - 3:
            out.append(_mk("stopped_member", f"{mem} has no data after {b1}", [f"**{mem}** stopped appearing after {b1} (data runs to {last}). Check whether it closed or the feed stopped."],
                           "medium", 1.5, t, mi, entity=str(mem), period=str(b1)))
    return out


def count_spikes(t, df, dim: str, grain: str) -> list[dict]:
    if grain != "month":
        return []
    mi = E.measure_info(t, {"col": "*", "agg": "count"})
    s = _series(t, df, mi, grain, dim)
    out = []
    for mem, g in s.groupby(dim):
        g = g.sort_values("__bucket")
        y = g["v"].to_numpy(dtype=float)
        if len(y) < 6:
            continue
        med = np.median(y)
        if med < 10:
            continue
        hot = y >= 2 * med
        idxs = np.where(hot)[0]
        if hot.any() and hot.sum() <= 3 and (idxs.max() - idxs.min()) <= 3:
            xs = g["__bucket"].astype(str).tolist()
            idx = np.where(hot)[0]
            ratio = y[idx].mean() / med
            det = [f"{_label(mi)} for **{mem}** were **{ratio:.1f}x** the usual level in {', '.join(xs[i] for i in idx)} ({', '.join(group(y[i]) for i in idx)} vs a usual {group(med)} per {grain}).",
                   "Look for what changed just before (a new system, a policy, an outage)."]
            out.append(_mk("spike", f"Spike in {_lc(_label(mi))} for {mem} ({ratio:.1f}x normal)", det, "high" if ratio >= 3 else "medium", ratio, t, mi,
                           entity=str(mem), period=", ".join(xs[i] for i in idx),
                           chart={"type": "bar", "x": xs, "series": [{"name": str(mem), "data": [float(v) for v in y]}], "highlight": [xs[i] for i in idx]}))
    return out


PLAN_W = re.compile(r"(planned|plan|target|budget|scheduled|expected|estimate|standard)")
ACT_W = re.compile(r"(actual|achieved|real|taken|spent|done)")


def plan_actual_pairs(t) -> list[tuple[str, str]]:
    ms = t.measures()
    out = []
    for a in ms:
        if not ACT_W.search(a):
            continue
        base = PLAN_W.sub("", ACT_W.sub("", a)).strip("_")
        for p in ms:
            if p != a and PLAN_W.search(p) and PLAN_W.sub("", p).strip("_") == base:
                out.append((a, p))
    return out


def plan_gaps(t, df: pd.DataFrame, dim: str | None) -> list[dict]:
    out = []
    for a, p in plan_actual_pairs(t):
        d = df.copy()
        d["__gap"] = d[a] - d[p]
        lab = t.cols[a].get("label", a)
        plab = t.cols[p].get("label", p)
        mi = {"col": "__gap", "agg": "mean", "label": f"{lab} over {_lc(plab)}", "unit": t.cols[a].get("unit", ""), "unit_kind": t.cols[a].get("unit_kind", "")}
        on_time = float(((d[a] <= d[p] * 1.10)).mean() * 100)
        det = [f"On average {_lc(lab)} is **{_f(d['__gap'].mean(), mi)}** above {_lc(plab)}; {on_time:.1f}% of records are within 10% of plan."]
        if dim:
            r = d.groupby(dim)["__gap"].mean().sort_values(ascending=False)
            if len(r) >= 3:
                med = r.median()
                det.append(f"Worst {t.cols[dim].get('label', dim).lower()}: **{r.index[0]}** at {_f(r.iloc[0], mi)} above plan per record (typical {_f(med, mi)}).")
                if r.iloc[0] > 0 and (r.iloc[0] - med) > 2 * max(abs(med), 1):
                    steady = d[d[dim] == r.index[0]]["__gap"]
                    det.append("The gap is steady across records, so the plan for it is probably under-estimated (re-baseline it)." if steady.std() < abs(steady.mean())
                               else "The gap varies a lot - look for specific incidents.")
                out.append(_mk("plan_gap", f"{r.index[0]}: {_lc(lab)} {_f(r.iloc[0], mi)} above {_lc(plab)} on average", det, "medium", min(8, r.iloc[0] / max(abs(med), 1)), t,
                               {"col": a, "label": lab, "unit": mi["unit"], "unit_kind": mi["unit_kind"], "agg": "mean"}, entity=str(r.index[0]),
                               chart={"type": "bar", "x": [str(k) for k in r.index[:15]], "series": [{"name": f"{lab} over plan", "data": [round(v, 2) for v in r.values[:15]]}], "highlight": [str(r.index[0])]}))
    return out


def quality(t) -> list[dict]:
    out = []
    for ch in t.entry.get("validation", []):
        if ch["status"] in ("warn", "fail"):
            sev = "medium" if ch["status"] == "fail" or re.search(r"label|unreadable|missing", ch["detail"]) else "low"
            out.append(_mk("data_quality", f"Data quality - {t.label}: {ch['check']}", [ch["detail"]], sev, 1.0 if sev == "medium" else 0.5, t, None, entity=ch["check"]))
    for n in t.entry.get("notes", []):
        if "transform" in n:
            out.append(_mk("data_quality", f"Data layout fixed - {t.label}", [f"The file needed reshaping before use ({n}). Ask the source team for a row-per-record export."], "low", 0.4, t, None))
    return out


# ------------------------------------------------------------------------------------- runner
def run_insights(store, scope: str = "all") -> list[dict]:
    with _lock:
        key = (store.version, id(store))
        if key in _cache:
            fs = _cache[key]
        else:
            fs = []
            for t in store.usable():
                try:
                    fs += _table_insights(t)
                except Exception as ex:  # one table never stops the rest
                    fs.append(_mk("data_quality", f"Could not analyse {t.label}", [f"{type(ex).__name__}: {str(ex)[:150]}"], "low", 0.1, t, None))
            seen, uniq, win = set(), [], set()
            for f in sorted(fs, key=lambda f: (-(SEV_W[f["severity"]] * f["impact"]))):
                if f["id"] in seen:
                    continue
                if f["category"] == "anomaly_window":
                    wk = (f["table"], f["entity"], f["period"][:7])
                    if wk in win:
                        continue
                    win.add(wk)
                seen.add(f["id"])
                uniq.append(f)
            fs = uniq[:80]
            _cache.clear()
            _cache[key] = fs
    if scope == "trends":
        return [f for f in fs if f["category"] in ("trend", "seasonality", "step_change", "new_member")]
    if scope == "anomalies":
        return [f for f in fs if f["category"] in ("anomaly_window", "member_change", "spike", "peer_outlier", "deterioration", "stopped_member")]
    if scope == "quality":
        return [f for f in fs if f["category"] == "data_quality"]
    return fs


def _table_insights(t) -> list[dict]:
    out = []
    df = t.df
    grain = _grain_for(t)
    dims = _dims(t)
    ms = _measures(t)
    out += quality(t)
    out += plan_gaps(t, df, dims[0] if dims else None)
    if grain:
        for mi in ms:
            s = _series(t, df, mi, grain)
            if mi["agg"] != "count" or not dims:
                out += trend(t, mi, s, grain)
                if grain == "month":
                    out += seasonality(t, mi, s)
                if mi["agg"] in ("ratio", "mean"):
                    out += step_change(t, mi, s)
            for dim in dims[:2]:
                sd = _series(t, df, mi, grain, dim)
                if mi["agg"] != "count":
                    out += member_jumps(t, mi, sd, dim, grain)
                    out += member_drift(t, mi, sd, dim, grain)
                if mi is ms[0]:
                    out += new_members(t, sd, dim, mi)
            if mi["agg"] != "count":
                out += abnormal_windows(t, df, mi, dims[0] if dims else None)
        if any(m["agg"] == "count" for m in ms):
            for dim in dims[:2]:
                sp = sorted(count_spikes(t, df, dim, grain), key=lambda f: -f["impact"])
                if len(sp) > 3:
                    rest = sp[3:]
                    sp = sp[:3] + [_mk("spike", f"{len(rest)} more {t.cols[dim].get('label', dim).lower()} values also spiked", [", ".join(f"{f['entity']} ({f['impact']:.1f}x)" for f in rest)],
                                       "low", 0.5, t, None, entity="others")]
                out += sp
    for mi in ms:
        if mi["agg"] == "count":
            continue
        for dim in dims[:2]:
            out += peer_outliers(t, df, mi, dim)
            if mi is ms[0]:
                out += concentration(t, df, mi, dim)
    return out


def insights_answer(store, q: str) -> dict:
    fs = run_insights(store)
    ql = q.lower()
    pick = [f for f in fs if any(w in ql for w in re.findall(r"[a-z]{4,}", (f["table_label"] + " " + (f["measure_label"] or "") + " " + f["entity"]).lower()))]
    use = (pick or [f for f in fs if f["category"] != "data_quality"] or fs)[:10]
    if not use:
        return result("insights", "Nothing unusual found", ["I ran trend, seasonality, step-change, anomaly and peer checks on every table and found nothing that stands out."])
    ans = [f"I checked every table for trends, seasonal patterns, sudden changes, abnormal periods, outliers and data problems. **{len(fs)} finding(s)**; the top {len(use)}:"]
    for i, f in enumerate(use, 1):
        ans.append(f"{i}. **{f['title']}** - {f['detail'][0]}")
    rows = [[f["severity"], f["title"], f["table_label"], f["period"] or "-"] for f in use]
    ch = next((f["chart"] for f in use if f.get("chart")), None)
    return result("insights", "What stands out in your data", ans, table={"columns": ["Severity", "Finding", "Table", "Period"], "rows": rows, "caption": "Top findings", "total_rows": len(rows)},
                  chart={**ch, "title": use[0]["title"]} if ch else None, sources=[{"file": f["source"], "origin": f["category"]} for f in use[:3]],
                  followups=["What should we act on first?", "Show all insights about anomalies"])
