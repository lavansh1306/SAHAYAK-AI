"""Document questions with citations, and checking a document's numbers against the loaded data."""
from __future__ import annotations

import re

import pandas as pd

from .. import llm
from ..documents import bm25_search
from ..verify import numbers_in, verify_text
from .answer import result


def pick_doc(store, q: str):
    ql = q.lower()
    best, bs = None, 0
    for d in store.documents:
        stem = re.sub(r"[^a-z0-9]+", " ", d.name.lower().rsplit(".", 1)[0])
        toks = [w for w in stem.split() if len(w) > 2 and not w.isdigit()]
        s = sum(1 for w in toks if w in ql)
        if s > bs:
            best, bs = d, s
    return best


def doc_answer(store, q: str) -> dict:
    d = pick_doc(store, q)
    hits = bm25_search(store.documents, q, k=5, doc_filter=d.name if d else None) or bm25_search(store.documents, q, k=5)
    if not hits:
        return result("doc_search", "Not found in the documents", ["I could not find this in the loaded documents."], type="clarify")
    if re.search(r"\b(summari[sz]e|summary|overview|gist|key points)\b", q.lower()) and d:
        secs = [(h, b) for h, b in d.sections if b.strip()][:8]
        ans = [f"**{d.name}** has {len(d.sections)} section(s). Key lines:"] + [f"- {h if h != '(preamble)' else 'Opening'}: {re.split(r'(?<=[.!?])\s', b.strip().replace(chr(10), ' '))[0][:220]}" for h, b in secs]
        return result("doc_summary", f"Summary of {d.name}", ans, citations=[{"doc": d.name, "locator": f"section '{h}'", "text": b[:200]} for h, b in secs[:4]],
                      sources=[{"file": d.name, "origin": "document", "filter": "all sections"}])
    ans = [f"From **{h['doc']}** ({h['locator']}): \"{h['text']}\"" for h in hits[:3]]
    out = result("doc_search", "From your documents", ans, citations=hits, sources=[{"file": h["doc"], "origin": h["locator"]} for h in hits[:3]])
    if llm.available():
        ctx = "\n".join(f"[{i + 1}] ({h['doc']}, {h['locator']}) {h['text']}" for i, h in enumerate(hits))
        t = llm.chat([{"role": "system", "content": "Answer the question using ONLY the numbered passages. Quote numbers exactly as written. Cite passages like [1]. "
                                                     "If the passages do not answer it, say 'The documents do not say.' Answer in 1-3 short sentences."},
                      {"role": "user", "content": f"Passages:\n{ctx}\n\nQuestion: {q}"}], max_tokens=220, purpose="doc_answer")
        if t:
            allowed = {round(x, 2) for h in hits for x in numbers_in(h["text"])}
            allowed |= {round(x, 0) for x in allowed} | {float(i) for i in range(1, 6)}
            ok, bad = verify_text(t, allowed)
            if ok:
                out["summary"] = t.strip()
    return out


def fact_check(store, q: str) -> dict:
    """Check every sentence with a number in a document against the data, using the normal question engine."""
    from . import engine
    from .answer import compose
    from .understand import rules_plan
    d = pick_doc(store, q) or (store.documents[0] if store.documents else None)
    if d is None:
        return result("fact_check", "No document loaded", ["Upload the document (PDF or Word) on the Pipeline page first."], type="clarify")
    rows = []
    for p in d.passages:
        s = p.text
        nums = [n for n in numbers_in(s) if not (1900 <= n <= 2100 and float(n).is_integer())]
        if not nums or len(s) > 400:
            continue
        rp = rules_plan(s, store)
        if rp.get("confidence", 0) < 0.55 or not rp.get("plan"):
            continue
        plan = rp["plan"]
        plan["group"], plan["grain"], plan["having"], plan["share"] = [], None, [], False
        plan["compare"] = None
        try:
            x = engine.run(store, plan)
        except Exception:
            continue
        v = x["res"]["m0"].iloc[0] if len(x["res"]) else None
        if v is None or pd.isna(v):
            continue
        hit = None
        for n in nums:
            for dp in (0, 1, 2):
                if round(float(v), dp) == round(n, dp) and (dp > 0 or abs(n) >= 10):
                    hit = n
                    break
            if hit is not None:
                break
        close = min(nums, key=lambda n: abs(n - v) / max(abs(v), 1e-9))
        rel = abs(close - v) / max(abs(v), 1e-9)
        status = "Matches" if hit is not None else ("Differs" if rel < 0.5 else "Not comparable")
        if status == "Not comparable":
            continue
        rows.append({"Statement": s[:160], "Number in document": hit if hit is not None else close, "From data": round(float(v), 2),
                     "How computed": f"{x['measures'][0]['text']}" + (f" ({x['period']['label']})" if x.get("period") else ""), "Result": status, "Where": p.locator})
    if not rows:
        return result("fact_check", f"Checked {d.name}", [f"I could not match any figure in {d.name} to a calculation on the loaded data. The document may describe data that is not loaded."])
    df = pd.DataFrame(rows)
    bad = df[df.Result != "Matches"]
    ans = [f"I checked **{len(df)} figure(s)** in {d.name} that I could recompute from the loaded data: **{len(df) - len(bad)} match**, **{len(bad)} differ**."]
    for r in bad.head(6).itertuples():
        ans.append(f"- \"{r.Statement[:90]}...\": document says {r._2:,}, data gives {r._3:,} ({r._4}).")
    return result("fact_check", f"Numbers in {d.name} vs the data", ans, table={"columns": list(df.columns), "rows": df.values.tolist(), "caption": "Each figure recomputed", "total_rows": len(df)},
                  kpis=[{"label": "Figures checked", "value": len(df), "display": str(len(df))}, {"label": "Differ", "value": len(bad), "display": str(len(bad)), "tone": "bad" if len(bad) else "good"}],
                  sources=[{"file": d.name, "origin": "document"}])
