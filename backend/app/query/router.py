"""ask(): one entry point for every chat message.

Order: guardrails -> no data -> help -> special requests (reports, decks, alerts, insights, actions, audit, documents)
-> follow-up to the previous answer -> rules plan -> LLM plan -> clarify. Numbers always come from engine.run()."""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime

from .. import llm
from ..config import STATE_DIR
from ..store import STORE, examples
from ..util import clean
from ..verify import allowed_numbers, numbers_in, verify_text
from . import engine, guardrails
from .answer import compose, result
from .understand import followup, from_llm, llm_plan, normalise, rules_plan

SESSIONS: dict[str, dict] = {}
_slock = threading.Lock()
LOG = STATE_DIR / "query_log.jsonl"

R_REPORT = re.compile(r"\b(report|mis|infographic|one[- ]?pager|pdf|word|docx)\b")
R_MAKE = re.compile(r"\b(make|create|generate|prepare|build|produce|draft|export|give me|send|banao|bana do|write)\b")
R_DECK = re.compile(r"\b(slides?|deck|presentation|ppt|pptx|powerpoint)\b")
R_DECK_EDIT = re.compile(r"\b(change|replace|convert|swap|add|remove|delete|rename|move|make)\b.*\b(slide|chart|deck|title)\b|\bslide \d+\b")
R_REPORT_EDIT = re.compile(r"\b(redo|regenerate|re-?run|update|change|make|add|remove|drop)\b.*\b(report|section|crore|lakh)\b|\bin (rs )?(crore|lakh)s?\b|\badd (a )?section\b")
R_ALERT = re.compile(r"\b(alert me|notify me|tell me when|warn me|let me know (when|if)|send (me )?an alert|set (up )?an alert|subscribe)\b")
R_INSIGHT = re.compile(r"\b(unusual|anomal\w*|insights?|patterns?|interesting|stand(s)? out|what changed|surpris\w*|outliers?|red flags?|what do you see|kuch ajeeb)\b")
R_ACTION = re.compile(r"\b(act on|actions?|prioriti[sz]e|top (issues|problems|risks)|biggest (problems|issues|risks)|recommend\w*|what should (we|i) do|focus on first|kya karna chahiye)\b")
R_WHATIF = re.compile(r"\bwhat if\b|\bif\b.{0,60}\b(increases?|decreases?|rises?|falls?|drops?|grows?|goes up|goes down|is cut|is raised|changes?)\b.{0,30}\d")
R_PLAN = re.compile(r"\b(budget|target|plan(ned)?)\b.*\b(actual|achiev\w*|vs|versus|against|miss\w*|met|meet)\b|\b(actual|achiev\w*|meet|met|miss|missed|beat|hit)\b.*\b(budget|target|plan)\b")
R_AUDIT = re.compile(r"\b(check|audit|verify|validate|find (the )?(errors?|mistakes)|review|cross[- ]?check)\b.*\b(draft|manual|my report|summary sheet|excel|spreadsheet|uploaded report)\b")
R_FACT = re.compile(r"\b(check|verify|reconcile|match|tally|correct|accurate|right)\b.*\b(document|pdf|report|note|word file|mis)\b|\b(document|pdf|report|note|mis)\b.*\b(match|tally|agree|correct|accurate|right)\w*\b")
R_DOC = re.compile(r"\b(document|pdf|board note|the note|report says|according to|mentioned|policy|minutes|circular|letter|memo|says about|what does the)\b")
R_META = re.compile(r"\b(what data|which (tables|files|data)|what (can|could|should) i ask|what is loaded|data (is )?loaded|data dictionary|what tables|list (the )?tables)\b")
R_HELLO = re.compile(r"^\s*(hi|hello|hey|namaste|namaskar|good (morning|afternoon|evening)|help|what can you do|who are you)\b")


