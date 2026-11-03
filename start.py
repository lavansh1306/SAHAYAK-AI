#!/usr/bin/env python3
"""
SAHAYAK-AI one-command starter.

    python start.py              # installs what is missing, starts backend (8000) + frontend (5173), opens the browser
    python start.py --prod       # builds the UI once and serves everything from the backend on port 8000
    python start.py --no-browser # don't open a browser tab
    python start.py --skip-install

Works on Windows, macOS and Linux. Needs Python 3.10+ (and Node 18+ for the dev frontend; without Node the
pre-built UI in frontend/dist is served by the backend instead).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
VENV = ROOT / ".venv"
IS_WIN = os.name == "nt"


def say(msg: str) -> None:
    print(f"[sahayak] {msg}", flush=True)


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")


def ensure_python_env(skip: bool) -> Path:
    if sys.version_info < (3, 10):
        sys.exit("Python 3.10 or newer is required. You have " + sys.version.split()[0])
    py = venv_python()
    if not py.exists():
        say("Creating virtual environment in .venv ...")
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV)])
    req = BACKEND / "requirements.txt"
    stamp = VENV / ".req_hash"
    h = hashlib.sha256(req.read_bytes()).hexdigest()
    if not skip and (not stamp.exists() or stamp.read_text() != h):
        say("Installing Python packages (first run takes a few minutes) ...")
        subprocess.check_call([str(py), "-m", "pip", "install", "--upgrade", "pip", "-q"])
        subprocess.check_call([str(py), "-m", "pip", "install", "-r", str(req), "-q"])
        stamp.write_text(h)
    return py


def npm_cmd() -> str | None:
    for name in (("npm.cmd", "npm") if IS_WIN else ("npm",)):
        p = shutil.which(name)
        if p:
            return p
    return None


def ensure_node(skip: bool) -> str | None:
    npm = npm_cmd()
    if not npm:
        return None
    pkg = FRONTEND / "package.json"
    stamp = FRONTEND / "node_modules" / ".pkg_hash"
    h = hashlib.sha256(pkg.read_bytes()).hexdigest()
    if not skip and (not stamp.exists() or stamp.read_text() != h):
        say("Installing frontend packages (npm install) ...")
        subprocess.check_call([npm, "install", "--no-audit", "--no-fund"], cwd=FRONTEND)
        stamp.write_text(h)
    return npm


def ensure_env_file() -> None:
    env, ex = ROOT / ".env", ROOT / ".env.example"
    if not env.exists() and ex.exists():
        shutil.copy(ex, env)
        say("Created .env - configured for local Ollama at http://127.0.0.1:11434 with model 'qwen3:4b'.")


def wait_for(url: str, seconds: int = 90) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.7)
    return False


def spawn(cmd: list[str], cwd: Path, env=None) -> subprocess.Popen:
    kw = {"cwd": str(cwd), "env": env or os.environ.copy()}
    if IS_WIN:
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(cmd, **kw)


def stop(p: subprocess.Popen | None) -> None:
    if p is None or p.poll() is not None:
        return
    try:
        if IS_WIN:
            subprocess.call(["taskkill", "/F", "/T", "/PID", str(p.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        p.wait(timeout=8)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def main() -> None:
    ap = argparse.ArgumentParser(description="Start SAHAYAK-AI (backend + frontend)")
    ap.add_argument("--prod", action="store_true", help="build the UI and serve everything from the backend on one port")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--skip-install", action="store_true")
    ap.add_argument("--backend-port", type=int, default=int(os.environ.get("BACKEND_PORT", 8000)))
    ap.add_argument("--frontend-port", type=int, default=int(os.environ.get("FRONTEND_PORT", 5173)))
    a = ap.parse_args()

    ensure_env_file()
    py = ensure_python_env(a.skip_install)
    npm = ensure_node(a.skip_install)
    prod = a.prod
    if not npm:
        if (FRONTEND / "dist" / "index.html").exists():
            say("Node.js not found - serving the pre-built UI from the backend (single port).")
            prod = True
        else:
            sys.exit("Node.js 18+ is needed to run the frontend. Install it from https://nodejs.org and run again.")
    if prod and npm and not a.skip_install:
        say("Building the UI ...")
        subprocess.check_call([npm, "run", "build"], cwd=FRONTEND)

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    back = front = None
    try:
        say(f"Starting backend on http://127.0.0.1:{a.backend_port} ...")
        back = spawn([str(py), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(a.backend_port)], BACKEND, env)
        if not wait_for(f"http://127.0.0.1:{a.backend_port}/api/health"):
            sys.exit("Backend did not start - see the messages above.")
        try:
            import json as _j, urllib.request as _u
            h = _j.loads(_u.urlopen(f"http://127.0.0.1:{a.backend_port}/api/health", timeout=5).read())
            say("Backend ready - " + ("no data yet: upload your files on the Pipeline page." if h.get("empty") else f"{h['tables']} tables and {h['documents']} documents in the warehouse."))
        except Exception:
            say("Backend ready.")
        url = f"http://localhost:{a.backend_port}"
        if not prod:
            fenv = env.copy()
            fenv["SAHAYAK_API"] = f"http://127.0.0.1:{a.backend_port}"
            say(f"Starting frontend on http://localhost:{a.frontend_port} ...")
            front = spawn([npm, "run", "dev", "--", "--port", str(a.frontend_port), "--host", "127.0.0.1"], FRONTEND, fenv)
            url = f"http://localhost:{a.frontend_port}"
            wait_for(f"http://127.0.0.1:{a.frontend_port}", 60)
        say(f"SAHAYAK-AI is running at {url}   (press Ctrl+C to stop)")
        if not a.no_browser:
            webbrowser.open(url)
        while True:
            time.sleep(1)
            if back.poll() is not None:
                say("Backend stopped.")
                break
            if front is not None and front.poll() is not None:
                say("Frontend stopped.")
                break
    except KeyboardInterrupt:
        say("Stopping ...")
    finally:
        stop(front)
        stop(back)


if __name__ == "__main__":
    main()
