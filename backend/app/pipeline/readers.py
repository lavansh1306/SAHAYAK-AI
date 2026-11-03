"""Stage 1 - Ingest: open any supported file and return its raw tables (values untouched) or mark it as a
document / slide template. Nothing here knows what the data is about."""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

TABLE_EXT = {".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".xls", ".sqlite", ".db", ".sqlite3", ".json", ".jsonl", ".ndjson", ".parquet"}
DOC_EXT = {".pdf", ".docx", ".md"}
TEMPLATE_EXT = {".pptx", ".potx"}
SUPPORTED = sorted(TABLE_EXT | DOC_EXT | TEMPLATE_EXT)
GENERIC_NAMES = {"sheet", "sheet1", "sheet2", "sheet3", "data", "records", "items", "rows", "results", "table", "table1", "export", "csv", "main", "values", "list"}


@dataclass
class RawTable:
    origin: str                 # where in the file: sheet name, table name, JSON key ...
    name: str                   # suggested table name
    df: pd.DataFrame            # header applied, values as found
    header_row: int | None = None
    notes: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


@dataclass
class RawFile:
    name: str
    kind: str                   # table | document | template
    tables: list[RawTable] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)


def _blank(v) -> bool:
    return v is None or (isinstance(v, float) and pd.isna(v)) or (isinstance(v, str) and not v.strip())


def _texty(v) -> bool:
    if _blank(v):
        return False
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return False
    s = str(v).strip()
    return not re.fullmatch(r"[-+(]?[₹$]?\s?[\d,]*\.?\d+%?\)?", s) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}( \d{2}:\d{2}(:\d{2})?)?", s)


def file_stem_name(fname: str) -> str:
    """'05_tanker_fleet_gps_trips_Q2FY27.csv' -> 'tanker fleet gps trips' (dates / versions / numbering removed)."""
    stem = Path(fname).stem.lower()
    toks = re.split(r"[^a-z0-9]+", stem)
    drop = re.compile(r"^(\d+|fy\d{2,4}|q[1-4](fy\d{2,4})?|h[12]|20\d{2}|\d{4}\d{2}|v\d+|final|raw|copy|export|extract|latest|new|old|"
                      r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|march|april|june|july|august|september|"
                      r"october|november|december|messy|clean|data|file|sample|test|fy\d{2}|to)$")
    keep = [t for t in toks if t and not drop.match(t)]
    return " ".join(keep) or stem


def suggest_name(fname: str, origin: str) -> str:
    o = re.sub(r"^table\s+", "", str(origin)).strip()
    if o and o.lower().replace(" ", "") not in GENERIC_NAMES and not re.fullmatch(r"sheet\s*\d*", o, re.I):
        return o.replace("_", " ")
    return file_stem_name(fname)


# ------------------------------------------------------------------------------------- grids
def detect_header_row(grid: pd.DataFrame, max_scan: int = 25) -> int:
    """First row that looks like column names: several cells, mostly text, followed by data rows."""
    n = min(max_scan, len(grid))
    width = int(grid.iloc[: max(n, 1)].notna().sum(axis=1).max()) if len(grid) else 0
    for i in range(n):
        vals = [v for v in grid.iloc[i].tolist() if not _blank(v)]
        if len(vals) < 2 or len(vals) < 0.5 * width:
            continue
        if sum(_texty(v) for v in vals) >= max(2, 0.6 * len(vals)):
            nxt = grid.iloc[i + 1: i + 4]
            if len(nxt) and nxt.notna().sum(axis=1).max() >= 2:
                return i
    return 0


def _two_row_header(grid: pd.DataFrame, h: int) -> bool:
    """Header split over two rows (merged group cells above, details below)."""
    if h + 2 >= len(grid):
        return False
    r1, r2, r3 = grid.iloc[h].tolist(), grid.iloc[h + 1].tolist(), grid.iloc[h + 2].tolist()
    blanks1 = sum(_blank(v) for v in r1)
    text2 = sum(_texty(v) for v in r2)
    nonblank2 = sum(not _blank(v) for v in r2)
    data3 = sum(not _blank(v) and not _texty(v) for v in r3)
    return blanks1 >= 1 and nonblank2 >= 2 and text2 >= 0.8 * nonblank2 and data3 >= 1 and text2 >= 2


