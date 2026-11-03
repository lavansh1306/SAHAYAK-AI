"""Document ingestion (PDF / DOCX / TXT) and a small BM25 search with citations."""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

STOP = set("""a an the of in on at to for from by with and or is are was were be been it its this that these those as
what which who whom how why when where do does did our we you your i me my there their they them than then so not
any all can could should would will shall may might into about over under up down out off per vs""".split())


def tokenize(text: str) -> list[str]:
    toks = re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", text.lower())
    out = []
    for t in toks:
        if t in STOP:
            continue
        if len(t) > 4 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.append(t)
    return out


@dataclass
class Passage:
    doc: str
    locator: str  # e.g. "page 1, section '1. Highlights'"
    text: str
    tokens: list[str] = field(default_factory=list)


@dataclass
class Document:
    name: str
    kind: str
    text: str
    passages: list[Passage]
    sections: list[tuple[str, str]]  # (heading, body)

    def summary(self) -> dict:
        return {"name": self.name, "kind": self.kind, "characters": len(self.text), "passages": len(self.passages),
                "sections": [h for h, _ in self.sections]}

    def to_json(self) -> dict:
        return {"name": self.name, "kind": self.kind, "text": self.text, "sections": [list(x) for x in self.sections],
                "passages": [{"doc": p.doc, "locator": p.locator, "text": p.text} for p in self.passages]}

    @staticmethod
    def from_json(o: dict) -> "Document":
        ps = [Passage(p["doc"], p["locator"], p["text"], tokenize(p["text"] + " " + p["locator"])) for p in o["passages"]]
        return Document(o["name"], o["kind"], o["text"], ps, [tuple(x) for x in o["sections"]])


HEADING_RE = re.compile(r"^\s*(\d+\.\s+[A-Z][^\n]{2,80}|#{1,3}\s+.+)$")


def _split_sentences(par: str) -> list[str]:
    par = re.sub(r"\s+", " ", par).strip()
    if not par:
        return []
    parts = re.split(r"(?<=[.;])\s+(?=[A-Z(])", par)
    return [p.strip() for p in parts if len(p.strip()) > 3]


def _sections_from_lines(lines: list[str]) -> list[tuple[str, str]]:
    sections: list[tuple[str, list[str]]] = [("(preamble)", [])]
    for ln in lines:
        if HEADING_RE.match(ln.strip()) and len(ln.strip()) < 90:
            sections.append((ln.strip().lstrip("#").strip(), []))
        else:
            sections[-1][1].append(ln)
    return [(h, "\n".join(b).strip()) for h, b in sections if "\n".join(b).strip() or h != "(preamble)"]


def load_document(path: Path, name: str) -> Document:
    ext = path.suffix.lower()
    pages: list[str] = []
    if ext == ".pdf":
        try:
            import pdfplumber

            with pdfplumber.open(str(path)) as pdf:
                pages = [(p.extract_text() or "") for p in pdf.pages]
        except Exception:
            from pypdf import PdfReader

            pages = [(p.extract_text() or "") for p in PdfReader(str(path)).pages]
        kind = "PDF"
    elif ext == ".docx":
        from docx import Document as Docx

        d = Docx(str(path))
        lines = []
        for p in d.paragraphs:
            t = p.text.strip()
            if not t:
                continue
            if p.style is not None and p.style.name.lower().startswith(("heading", "title")):
                lines.append(f"## {t}")
            else:
                lines.append(t)
        for tb in d.tables:
            for row in tb.rows:
                lines.append(" | ".join(c.text.strip() for c in row.cells))
        pages = ["\n".join(lines)]
        kind = "Word"
    else:
        pages = [path.read_text(encoding="utf-8", errors="ignore")]
        kind = "Text"
    text = "\n".join(pages)
    passages: list[Passage] = []
    all_sections: list[tuple[str, str]] = []
    for pi, pg in enumerate(pages, start=1):
        secs = _sections_from_lines(pg.splitlines())
        all_sections.extend(secs)
        for h, body in secs:
            # paragraphs: merge wrapped lines (PDF) into sentences
            for s in _split_sentences(body.replace("\n", " ")):
                loc = f"page {pi}, section '{h}'" if kind == "PDF" else f"section '{h}'"
                passages.append(Passage(name, loc, s, tokenize(s + " " + h)))
    return Document(name=name, kind=kind, text=text, passages=passages, sections=all_sections)


def bm25_search(docs: list[Document], query: str, k: int = 5, doc_filter: str | None = None) -> list[dict]:
    pas = [p for d in docs for p in d.passages if not doc_filter or doc_filter.lower() in d.name.lower()]
    if not pas:
        return []
    q = tokenize(query)
    if not q:
        return []
    N = len(pas)
    avgdl = sum(len(p.tokens) for p in pas) / N or 1
    df = Counter(t for p in pas for t in set(p.tokens))
    k1, b = 1.4, 0.75
    scored = []
    for p in pas:
        tf = Counter(p.tokens)
        s = 0.0
        for t in q:
            if t not in tf:
                continue
            idf = math.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(p.tokens) / avgdl))
        if s > 0:
            scored.append((s, p))
    scored.sort(key=lambda x: -x[0])
    return [{"doc": p.doc, "locator": p.locator, "text": p.text, "score": round(s, 3)} for s, p in scored[:k]]


