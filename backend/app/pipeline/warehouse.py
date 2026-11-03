"""Stage 5 - Load: versioned analytical store (Parquet files + a manifest) with row lineage.

- merge (default): rows whose key already exists are replaced, new keys are appended, and tables without a key
  drop exact duplicate rows -> uploading the same or an overlapping file never double counts
- replace: the table is replaced
- new columns in later files are added; old rows get blanks
Every load keeps the previous version (last 5) for rollback."""
from __future__ import annotations

import json
import shutil
import threading
from datetime import datetime
from pathlib import Path

import pandas as pd

KEEP_VERSIONS = 5
LINEAGE = ["_source", "_run", "_loaded_at"]


class Warehouse:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.tables = self.root / "tables"
        self.versions = self.root / "_versions"
        self.docs = self.root / "documents"
        self.templates = self.root / "templates"
        self.drafts = self.root / "drafts"
        for p in (self.tables, self.versions, self.docs, self.templates, self.drafts):
            p.mkdir(parents=True, exist_ok=True)
        self.mpath = self.root / "manifest.json"
        self.lock = threading.RLock()

    # ------------------------------------------------------------------ manifest
    def manifest(self) -> dict:
        try:
            m = json.loads(self.mpath.read_text(encoding="utf-8"))
        except Exception:
            m = {}
        for k in ("tables", "documents", "templates", "drafts"):
            m.setdefault(k, {})
        return m

    def save_manifest(self, m: dict) -> None:
        tmp = self.mpath.with_suffix(".tmp")
        tmp.write_text(json.dumps(m, indent=1, default=str), encoding="utf-8")
        tmp.replace(self.mpath)

    # ------------------------------------------------------------------ tables
    def list_tables(self) -> list[str]:
        return sorted(p.stem for p in self.tables.glob("*.parquet"))

    def read(self, key: str) -> pd.DataFrame | None:
        p = self.tables / f"{key}.parquet"
        return pd.read_parquet(p) if p.exists() else None

    def write(self, key: str, df: pd.DataFrame, keys: list[str], mode: str, run_id: str, source: str) -> dict:
        with self.lock:
            new = df.copy()
            new["_source"] = source
            new["_run"] = run_id
            new["_loaded_at"] = datetime.now().isoformat(timespec="seconds")
            old = self.read(key)
            before = 0 if old is None else len(old)
            inserted = updated = 0
            if old is None or mode == "replace":
                merged, inserted = new, len(new)
            else:
                for c in old.columns:
                    if c not in new.columns:
                        new[c] = pd.NA
                for c in new.columns:
                    if c not in old.columns:
                        old[c] = pd.NA
                for c in new.columns:  # keep one type per column
                    if str(old[c].dtype).startswith("datetime") and not str(new[c].dtype).startswith("datetime"):
                        new[c] = pd.to_datetime(new[c], errors="coerce")
                    elif str(new[c].dtype).startswith("datetime") and not str(old[c].dtype).startswith("datetime"):
                        old[c] = pd.to_datetime(old[c], errors="coerce")
                data_cols = [c for c in new.columns if c not in LINEAGE]
                ks = [k for k in keys if k in new.columns and k in old.columns]
                if ks:
                    kf = lambda d: d[ks].astype(str).agg("|".join, axis=1)  # noqa: E731
                    ok, nk = kf(old), kf(new)
                    upd = ok.isin(set(nk))
                    updated = int(upd.sum())
                    inserted = int((~nk.isin(set(ok))).sum())
                    merged = pd.concat([old[~upd], new], ignore_index=True)
                else:
                    sig = lambda d: d[data_cols].astype(str).agg("|".join, axis=1)  # noqa: E731
                    so, sn = sig(old), sig(new)
                    dup = sn.isin(set(so))
                    updated = int(dup.sum())
                    inserted = int((~dup).sum())
                    merged = pd.concat([old, new[~dup]], ignore_index=True)
            sort = [k for k in keys if k in merged.columns]
            if sort:
                try:
                    merged = merged.sort_values(sort, kind="stable").reset_index(drop=True)
                except TypeError:
                    pass
            m = self.manifest()
            info = m["tables"].get(key, {"versions": []})
            v = (info["versions"][-1]["v"] + 1) if info["versions"] and isinstance(info["versions"][-1].get("v"), int) else len(info["versions"]) + 1
            p = self.tables / f"{key}.parquet"
            if p.exists():
                vd = self.versions / key
                vd.mkdir(exist_ok=True)
                shutil.copy(p, vd / f"v{v - 1}.parquet")
                for o in sorted(vd.glob("v*.parquet"), key=lambda x: int(x.stem[1:]))[:-KEEP_VERSIONS]:
                    o.unlink()
            for c in merged.columns:  # parquet needs one type per column
                if merged[c].dtype == object:
                    types = {type(x) for x in merged[c].dropna().head(5000)}
                    if len(types) > 1:
                        merged[c] = merged[c].map(lambda x: None if x is None or (isinstance(x, float) and pd.isna(x)) else str(x))
            merged.to_parquet(p, index=False)
            info["versions"].append({"v": v, "run_id": run_id, "source": source, "mode": mode, "rows_in_file": len(new), "inserted": inserted,
                                     "updated": updated, "rows_before": before, "rows_after": len(merged), "ts": datetime.now().isoformat(timespec="seconds")})
            info["rows"] = len(merged)
            srcs = info.get("sources", [])
            info["sources"] = [source] if mode == "replace" else (srcs + [source] if source not in srcs else srcs)
            m["tables"][key] = info
            self.save_manifest(m)
            return {"mode": mode, "inserted": inserted, "updated": updated, "rows_before": before, "rows_after": len(merged), "version": v}

    def set_info(self, key: str, **kw) -> None:
        with self.lock:
            m = self.manifest()
            m["tables"].setdefault(key, {"versions": []}).update(kw)
            self.save_manifest(m)

    def rollback(self, key: str) -> bool:
        with self.lock:
            vd = self.versions / key
            olds = sorted(vd.glob("v*.parquet"), key=lambda x: int(x.stem[1:])) if vd.exists() else []
            if not olds:
                return False
            shutil.copy(olds[-1], self.tables / f"{key}.parquet")
            olds[-1].unlink()
            m = self.manifest()
            df = pd.read_parquet(self.tables / f"{key}.parquet")
            info = m["tables"][key]
            info["versions"].append({"v": "rollback", "ts": datetime.now().isoformat(timespec="seconds"), "rows_after": len(df), "mode": "rollback",
                                     "source": f"restored v{olds[-1].stem[1:]}"})
            info["rows"] = len(df)
            if "_source" in df.columns:
                info["sources"] = sorted(df["_source"].dropna().unique().tolist())
            self.save_manifest(m)
            return True

    def delete(self, key: str) -> None:
        with self.lock:
            (self.tables / f"{key}.parquet").unlink(missing_ok=True)
            shutil.rmtree(self.versions / key, ignore_errors=True)
            m = self.manifest()
            m["tables"].pop(key, None)
            self.save_manifest(m)

    # ------------------------------------------------------------------ files
    def put_file(self, kind: str, name: str, src: Path, info: dict | None = None) -> Path:
        with self.lock:
            folder = {"documents": self.docs, "templates": self.templates, "drafts": self.drafts}[kind]
            dest = folder / name
            if Path(src).resolve() != dest.resolve():
                shutil.copy(src, dest)
            m = self.manifest()
            m[kind][name] = {"path": str(dest), "loaded_at": datetime.now().isoformat(timespec="seconds"), **(info or {})}
            self.save_manifest(m)
            return dest

    def remove_file(self, kind: str, name: str) -> bool:
        with self.lock:
            m = self.manifest()
            e = m.get(kind, {}).pop(name, None)
            if e:
                Path(e["path"]).unlink(missing_ok=True)
                Path(e["path"] + ".json").unlink(missing_ok=True)
            self.save_manifest(m)
            return e is not None

    def wipe(self) -> None:
        with self.lock:
            shutil.rmtree(self.root, ignore_errors=True)
            self.__init__(self.root)
