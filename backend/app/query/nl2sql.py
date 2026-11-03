"""Guarded NL-to-SQL for questions outside the certified glossary (results labelled 'Unverified')."""
from __future__ import annotations

import re

import pandas as pd
import sqlglot
from sqlglot import exp

import threading

import duckdb

from .. import llm
from .answer import result
_DUCK_LOCK = threading.Lock()

BLOCKED_FUNCS = {"read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto", "glob", "read_text", "read_blob", "sniff_csv",
                 "parquet_scan", "csv_scan", "query", "query_table", "getenv", "current_setting"}


def schema_text(store, max_tables: int = 20) -> str:
    from .understand import dictionary_text
    return dictionary_text(store, max_tables=max_tables)


def validate_sql(sql: str, known: set | None = None) -> tuple[bool, str, str]:
    """Return (ok, reason, safe_sql). Only a single read-only SELECT over known tables is allowed."""
    sql = sql.strip().rstrip(";")
    try:
        parsed = sqlglot.parse(sql, read="duckdb")
    except Exception as e:
        return False, f"SQL did not parse: {e}", ""
    if len(parsed) != 1 or parsed[0] is None:
        return False, "Exactly one statement is allowed.", ""
    tree = parsed[0]
    if not isinstance(tree, (exp.Select, exp.Union, exp.With)) and not tree.find(exp.Select):
        return False, "Only SELECT queries are allowed (read-only).", ""
    for bad in (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter, exp.Command, exp.Merge, exp.TruncateTable):
        if tree.find(bad):
            return False, "Write / DDL statements are not allowed (read-only).", ""
    known = known or set()
    ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
    for t in tree.find_all(exp.Table):
        name = t.name
        if name and name not in known and name not in ctes:
            return False, f"Unknown table '{name}'. Allowed: {', '.join(sorted(known))}", ""
    for f in tree.find_all(exp.Anonymous, exp.Func):
        fname = (f.name if isinstance(f, exp.Anonymous) else f.sql_name()).lower()
        if fname in BLOCKED_FUNCS:
            return False, f"Function '{fname}' is not allowed.", ""
    if not tree.args.get("limit") and isinstance(tree, exp.Select):
        tree = tree.limit(500)
    return True, "", tree.sql(dialect="duckdb")


def run_sql(store, sql: str) -> pd.DataFrame:
    with _DUCK_LOCK:
        con = duckdb.connect()
        for t in store.usable():
            con.register(t.key, t.df)
        try:
            con.execute("SET enable_external_access = false")
        except Exception:
            pass
        out = con.execute(sql).df()
        con.close()
        return out


SQL_SCHEMA = {"type": "object", "properties": {"sql": {"type": "string"}, "assumptions": {"type": "array", "items": {"type": "string"}},
                                               "chart": {"type": "string", "enum": ["bar", "line", "pie", "none"]}},
              "required": ["sql", "assumptions", "chart"], "additionalProperties": False}

def answer_with_sql(store, question: str) -> dict | None:
    if not llm.available():
        return None
    msgs = [{"role": "system", "content": "You write one DuckDB SQL SELECT query to answer a business question. "
                                          "Use ONLY these tables and columns. Months are text 'YYYY-MM'. Never modify data. "
                                          "Return JSON {sql, assumptions, chart}. If the question cannot be answered from these tables, return sql ''.\n\n" + schema_text(store)},
            {"role": "user", "content": question}]
    obj = llm.chat_json(msgs, SQL_SCHEMA, purpose="sql_query")
    if not obj or not obj.get("sql"):
        return None
    ok, why, safe = validate_sql(obj["sql"], {t.key for t in store.usable()})
    if not ok:
        return result("nl2sql", "Could not run a safe query", [f"The generated query was blocked: {why}"], extra={"sql": obj.get("sql"), "unverified": True}, verified=False)
    try:
        df = run_sql(store, safe)
    except Exception as e:
        return result("nl2sql", "Query failed", [f"The generated query failed: {str(e)[:200]}"], extra={"sql": safe, "unverified": True}, verified=False)
    ans = [f"**Unverified answer** - this question is outside the certified glossary, so I generated a read-only SQL query (shown below). Please review it."]
    if df.shape == (1, 1):
        v = df.iat[0, 0]
        ans.append(f"Result: **{v:,.2f}**" if isinstance(v, (int, float)) else f"Result: **{v}**")
    else:
        ans.append(f"Result: {len(df)} row(s).")
    ch = None
    num = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    cat = [c for c in df.columns if c not in num]
    if obj.get("chart") in ("bar", "line", "pie") and num and cat and 1 < len(df) <= 60:
        ch = {"type": obj["chart"], "title": question[:80], "x": df[cat[0]].astype(str).tolist(), "series": [{"name": num[0], "data": df[num[0]].round(4).tolist()}]}
    tb = {"columns": [str(c) for c in df.columns], "rows": df.head(200).astype(object).where(df.head(200).notna(), None).values.tolist(), "caption": "Query result", "total_rows": len(df)}
    return result("nl2sql", "Ad-hoc query (unverified)", ans, table=tb, chart=ch,
                  assumptions=obj.get("assumptions") or [], sources=[{"file": "loaded tables (read-only copy)", "formula": safe}],
                  extra={"sql": safe, "unverified": True}, verified=False)