def session(sid: str) -> dict:
    with _slock:
        return SESSIONS.setdefault(sid, {"history": [], "last_plan": None, "last_question": None, "last_report_spec": None, "last_deck_spec": None})


def _finish(res: dict, q: str, sid: str, sess: dict, route: dict, t0: float) -> dict:
    res = clean(res)
    res.setdefault("id", uuid.uuid4().hex[:12])
    res["question"] = q
    res["route"] = route
    res["elapsed_ms"] = int((time.time() - t0) * 1000)
    sess["history"].append({"q": q, "title": res.get("title"), "intent": res.get("intent")})
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), "q": q, "intent": res.get("intent"), "title": res.get("title"),
                                "route": route.get("chosen_by"), "ms": res["elapsed_ms"], "verified": res.get("verified")}) + "\n")
    except Exception:
        pass
    return res


def _narrate(res: dict, q: str) -> None:
    """Optional one-line 'in simple words' summary by the LLM; every number in it must exist in the computed result."""
    from ..config import settings
    if not (settings.llm_narration and llm.available()) or res.get("type") != "answer":
        return
    facts = {"answer": res.get("answer"), "kpis": res.get("kpis"), "table": (res.get("table") or {}).get("rows", [])[:15]}
    t = llm.chat([{"role": "system", "content": "Write ONE short plain-English sentence summarising the answer for a manager. Use only numbers that appear in the facts, "
                                                 "written the same way. No advice, no speculation."},
                  {"role": "user", "content": f"Question: {q}\nFacts: {json.dumps(clean(facts))[:3500]}"}], max_tokens=120, purpose="narration")
    if not t:
        return
    ok, bad = verify_text(t, allowed_numbers(res.get("answer"), res.get("kpis"), res.get("table"), res.get("facts")), numbers_in(q))
    if ok:
        res["summary"] = t.strip()
        res["narration"] = "summary checked: every number matches"
    else:
        res["narration"] = f"LLM summary dropped (numbers not in the result: {', '.join(map(str, bad[:3]))})"


def _help() -> dict:
    caps = []
    for t in STORE.usable()[:8]:
        ms = [t.cols[m]["label"] for m in t.measures()[:3]]
        caps.append(f"**{t.label}**: " + (", ".join(ms) if ms else "counts") + (f" by {', '.join(t.cols[d]['label'] for d in t.dims()[:3])}" if t.dims() else ""))
    ans = ["I answer questions from **your loaded data only**, in English or Hinglish, with numbers you can trace to the source rows."]
    ans += ([f"Loaded now: {c}" for c in caps] if caps else ["No data is loaded yet - upload files on the Pipeline page."])
    ans.append("I can also: build MIS reports (PDF/Word) and one-page infographics, make PowerPoint decks, find unusual patterns, rank what to act on, "
               "run what-if checks, set alerts, check a manual Excel report cell by cell, and answer from PDF/Word documents with citations.")
    return result("help", "Hello! I'm SAHAYAK-AI", ans, followups=[i for g in examples(STORE)[:2] for i in g["items"][:2]])


def _meta() -> dict:
    rows = [[t.label, t.key, len(t.df), t.entry.get("grain", ""), " to ".join(str(x)[:10] for x in (t.entry.get("time_range") or [])),
             ", ".join(t.cols[m]["label"] for m in t.measures()[:5]), ", ".join(t.cols[d]["label"] for d in t.dims()[:5]), t.status()] for t in STORE.tables.values()]
    ans = [f"**{len(STORE.tables)} table(s)** and **{len(STORE.documents)} document(s)** are loaded."]
    if STORE.links:
        ans.append("Linked tables: " + "; ".join(l["text"] for l in STORE.links[:5]) + ".")
    return result("data_overview", "What is loaded", ans, table={"columns": ["Table", "Key", "Rows", "Time grain", "Covers", "Numbers", "Groupings", "Status"], "rows": rows,
                                                               "caption": "Data catalogue", "total_rows": len(rows)},
                  followups=[i for g in examples(STORE)[:1] for i in g["items"][:3]])