def _apply_header(grid: pd.DataFrame, h: int) -> tuple[pd.DataFrame, int]:
    two = _two_row_header(grid, h)
    top = grid.iloc[h].tolist()
    if two:
        filled, last = [], ""
        for v in top:
            last = str(v).strip() if not _blank(v) else last
            filled.append(last)
        sub = grid.iloc[h + 1].tolist()
        names = []
        for a, b in zip(filled, sub):
            b = "" if _blank(b) else str(b).strip()
            names.append(f"{a} {b}".strip() if a and b and a.lower() not in b.lower() else (b or a))
        body_from = h + 2
    else:
        names = ["" if _blank(v) else str(v).strip() for v in top]
        body_from = h + 1
    cols, seen = [], {}
    for j, c in enumerate(names):
        c = re.sub(r"\s+", " ", c) or f"column_{j + 1}"
        if isinstance(top[j], (pd.Timestamp,)) and not _blank(top[j]):
            c = pd.Timestamp(top[j]).strftime("%Y-%m-%d")
        k = c.lower()
        if k in seen:
            seen[k] += 1
            c = f"{c}_{seen[k]}"
        else:
            seen[k] = 1
        cols.append(c)
    df = grid.iloc[body_from:].copy()
    df.columns = cols
    df = df.dropna(how="all")
    keep = [not (df[c].isna().all() and str(c).startswith("column_")) for c in df.columns]
    return df.loc[:, keep].reset_index(drop=True), (h + 2 if two else h + 1)


def _readme_like(grid: pd.DataFrame) -> bool:
    nonempty_cols = int((grid.notna().sum() > 0).sum())
    if nonempty_cols <= 1:
        return True
    if nonempty_cols == 2 and len(grid) <= 30:
        first = grid.dropna(how="all").iloc[:, 0].dropna().astype(str)
        return bool(len(first)) and first.str.len().mean() < 30 and (grid.iloc[:, 1].dropna().astype(str).str.len().mean() > 25)
    return False


def _split_blocks(grid: pd.DataFrame) -> list[pd.DataFrame]:
    """Split a sheet that holds several tables stacked vertically (separated by 2+ blank rows)."""
    blank = grid.isna().all(axis=1).tolist()
    blocks, start, run = [], 0, 0
    for i, b in enumerate(blank):
        run = run + 1 if b else 0
        if run == 2:
            seg = grid.iloc[start: i - 1]
            if seg.notna().any().any():
                blocks.append(seg)
            start = i + 1
    seg = grid.iloc[start:]
    if seg.notna().any().any():
        blocks.append(seg)
    good = [b for b in blocks if len(b.dropna(how="all")) >= 3]
    return good if len(good) > 1 else [grid]


def grid_tables(grid: pd.DataFrame, origin: str, fname: str) -> tuple[list[RawTable], list[str]]:
    grid = grid.dropna(how="all").dropna(how="all", axis=1).reset_index(drop=True)
    if grid.empty:
        return [], []
    if _readme_like(grid):
        return [], [" ".join(str(v) for v in r if not _blank(v)) for r in grid.values.tolist() if any(not _blank(v) for v in r)][:40]
    out, notes_all = [], []
    blocks = _split_blocks(grid)
    for bi, block in enumerate(blocks):
        block = block.dropna(how="all", axis=1).reset_index(drop=True)
        h = detect_header_row(block)
        title = [" ".join(str(v) for v in block.iloc[i].tolist() if not _blank(v)) for i in range(h)]
        df, hr = _apply_header(block, h)
        notes = [t for t in title if t]
        # trailing note lines (one long text cell, rest empty)
        keep_rows = []
        for idx, r in df.iterrows():
            vals = [v for v in r.tolist() if not _blank(v)]
            if len(vals) == 1 and isinstance(vals[0], str) and len(vals[0]) > 25 and df.shape[1] > 2:
                notes.append(vals[0].strip())
            else:
                keep_rows.append(idx)
        df = df.loc[keep_rows].reset_index(drop=True)
        if df.shape[1] < 1 or len(df) == 0:
            continue
        o = origin if len(blocks) == 1 else f"{origin} (table {bi + 1})"
        name = suggest_name(fname, origin)
        if len(blocks) > 1:
            name = (title[-1] if title else f"{name} {bi + 1}")
        out.append(RawTable(origin=o, name=name, df=df, header_row=hr, notes=notes))
        notes_all += notes
    return out, notes_all


