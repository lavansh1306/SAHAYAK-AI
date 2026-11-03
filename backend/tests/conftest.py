"""Tests start from an EMPTY data folder and push every file in the test folder through the real pipeline.

The folder defaults to tests/fixtures/sample_files; point SAHAYAK_TEST_FILES at any folder of your own files to run
the same checks on your data. The checks are generic: they compare the app against direct calculations on the
loaded tables, never against hard-coded numbers."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["SAHAYAK_DATA_DIR"] = tempfile.mkdtemp(prefix="sahayak_test_")
os.environ.setdefault("AS_OF_DATE", "")
os.environ["LLM_ENABLED"] = os.environ.get("TEST_LLM_ENABLED", "false")
FILES = Path(os.environ.get("SAHAYAK_TEST_FILES") or (ROOT / "tests" / "fixtures" / "sample_files"))


def data_files():
    return sorted(p for p in FILES.iterdir() if p.is_file() and not p.name.startswith("."))


@pytest.fixture(scope="session", autouse=True)
def store():
    from app.store import STORE, init_store
    init_store()
    assert STORE.is_empty(), "the app must start with no data"
    runs = {}
    for f in data_files():
        runs[f.name] = STORE.load_file(f)
    STORE.runs = runs
    return STORE


@pytest.fixture()
def ask():
    import uuid
    from app.query.router import ask as _ask
    sid = uuid.uuid4().hex[:6]
    return lambda q: _ask(q, sid)
