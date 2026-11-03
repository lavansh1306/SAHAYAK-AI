"""DataStore: the warehouse tables + their data dictionary + documents/templates, as used by the whole app."""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .config import WAREHOUSE_DIR
from .documents import Document
from .pipeline.dictionary import dim_rank, find_links, load_overrides, measure_rank, merged_entry, short
from .pipeline.warehouse import LINEAGE, Warehouse


@dataclass
class Table:
    key: str
    df: pd.DataFrame
    entry: dict
    lineage: pd.Series | None = None

    @property
    def label(self) -> str:
        return self.entry.get("label") or self.key

    @property
    def cols(self) -> dict:
        return self.entry["columns"]

    @property
    def time(self) -> str | None:
        t = self.entry.get("time")
        return t if t in self.df.columns else None

    @property
    def time_type(self) -> str | None:
        return self.cols.get(self.time, {}).get("type") if self.time else None

    def measures(self) -> list[str]:
        ms = [c for c, e in self.cols.items() if e.get("role") == "measure" and not e.get("hidden")]
        return sorted(ms, key=lambda c: -measure_rank(self.cols[c]))

    def dims(self) -> list[str]:
        ds = [c for c, e in self.cols.items() if e.get("role") == "dimension" and not e.get("hidden")]
        return sorted(ds, key=lambda c: -dim_rank(self.cols[c]))

    def ids(self) -> list[str]:
        return [c for c, e in self.cols.items() if e.get("role") == "id" and not e.get("hidden")]

    def importance(self) -> float:
        ms = self.measures()
        s = (measure_rank(self.cols[ms[0]]) if ms else 0.0) + (4 if self.time else 0) + (2 if self.entry.get("grain") == "month" else 0)
        tr = self.entry.get("time_range")
        if tr:
            try:
                s += min(3.0, (pd.Timestamp(str(tr[1])[:10]) - pd.Timestamp(str(tr[0])[:10])).days / 365)
            except Exception:
                pass
        return s

    def status(self) -> str:
        return self.entry.get("status", "ok")

    def summary(self) -> dict:
        e = self.entry
        return {"key": self.key, "label": self.label, "rows": int(len(self.df)), "time": self.time, "grain": e.get("grain"),
                "time_range": e.get("time_range"), "status": e.get("status", "ok"), "quality": e.get("quality"),
                "measures": self.measures(), "dims": self.dims(), "sources": e.get("sources", [])}


@dataclass
class DataStore:
    root: Path = WAREHOUSE_DIR
    tables: dict = field(default_factory=dict)
    documents: list = field(default_factory=list)
    templates: dict = field(default_factory=dict)
    drafts: dict = field(default_factory=dict)
    links: list = field(default_factory=list)
    catalog: dict = field(default_factory=dict)
    version: int = 0

    def __post_init__(self):
        self.wh = Warehouse(self.root)
        self.lock = threading.RLock()

    def reload(self) -> "DataStore":
        with self.lock:
            m = self.wh.manifest()
            ov = load_overrides(self.root)
            tabs = {}
            for key in self.wh.list_tables():
                info = m["tables"].get(key, {})
                auto = info.get("entry")
                if not auto:
                    continue
                df = self.wh.read(key)
                lin = df["_source"] if "_source" in df.columns else None
                df = df.drop(columns=[c for c in LINEAGE if c in df.columns])
                e = merged_entry(auto, ov.get(key, {}))
                e["sources"] = info.get("sources", [])
                e["versions"] = info.get("versions", [])[-6:]
                tabs[key] = Table(key, df, e, lin)
            self.tables = tabs
            self.documents = []
            for name, d in m.get("documents", {}).items():
                j = Path(d["path"] + ".json")
                if j.exists():
                    self.documents.append(Document.from_json(json.loads(j.read_text(encoding="utf-8"))))
            self.templates = {n: d["path"] for n, d in m.get("templates", {}).items()}
            self.drafts = {n: d for n, d in m.get("drafts", {}).items()}
            try:
                self.links = find_links({k: t for k, t in tabs.items() if t.status() != "quarantined"})
            except Exception:
                self.links = []
            self.version += 1
            self.catalog = build_catalog(self)
            return self

    # ---------------------------------------------------------------- access
    def get(self, key: str) -> Table | None:
        t = self.tables.get(key)
        return None if t is None or t.status() == "quarantined" else t

    def usable(self) -> list[Table]:
        return [t for t in self.tables.values() if t.status() != "quarantined"]

    def is_empty(self) -> bool:
        return not self.tables and not self.documents

    def load_file(self, path: Path, name: str | None = None, mode: str = "merge", auto_accept: bool = True) -> dict:
        from .pipeline.runner import submit
        return submit(Path(path), name or Path(path).name, mode=mode, store=self, background=False, auto_accept=auto_accept)