def _clarify(q: str, msg: str | None = None, options: list[str] | None = None) -> dict:
    opts = options or [i for g in examples(STORE)[:3] for i in g["items"][:2]]
    return result("clarify", "Could you say a bit more?", [msg or "I could not tell which number you want from the loaded data."], type="clarify",
                  clarify={"question": "Did you mean one of these?", "options": opts[:6]})


def ask(question: str, sid: str = "default") -> dict:
    t0 = time.time()
    q = (question or "").strip()
    sess = session(sid)
    route = {"chosen_by": None, "rules_confidence": None}
    if not q:
        return _finish(_clarify(q, "Please type a question."), q, sid, sess, route, t0)
    g = guardrails.check(q, STORE if not STORE.is_empty() else None)
    if g:
        route["chosen_by"] = "guardrail"
        return _finish(result(g["intent"], g["title"], g["answer"], type=g["type"], followups=g.get("followups", [])), q, sid, sess, route, t0)
    qn = normalise(q)
    if R_HELLO.search(qn):
        route["chosen_by"] = "help"
        return _finish(_help(), q, sid, sess, route, t0)
    if STORE.is_empty():
        route["chosen_by"] = "no-data"
        return _finish(result("no_data", "No data loaded yet", ["There is no data in SAHAYAK-AI yet, so there is nothing to compute from.",
                                                                 "Open **Pipeline** and upload your files (CSV, Excel, database, JSON, PDF, Word). Each file is checked, cleaned and loaded in 7 visible stages - then ask again."],
                              type="clarify"), q, sid, sess, route, t0)
    try:
        res = _special(q, qn, sess, route)
        if res is None:
            res = _analyse(q, qn, sess, route)
    except engine.PlanError as e:
        res = result("query", "Could not compute that", [str(e)], type="clarify", followups=[i for g in examples(STORE)[:2] for i in g["items"][:2]])
    except Exception as e:  # never crash the chat
        import traceback
        traceback.print_exc()
        res = result("error", "Something went wrong", [f"I hit an error while working on this ({type(e).__name__}: {str(e)[:160]}).", "Please rephrase, or check the table on the Data page."],
                     type="clarify")
    if res.get("type") == "answer" and res.get("intent") == "query":
        _narrate(res, q)
    return _finish(res, q, sid, sess, route, t0)