# ------------------------------------------------------------------------------------- formats
def _read_csv(path: Path, sep_hint: str | None = None) -> pd.DataFrame:
    raw = Path(path).read_bytes()
    encs = ["utf-8-sig", "utf-16", "cp1252", "latin-1"] if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ["utf-8-sig", "cp1252", "latin-1"]
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        encs = ["utf-16"] + encs
    last = None
    for enc in encs:
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError as e:
            last = e
            continue
        head = text[:6000].splitlines()[:8]
        sep = sep_hint or max([",", ";", "\t", "|"], key=lambda d: sum(line.count(d) for line in head))
        import io
        return pd.read_csv(io.StringIO(text), header=None, dtype=object, sep=sep, skip_blank_lines=False, engine="python", on_bad_lines="warn")
    raise ValueError(f"Could not read the text encoding: {last}")


def _json_tables(obj, fname: str) -> tuple[list[RawTable], list[str], dict]:
    lists: dict[str, list] = {}
    notes, meta = [], {}

    def walk(o, path):
        if isinstance(o, list) and o and all(isinstance(x, dict) for x in o[:20]):
            lists[path or "records"] = o
        elif isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, (list, dict)):
                    walk(v, f"{path}.{k}" if path else k)
                elif path == "":
                    notes.append(f"{k}: {v}")
            if path and not any(isinstance(v, (list, dict)) for v in o.values()) and len(o) <= 30:
                meta[path] = o

    walk(obj, "")
    tables = []
    for k, v in lists.items():
        df = pd.json_normalize(v, sep="_")
        if df.empty:
            continue
        tables.append(RawTable(origin=k, name=suggest_name(fname, k.split(".")[-1]), df=df.astype(object).where(df.notna(), None), notes=list(notes)))
    return tables, notes, meta


def read_file(path: Path, name: str | None = None) -> RawFile:
    name = name or path.name
    ext = path.suffix.lower()
    if ext in TEMPLATE_EXT:
        return RawFile(name, "template")
    if ext in DOC_EXT:
        return RawFile(name, "document")
    if ext in (".csv", ".tsv", ".txt"):
        grid = _read_csv(path, "\t" if ext == ".tsv" else None)
        if ext == ".txt" and grid.shape[1] <= 1:
            return RawFile(name, "document")
        t, notes = grid_tables(grid, "csv", name)
        return RawFile(name, "table", t, notes)
    if ext in (".xlsx", ".xlsm", ".xls"):
        sheets = pd.read_excel(path, sheet_name=None, header=None, dtype=object)
        tables, notes = [], []
        for sh, grid in sheets.items():
            t, n = grid_tables(grid, sh, name)
            tables += t
            notes += n
        return RawFile(name, "table", tables, notes)
    if ext == ".parquet":
        df = pd.read_parquet(path)
        return RawFile(name, "table", [RawTable("parquet", file_stem_name(name), df)])
    if ext in (".sqlite", ".db", ".sqlite3"):
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)  # read-only by design
        try:
            names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'")]
            tables, notes = [], []
            for t in names:
                df = pd.read_sql(f'SELECT * FROM "{t}"', con)
                if df.empty:
                    continue
                if t.startswith("_") or (df.shape[1] <= 2 and len(df) <= 10 and all(df[c].dtype == object for c in df.columns)):
                    notes += [" ".join(str(x) for x in r if x is not None) for r in df.values.tolist()]
                    continue
                tables.append(RawTable(origin=f"table {t}", name=t.replace("_", " "), df=df))
            for t in tables:
                t.notes = list(notes)
            return RawFile(name, "table", tables, notes)
        finally:
            con.close()
    if ext in (".json", ".jsonl", ".ndjson"):
        text = Path(path).read_text(encoding="utf-8-sig")
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            obj = [json.loads(line) for line in text.splitlines() if line.strip()]
        tables, notes, meta = _json_tables(obj, name)
        if not tables:
            raise ValueError("The JSON file has no list of records to load as a table")
        for t in tables:
            t.meta["json_meta"] = meta
        return RawFile(name, "table", tables, notes)
    raise ValueError(f"Unsupported file type '{ext}'. Supported: {', '.join(SUPPORTED)}")