def build_catalog(store: DataStore) -> dict:
    """Lookup structures used by question understanding and the UI."""
    values: dict[str, list] = {}
    for t in store.usable():
        for c in t.dims() + t.ids():
            e = t.cols[c]
            if e.get("type") == "fy_label" and t.time:
                continue  # financial years are read from the time column, never from a text label
            for v in (e.get("values") or [])[:2000]:
                k = re.sub(r"[^a-z0-9]+", " ", str(v).lower()).strip()
                if len(k) >= 2 and not re.fullmatch(r"\d+(\.\d+)?", k):
                    values.setdefault(k, []).append((t.key, c, v))
    latest = {}
    for t in store.usable():
        tr = t.entry.get("time_range")
        if tr:
            latest[t.key] = tr[1]
    return {"values": values, "latest": latest, "empty": store.is_empty(),
            "tables": [t.summary() for t in store.tables.values()],
            "documents": [d.name for d in store.documents], "templates": list(store.templates)}


def examples(store: DataStore, n: int = 24) -> list[dict]:
    """Suggested questions built from whatever was loaded."""
    from .query.periods import month_long
    groups = []
    label_count: dict = {}
    for t in store.usable():
        for c in t.measures():
            label_count[short(t.cols[c]["label"])] = label_count.get(short(t.cols[c]["label"]), 0) + 1
    for t in sorted(store.usable(), key=lambda t: -t.importance())[:8]:
        ms, ds = t.measures(), t.dims()
        items = []
        lab = short(t.label)
        last = t.entry.get("time_range", [None, None])[1]
        lastm = None
        if last and t.time_type in ("month", "date", "datetime"):
            lastm = month_long(str(last)[:7])
        if ms:
            m = short(t.cols[ms[0]]["label"])
            if label_count.get(m, 0) > 1:
                m = f"{lab} {m}" if lab not in m else m
            if lastm:
                items.append(f"What was the total {m} in {lastm}?")
            if ds:
                d = short(t.cols[ds[0]]["label"])
                items.append(f"Which {d} had the highest {m}" + (f" in {lastm}?" if lastm else "?"))
            if t.time and t.entry.get("grain") in ("day", "hour", "month", "week", "minute"):
                items.append(f"Show the monthly trend of {m}")
            if len(ds) > 1:
                items.append(f"Share of {m} by {short(t.cols[ds[1]]['label'])} as a pie chart")
            elif ds:
                items.append(f"Share of {m} by {short(t.cols[ds[0]]['label'])} as a pie chart")
            if len(ms) > 1:
                m2 = short(t.cols[ms[1]]["label"])
                if label_count.get(m2, 0) > 1:
                    m2 = f"{lab} {m2}" if lab not in m2 else m2
                items.append(f"Average {m2} by {short(t.cols[ds[0]]['label'])}" if ds else f"Average {m2}")
            if t.time and t.time_type in ("month", "date", "datetime"):
                items.append(f"How did {m} change compared with last year?")
        else:
            if lastm:
                items.append(f"How many {lab} records were there in {lastm}?")
            if ds:
                items.append(f"How many {lab} records by {short(t.cols[ds[0]]['label'])}?")
        if items:
            groups.append({"name": t.label, "items": items[:5]})
    gen = ["What unusual things do you see in the data?", "What should we act on first?"]
    if store.documents:
        gen.append(f"Summarise {store.documents[0].name}")
    groups.append({"name": "Insights and actions", "items": gen})
    return groups


STORE = DataStore()


def init_store() -> DataStore:
    return STORE.reload()