def _special(q: str, qn: str, sess: dict, route: dict) -> dict | None:
    if R_AUDIT.search(qn):
        from ..audit.checker import audit_latest
        route["chosen_by"] = "audit"
        return audit_latest(STORE)
    if R_META.search(qn):
        route["chosen_by"] = "catalogue"
        return _meta()
    if sess.get("last_deck_spec") and R_DECK_EDIT.search(qn) and not re.search(r"\b(make|create|build|prepare)\b.*\b\d+\s+slides\b", qn):
        from ..decks.builder import edit_deck
        route["chosen_by"] = "deck-edit"
        out = edit_deck(STORE, sess["last_deck_spec"], q)
        if out.get("deck_spec"):
            sess["last_deck_spec"] = out.pop("deck_spec")
        return out
    if R_DECK.search(qn) and (R_MAKE.search(qn) or re.search(r"\b\d+\s+slides\b", qn)):
        from ..decks.builder import deck_from_request
        route["chosen_by"] = "deck"
        out = deck_from_request(STORE, q, sess)
        if out.get("deck_spec"):
            sess["last_deck_spec"] = out.pop("deck_spec")
        return out
    if sess.get("last_report_spec") and R_REPORT_EDIT.search(qn) and not R_DECK.search(qn):
        from ..reports.builder import edit_report
        route["chosen_by"] = "report-edit"
        out = edit_report(STORE, sess["last_report_spec"], q)
        if out.get("report_spec"):
            sess["last_report_spec"] = out.pop("report_spec")
        return out
    if R_REPORT.search(qn) and R_MAKE.search(qn):
        from ..reports.builder import report_from_request
        route["chosen_by"] = "report"
        out = report_from_request(STORE, q)
        if out.get("report_spec"):
            sess["last_report_spec"] = out.pop("report_spec")
        return out
    if R_ALERT.search(qn):
        from ..alerts.engine import rule_from_question
        route["chosen_by"] = "alert"
        return rule_from_question(STORE, q)
    if R_FACT.search(qn) and STORE.documents:
        from .docs import fact_check
        route["chosen_by"] = "fact-check"
        return fact_check(STORE, q)
    if R_WHATIF.search(qn):
        from ..decision.support import what_if
        route["chosen_by"] = "what-if"
        return what_if(STORE, q)
    if R_PLAN.search(qn):
        from ..decision.support import plan_vs_actual
        route["chosen_by"] = "plan-vs-actual"
        r = plan_vs_actual(STORE, q)
        if r:
            return r
    if R_ACTION.search(qn):
        from ..decision.support import top_actions
        route["chosen_by"] = "actions"
        return top_actions(STORE, q)
    if R_INSIGHT.search(qn) and not re.search(r"\b(total|sum|average|by)\b", qn):
        from ..insights.engine import insights_answer
        route["chosen_by"] = "insights"
        return insights_answer(STORE, q)
    if STORE.documents and (R_DOC.search(qn) or any(w in qn for d in STORE.documents for w in [d.name.lower().rsplit(".", 1)[0]])):
        rp = rules_plan(q, STORE)
        if rp.get("confidence", 0) < 0.75:
            from .docs import doc_answer
            route["chosen_by"] = "documents"
            return doc_answer(STORE, q)
    return None


def _analyse(q: str, qn: str, sess: dict, route: dict) -> dict:
    plan = None
    if sess.get("last_plan"):
        fp = followup(q, sess["last_plan"], STORE)
        rp0 = rules_plan(q, STORE)
        from .understand import FOLLOW_RE
        if fp and (FOLLOW_RE.search(qn) or rp0.get("confidence", 0) < 0.6):
            plan = fp
            route["chosen_by"] = "follow-up"
    if plan is None:
        rp = rules_plan(q, STORE)
        route["rules_confidence"] = rp.get("confidence")
        if rp.get("plan") and rp["confidence"] >= 0.6:
            plan = rp["plan"]
            route["chosen_by"] = "rules"
        elif llm.available():
            obj = llm_plan(q, STORE, [c["table"] for c in rp.get("candidates", [])][:4], {"question": sess.get("last_question")})
            lp, err = from_llm(obj, STORE)
            route["llm"] = err or "ok"
            if lp:
                plan = lp
                route["chosen_by"] = "llm"
            elif err.startswith("clarify:"):
                return _clarify(q, err[8:])
        if plan is None and rp.get("plan") and rp["confidence"] >= 0.35:
            plan = rp["plan"]
            route["chosen_by"] = "rules (low confidence)"
        if plan is None:
            if STORE.documents:
                from .docs import doc_answer
                d = doc_answer(STORE, q)
                if d.get("type") == "answer":
                    route["chosen_by"] = "documents"
                    return d
            if llm.available():
                from .nl2sql import answer_with_sql
                s = answer_with_sql(STORE, q)
                if s:
                    route["chosen_by"] = "nl2sql"
                    return s
            route["chosen_by"] = "clarify"
            return _clarify(q)
    x = engine.run(STORE, plan)
    res = compose(STORE, x, q)
    if route["chosen_by"] == "rules (low confidence)":
        res["assumptions"] = ["I was not fully sure what you meant, so I used: " + " | ".join(res.get("plan_text", [])) + ". Rephrase if this is not it."]
    sess["last_plan"] = plan
    sess["last_question"] = q
    return res
