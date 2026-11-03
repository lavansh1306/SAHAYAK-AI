"""Central configuration.

- `.env` (project root): LLM key/model, data folder, fixed 'today' for testing.
- `settings.yaml` (data/state/settings.yaml, created from defaults): company name, financial-year start month,
  number style, pipeline confidence levels. Editable from the Settings page - no code change needed.

All runtime data lives under DATA_DIR (default <project>/data). The app ships with NO data.
"""
from __future__ import annotations

import copy
import os
import threading
from pathlib import Path

import yaml

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BACKEND_DIR.parent


def _load_env_file() -> None:
    env = PROJECT_DIR / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env_file()

DATA_DIR = Path(os.environ.get("SAHAYAK_DATA_DIR") or (PROJECT_DIR / "data")).resolve()
LANDING_DIR = DATA_DIR / "landing"      # files exactly as uploaded
WAREHOUSE_DIR = DATA_DIR / "warehouse"  # cleaned, versioned tables + documents + templates
STATE_DIR = DATA_DIR / "state"          # runs, dictionary edits, alerts, settings, logs
OUTPUT_DIR = DATA_DIR / "outputs"       # generated reports, decks, checked drafts
for _p in (LANDING_DIR, WAREHOUSE_DIR, STATE_DIR, OUTPUT_DIR):
    _p.mkdir(parents=True, exist_ok=True)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


class Settings:
    """LLM and runtime switches from environment variables (read lazily so tests can change them)."""

    @property
    def ollama_model(self) -> str:
        return env("OLLAMA_MODEL", "qwen3:4b")

    @property
    def ollama_base_url(self) -> str:
        url = env("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
        return url

    @property
    def llm_enabled(self) -> bool:
        return env("LLM_ENABLED", "true").lower() != "false"

    @property
    def llm_narration(self) -> bool:
        return env("LLM_NARRATION", "true").lower() != "false"

    @property
    def llm_timeout(self) -> float:
        return float(env("LLM_TIMEOUT_SECONDS", "120"))

    @property
    def as_of_date(self) -> str:
        """Optional fixed 'today' (YYYY-MM-DD) for reproducible tests."""
        return env("AS_OF_DATE", "")


settings = Settings()

# ------------------------------------------------------------------------------------- app settings
DEFAULTS: dict = {
    "company": "Assam Gas Company Ltd",
    "company_short": "AGCL",
    "fiscal_year_start_month": 4,          # April-March
    "number_style": "indian",              # indian (12,34,567.89) or international (1,234,567.89)
    "default_money_unit": "lakh",          # how reports show money when the data unit is known: lakh or crore
    "pipeline": {
        "auto_accept_confidence": 0.85,    # column types / table matches above this load without asking
        "same_table_similarity": 0.80,     # a new file joins an existing table when its columns match this much
        "review_table_similarity": 0.50,   # between this and the above, the user is asked
        "max_invalid_share": 0.20,         # more unreadable values than this in a key/time column -> quarantine
        "fuzzy_merge_similarity": 92,      # spelling variants merged inside a category column (0-100)
    },
}
_SETTINGS_PATH = STATE_DIR / "settings.yaml"
_slock = threading.RLock()


def app_settings() -> dict:
    with _slock:
        cur = copy.deepcopy(DEFAULTS)
        try:
            user = yaml.safe_load(_SETTINGS_PATH.read_text(encoding="utf-8")) or {}
        except Exception:
            user = {}
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cur.get(k), dict):
                cur[k].update(v)
            else:
                cur[k] = v
        return cur


def save_settings(patch: dict) -> dict:
    with _slock:
        cur = app_settings()
        for k, v in patch.items():
            if isinstance(v, dict) and isinstance(cur.get(k), dict):
                cur[k].update(v)
            elif k in DEFAULTS:
                cur[k] = v
        _SETTINGS_PATH.write_text(yaml.safe_dump(cur, sort_keys=False), encoding="utf-8")
        return cur


def fy_start() -> int:
    try:
        m = int(app_settings().get("fiscal_year_start_month", 4))
        return m if 1 <= m <= 12 else 4
    except Exception:
        return 4
