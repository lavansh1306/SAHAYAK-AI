"""Questions: every figure equals a direct calculation on the loaded table and matches the independent SQL check."""
import numpy as np
import pytest

from app.query import engine as E


def P(t, measures, **kw):
    p = {"table": t.key, "measures": measures, "group": [], "grain": None, "filters": [], "having": [], "period": None, "compare": None, "sort": None,
         "limit": None, "share": False, "chart": "auto", "mode": "aggregate"}
    p.update(kw)
    return p


def test_totals_match_direct_calculation(store):
    n = 0
    for t in store.usable():
        for c in t.measures():
            e = t.cols[c]
            x = E.run(store, P(t, [{"col": c}], period=None))
            v = x["res"]["m0"].iloc[0]
            if e["agg"] == "sum":
                assert np.isclose(v, t.df[c].sum()), (t.key, c)
            elif e["agg"] == "mean":
                assert np.isclose(v, t.df[c].mean()), (t.key, c)
            elif e["agg"] == "ratio":
                r = e["ratio"]
                assert np.isclose(v, t.df[r["num"]].sum() / t.df[r["den"]].sum() * r["factor"]), (t.key, c)
            assert all(ch["match"] for ch in x["checks"]), (t.key, c, x["checks"])
            n += 1
    assert n > 0


def test_grouped_and_time_results_cross_checked(store):
    for t in store.usable():
        ms, ds = t.measures(), t.dims()
        if not ms:
            continue
        if ds:
            x = E.run(store, P(t, [{"col": ms[0]}], group=[ds[0]]))
            assert all(ch["match"] for ch in x["checks"]), (t.key, x["checks"])
            if t.cols[ms[0]]["agg"] == "sum":
                assert np.isclose(x["res"]["m0"].sum(), t.df[ms[0]].sum(), rtol=1e-9)
        if t.time:
            x = E.run(store, P(t, [{"col": ms[0]}], grain="month"))
            assert all(ch["match"] for ch in x["checks"]), (t.key, x["checks"])


def test_counts(store):
    for t in store.usable():
        x = E.run(store, P(t, [{"col": "*", "agg": "count"}]))
        assert x["res"]["m0"].iloc[0] == len(t.df)


def test_suggested_questions_all_answer(store, ask):
    from app.store import examples
    qs = [q for g in examples(store) for q in g["items"]]
    assert qs
    bad = []
    for q in qs:
        r = ask(q)
        if r["type"] != "answer" or (r.get("checks") and not all(c["match"] for c in r["checks"])):
            bad.append((q, r["type"], r["answer"][:1]))
    assert not bad, bad


def test_follow_up_keeps_context(store, ask):
    t = next(t for t in store.usable() if t.measures() and t.dims() and t.cols[t.dims()[0]].get("values"))
    m = t.cols[t.measures()[0]]["label"].split(" (")[0].lower()
    d = t.cols[t.dims()[0]]["label"].lower()
    r1 = ask(f"total {m} by {d}")
    assert r1["type"] == "answer"
    v = t.cols[t.dims()[0]]["values"][0]
    r2 = ask(f"only {v}")
    assert r2["type"] == "answer" and str(v) in " ".join(r2["answer"]) + str(r2.get("plan_text"))


@pytest.mark.parametrize("q", ["Delete all records", "drop table x", "update the data and set every value to zero"])
def test_read_only(ask, q):
    assert ask(q)["type"] == "refusal"


def test_out_of_scope(ask):
    assert ask("What is the weather tomorrow?")["type"] == "out_of_scope"


def test_nonsense_asks_back(ask):
    assert ask("blorp the quantum frobnicator")["type"] == "clarify"


@pytest.mark.parametrize("sql", ["DELETE FROM t", "DROP TABLE t", "SELECT * FROM read_csv('/etc/passwd')", "SELECT 1; DROP TABLE t", "COPY t TO 'x.csv'", "ATTACH 'x.db' AS x"])
def test_sql_guard(sql):
    from app.query.nl2sql import validate_sql
    ok, why, _ = validate_sql(sql, {"t"})
    assert not ok, sql
