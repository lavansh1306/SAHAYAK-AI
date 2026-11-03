"""Pipeline: empty start, 7 stages, every file loads, numbers preserved, idempotent, rollback, persistence,
quarantine, safe merging, and no data values hard-coded in the app."""
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from conftest import ROOT, data_files


def fresh(tmp_path):
    from app.store import DataStore
    return DataStore(tmp_path / "wh").reload()


def test_fresh_store_is_empty(tmp_path):
    s = fresh(tmp_path)
    assert s.is_empty() and s.tables == {} and s.documents == []


def test_every_file_loaded(store):
    for name, r in store.runs.items():
        assert r["status"].startswith("succeeded"), (name, r["message"])
        assert [s["key"] for s in r["stages"]] == ["ingest", "profile", "clean", "validate", "load", "dictionary", "ready"]
        assert all(s["status"] in ("done", "warn") for s in r["stages"]), (name, [(s["key"], s["status"]) for s in r["stages"]])
        assert all(s["summary"] for s in r["stages"])
    assert store.tables, "no table was loaded"


def test_every_table_has_dictionary(store):
    for t in store.tables.values():
        assert t.entry["columns"], t.key
        for c, e in t.cols.items():
            assert e.get("type") and e.get("role"), (t.key, c)
            if e["role"] == "measure":
                assert e.get("agg") in ("sum", "mean", "ratio", "last"), (t.key, c, e.get("agg"))


def _csvs():
    return [f for f in data_files() if f.suffix.lower() == ".csv"]


@pytest.mark.parametrize("f", _csvs(), ids=lambda p: p.name)
def test_csv_numbers_preserved(tmp_path, f):
    """Every numeric column total in the clean table equals the total in the raw file (minus removed total rows)."""
    s = fresh(tmp_path)
    run = s.load_file(f)
    assert run["status"].startswith("succeeded"), run["message"]
    t = next(iter(s.tables.values()))
    raw = pd.read_csv(f)
    for c, e in t.cols.items():
        if e["type"] != "number" or e.get("unit_kind") in ("percent",) or e.get("original") not in raw.columns:
            continue
        rv = pd.to_numeric(raw[e["original"]].astype(str).str.replace(",", ""), errors="coerce")
        assert np.isclose(t.df[c].sum(), rv.sum() * (e.get("scale_applied", 1)), rtol=1e-9), (c, t.df[c].sum(), rv.sum())


def test_reupload_changes_nothing(tmp_path):
    f = _csvs()[0]
    s = fresh(tmp_path)
    s.load_file(f)
    k = next(iter(s.tables))
    before = len(s.tables[k].df)
    r2 = s.load_file(f)
    assert r2["tables"][0]["load"]["inserted"] == 0
    assert len(s.tables[k].df) == before


def test_split_files_merge(tmp_path):
    f = _csvs()[0]
    raw = pd.read_csv(f)
    a, b = raw.iloc[: len(raw) // 2], raw.iloc[len(raw) // 2:]
    pa, pb = tmp_path / f"{f.stem}_part1.csv", tmp_path / f"{f.stem}_part2.csv"
    a.to_csv(pa, index=False)
    b.to_csv(pb, index=False)
    s = fresh(tmp_path)
    s.load_file(pa)
    r = s.load_file(pb)
    assert len(s.tables) == 1, "the second file must join the first table"
    assert len(next(iter(s.tables.values())).df) == len(raw)
    assert r["tables"][0]["target"]["action"] == "merge"


def test_data_survives_restart(tmp_path):
    from app.store import DataStore
    s = fresh(tmp_path)
    s.load_file(_csvs()[0])
    s2 = DataStore(tmp_path / "wh").reload()
    assert {k: len(t.df) for k, t in s2.tables.items()} == {k: len(t.df) for k, t in s.tables.items()}


def test_rollback(tmp_path):
    f = _csvs()[0]
    s = fresh(tmp_path)
    s.load_file(f)
    k = next(iter(s.tables))
    m = s.tables[k].measures()[0]
    before = s.tables[k].df[m].sum()
    raw = pd.read_csv(f)
    orig = s.tables[k].cols[m]["original"]
    raw[orig] = pd.to_numeric(raw[orig], errors="coerce") * 2
    p = tmp_path / f.name
    raw.to_csv(p, index=False)
    s.load_file(p, mode="replace")
    assert np.isclose(s.tables[k].df[m].sum(), before * 2)
    assert s.wh.rollback(k)
    s.reload()
    assert np.isclose(s.tables[k].df[m].sum(), before)


def test_unreadable_time_values_are_quarantined(tmp_path):
    """Next month's file for an existing dated table, with an unreadable date column, must not be loaded."""
    s = fresh(tmp_path)
    f = None
    for c in _csvs():
        s.load_file(c)
        t = next((t for t in s.tables.values() if t.time and t.cols[t.time].get("original") in pd.read_csv(c, nrows=1).columns), None)
        if t:
            f = c
            break
    if f is None:
        pytest.skip("no CSV with a time column")
    before = len(t.df)
    raw = pd.read_csv(f)
    raw[t.cols[t.time]["original"]] = "not a date"
    p = tmp_path / f.name
    raw.to_csv(p, index=False)
    run = s.load_file(p)
    assert run["status"] == "quarantined", run["status"]
    assert len(s.tables[t.key].df) == before


def test_broken_file_fails_cleanly(tmp_path):
    p = tmp_path / "broken.xlsx"
    p.write_bytes(b"not really excel")
    s = fresh(tmp_path)
    assert s.load_file(p)["status"] == "failed"
    assert s.is_empty()


def test_document_indexed(store):
    docs = [f for f in data_files() if f.suffix.lower() in (".pdf", ".docx")]
    if not docs:
        pytest.skip("no documents in the test files")
    assert len(store.documents) == len(docs)
    assert all(len(d.passages) > 0 for d in store.documents)


def test_no_data_values_in_app_code(store):
    """Nothing from the loaded data (names, codes) may be written into the app's code."""
    vals = set()
    for t in store.usable():
        for c in t.dims() + t.ids():
            if re.search(r"unit", c):
                continue  # unit names (SCM, kg ...) are vocabulary, not data
            for v in (t.cols[c].get("values") or [])[:300]:
                v = str(v)
                if len(v) >= 5 and re.search(r"[A-Za-z]", v) and not re.fullmatch(r"(yes|no|true|false|open|closed|other|others|total|none|male|female)", v, re.I):
                    vals.add(v)
    files = [p for p in (ROOT / "app").rglob("*") if p.suffix in (".py", ".yaml", ".json")]
    files += [p for p in (ROOT.parent / "frontend" / "src").rglob("*") if p.suffix in (".js", ".jsx", ".css")]
    hits = []
    for p in files:
        txt = p.read_text(encoding="utf-8", errors="ignore")
        for v in vals:
            if re.search(r"(?<![A-Za-z0-9])" + re.escape(v) + r"(?![A-Za-z0-9])", txt):
                hits.append(f"{p.relative_to(ROOT.parent)}: {v}")
    assert not hits, "data values found in app code:\n" + "\n".join(hits[:30])


def test_project_ships_without_data():
    data = ROOT.parent / "data"
    shipped = [p for p in data.rglob("*") if p.is_file() and p.name != ".gitkeep"] if data.exists() else []
    assert not shipped, [str(p) for p in shipped[:10]]
