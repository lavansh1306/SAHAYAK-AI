"""Matplotlib chart images for Word/PDF reports (numbers come straight from computed series)."""
from __future__ import annotations

import uuid
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from ..config import OUTPUT_DIR  # noqa: E402

PALETTE = ["#1F4E79", "#2E86AB", "#F18F01", "#6A994E", "#C73E1D", "#8E7DBE", "#5C6B73"]
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 8.5, "axes.spines.top": False, "axes.spines.right": False,
                     "axes.grid": True, "grid.alpha": 0.25, "axes.titleweight": "bold", "axes.titlesize": 9.5})
TMP = OUTPUT_DIR / "_charts"
TMP.mkdir(parents=True, exist_ok=True)


def _save(fig, name=None) -> Path:
    p = TMP / (name or f"c_{uuid.uuid4().hex[:10]}.png")
    fig.tight_layout()
    fig.savefig(p, dpi=170)
    plt.close(fig)
    return p


def line(x, series: dict, title="", ylabel="", hline=None, band=None, size=(7.2, 2.9), every=1) -> Path:
    fig, ax = plt.subplots(figsize=size)
    for i, (k, v) in enumerate(series.items()):
        ax.plot(range(len(x)), v, color=PALETTE[i % 7], lw=1.8, label=k, marker="o" if len(x) <= 24 else None, ms=3)
    if band:
        ax.axvspan(band[0], band[1], color="#C73E1D", alpha=0.12, label=band[2] if len(band) > 2 else None)
    for h in (hline or []):
        ax.axhline(h[0], color="#C73E1D", ls="--", lw=1)
        ax.text(len(x) - 1, h[0], f" {h[1]}", va="bottom", ha="right", fontsize=7, color="#C73E1D")
    step = max(1, len(x) // 18) if every == 1 else every
    ax.set_xticks(range(0, len(x), step))
    ax.set_xticklabels([x[i] for i in range(0, len(x), step)], rotation=45, ha="right")
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    if len(series) > 1 or band:
        ax.legend(frameon=False, fontsize=7.5)
    return _save(fig)


def bar(x, values, title="", ylabel="", highlight=None, horizontal=False, size=(7.2, 2.9), fmt="{:,.0f}", hline=None) -> Path:
    fig, ax = plt.subplots(figsize=size)
    cols = [("#C73E1D" if (highlight and lab in highlight) else PALETTE[0]) for lab in x]
    if horizontal:
        bars = ax.barh(x[::-1], values[::-1], color=cols[::-1])
        for b_, v in zip(bars, values[::-1]):
            ax.text(b_.get_width(), b_.get_y() + b_.get_height() / 2, " " + fmt.format(v), va="center", fontsize=7)
        ax.grid(axis="y", alpha=0)
    else:
        bars = ax.bar(x, values, color=cols)
        for b_, v in zip(bars, values):
            ax.text(b_.get_x() + b_.get_width() / 2, b_.get_height(), fmt.format(v), ha="center", va="bottom", fontsize=7)
        plt.setp(ax.get_xticklabels(), rotation=30 if len(x) > 6 else 0, ha="right" if len(x) > 6 else "center")
        ax.grid(axis="x", alpha=0)
    for h in (hline or []):
        ax.axhline(h[0], color="#C73E1D", ls="--", lw=1)
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    return _save(fig)


def grouped(x, series: dict, title="", ylabel="", size=(7.2, 2.9)) -> Path:
    import numpy as np
    fig, ax = plt.subplots(figsize=size)
    n = len(series)
    w = 0.8 / n
    for i, (k, v) in enumerate(series.items()):
        ax.bar(np.arange(len(x)) + i * w - 0.4 + w / 2, v, w, label=k, color=PALETTE[i % 7])
    ax.set_xticks(range(len(x)))
    ax.set_xticklabels(x)
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.legend(frameon=False, fontsize=7.5)
    ax.grid(axis="x", alpha=0)
    return _save(fig)


def pie(labels, values, title="", size=(4.6, 3.0)) -> Path:
    fig, ax = plt.subplots(figsize=size)
    ax.pie(values, labels=labels, autopct="%1.1f%%", colors=PALETTE[: len(values)], startangle=90, wedgeprops={"width": 0.45}, textprops={"fontsize": 7.5})
    ax.set_title(title)
    ax.grid(False)
    return _save(fig)
