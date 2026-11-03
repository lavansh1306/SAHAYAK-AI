"""Browser end-to-end test of the running app, starting from an EMPTY data folder.

    SAHAYAK_DATA_DIR=/tmp/sahayak_e2e python start.py --no-browser      (or uvicorn on any port)
    python backend/tests/e2e/ui_e2e.py http://localhost:5173 [screenshot_dir] [folder_with_files]

Uploads every file in the folder (default: tests/fixtures/sample_files) through the Pipeline page and then uses
every page. Checks are generic - they do not depend on what the files contain."""
import re
import sys
import tempfile
from pathlib import Path

import pandas as pd
from playwright.sync_api import expect, sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:5173"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "e2e_shots")
FILES = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(__file__).resolve().parents[1] / "fixtures" / "sample_files"
OUT.mkdir(parents=True, exist_ok=True)
results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def wait_runs_done(pg, n, timeout=240000):
    pg.wait_for_function(f"""() => {{ const r = [...document.querySelectorAll('.run')];
        return r.length >= {n} && r.every(x => !x.classList.contains('running') && !x.classList.contains('queued')); }}""", timeout=timeout)


with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1400, "height": 900})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    # 1. empty start
    pg.goto(BASE)
    pg.wait_for_selector("text=No data yet")
    pg.screenshot(path=str(OUT / "01_empty.png"))
    check("empty: overview says no data", "No data yet" in pg.inner_text("body"))
    pg.click("nav >> text=Ask")
    pg.wait_for_selector("text=Go to Pipeline")
    check("empty: no example questions", pg.locator(".chip").count() == 0)
    # 2. upload everything
    pg.click("nav >> text=Pipeline")
    pg.wait_for_selector(".pipe-map .pm")
    check("pipeline shows 7 stages", pg.locator(".pipe-map .pm").count() == 7)
    files = sorted(str(f) for f in FILES.iterdir() if f.is_file())
    pg.set_input_files("[data-testid=file-input]", files)
    pg.wait_for_selector(".run")
    wait_runs_done(pg, len(files))
    pg.wait_for_timeout(1500)
    st = pg.locator(".run-head .rs").all_inner_texts()
    check(f"all {len(files)} files processed and loaded", len(st) >= len(files) and all(s.startswith("Loaded") or s.startswith("Needs") for s in st[:len(files)]))
    pg.screenshot(path=str(OUT / "02_pipeline.png"), full_page=True)
    first = pg.locator(".run").first
    first.locator(".run-head").click()
    pg.wait_for_selector(".run-body .stage")
    check("run shows 7 stages with logs", first.locator(".run-body .stage").count() == 7)
    check("run shows profiled columns", first.locator(".map-chip").count() >= 2)
    pg.screenshot(path=str(OUT / "03_run.png"), full_page=True)
    first.locator(".run-head").click()
    # 3. a file needing review (same data as an existing table, half the columns renamed)
    csvs = [f for f in FILES.iterdir() if f.suffix == ".csv"]
    if csvs:
        df = pd.read_csv(csvs[0])
        ren = {c: f"field_{i}" for i, c in enumerate(df.columns) if i % 2 == 0}
        tmp = Path(tempfile.mkdtemp()) / "renamed_export.csv"
        df.rename(columns=ren).head(200).to_csv(tmp, index=False)
        pg.set_input_files("[data-testid=file-input]", [str(tmp)])
        pg.wait_for_function("() => { const r = document.querySelector('.run'); return r && r.textContent.includes('renamed_export') && !r.classList.contains('running') && !r.classList.contains('queued') }", timeout=60000)
        top = pg.locator(".run").first
        if "needs_review" in (top.get_attribute("class") or ""):
            if not top.locator(".review-panel").count():
                top.locator(".run-head").click()
            pg.wait_for_selector(".review-panel")
            pg.screenshot(path=str(OUT / "04_review.png"), full_page=True)
            check("review panel lists every column", pg.locator(".review-panel select[aria-label^='Type of']").count() >= 3)
            pg.wait_for_timeout(800)
            check("review offers matching to existing columns", pg.locator(".review-panel select[aria-label^='Existing column for']").count() >= 3)
            for orig, new in ren.items():
                sel = pg.locator(f".review-panel select[aria-label='Existing column for {new}']")
                if sel.count():
                    sel.select_option(re.sub(r"[^a-z0-9]+", "_", orig.lower()).strip("_"))
            pg.locator(".review-panel button", has_text="Confirm and continue").click()
            pg.wait_for_function("() => { const r = document.querySelector('.run .rs'); return r && /Loaded/.test(r.textContent) }", timeout=60000)
            check("confirmed file loads", True)
        else:
            check("renamed file processed", "Loaded" in top.locator(".rs").first.inner_text())
    # 4. overview dashboard
    pg.click("nav >> text=Overview")
    pg.wait_for_selector(".dash-card")
    pg.wait_for_timeout(800)
    pg.screenshot(path=str(OUT / "05_overview.png"), full_page=True)
    check("overview has dashboard cards", pg.locator(".dash-card").count() >= 2)
    check("overview has charts", pg.locator(".dash-card .chart-box svg").count() >= 2)
    # 5. data dictionary edit
    pg.click("nav >> text=Data dictionary")
    pg.wait_for_selector("table.dict")
    check("dictionary lists tables", pg.locator(".dict-item").count() >= 2)
    syn = pg.locator("input[aria-label^='Synonyms of']").first
    syn.fill("zebra figure")
    pg.click("text=Save changes")
    pg.wait_for_timeout(800)
    pg.screenshot(path=str(OUT / "06_dictionary.png"), full_page=True)
    # 6. ask
    pg.click("nav >> text=Ask")
    pg.wait_for_selector(".chip")
    check("example questions built from the data", pg.locator(".chip").count() >= 5)
    def ask(q, shot):
        n = pg.locator(".answer").count()
        pg.fill("textarea", q)
        pg.keyboard.press("Enter")
        expect(pg.locator(".answer")).to_have_count(n + 1, timeout=60000)
        pg.wait_for_timeout(700)
        pg.locator(".answer").last.scroll_into_view_if_needed()
        pg.screenshot(path=str(OUT / shot))
        return pg.locator(".answer").last
    chip = pg.locator(".chip").nth(1).inner_text()
    a = ask(chip, "07_answer.png")
    check("example question answered and verified", "Verified" in a.inner_text())
    check("answer has a chart or numbers", a.locator(".chart-box svg").count() >= 1 or a.locator(".readout").count() >= 1)
    a = ask("zebra figure by month", "08_synonym.png")
    check("synonym from the dictionary is understood", "Verified" in a.inner_text())
    a = ask("Delete all records.", "09_refusal.png")
    check("destructive request refused", "read-only" in a.inner_text().lower())
    a = ask("What unusual things do you see in the data?", "10_insights_answer.png")
    check("insights answer", "finding" in a.inner_text().lower())
    # 7. insights, alerts
    pg.click("nav >> text=Insights")
    pg.wait_for_selector(".finding", timeout=60000)
    check("insights page shows findings", pg.locator(".finding").count() >= 3)
    pg.screenshot(path=str(OUT / "11_insights.png"))
    pg.click("nav >> text=Alerts")
    pg.wait_for_selector("text=New alert")
    pg.wait_for_timeout(800)
    pg.fill("label:has-text('Red limit') input", "0")
    pg.click("text=Save alert")
    pg.wait_for_timeout(1500)
    check("alert rule saved", pg.locator("text=Your alert rules").count() == 1 and "No rules yet" not in pg.inner_text("body"))
    pg.screenshot(path=str(OUT / "12_alerts.png"))
    # 8. reports, deck, audit
    pg.click("nav >> text=Reports and decks")
    pg.wait_for_selector("text=Build report")
    pg.click("text=Build report")
    pg.wait_for_selector(".page .answer >> text=Report created", timeout=120000)
    check("report built (PDF + Word)", pg.locator(".page .answer a.file-link").count() == 2)
    pg.click("text=Build deck")
    pg.wait_for_selector(".page .answer >> text=Deck created", timeout=120000)
    href = pg.locator(".page .answer a.file-link").first.get_attribute("href")
    r = pg.request.get(BASE + href)
    check("deck downloads", r.status == 200 and len(r.body()) > 20000)
    pg.click("text=One-page infographic")
    pg.wait_for_selector(".page .answer >> text=Infographic created", timeout=120000)
    pg.screenshot(path=str(OUT / "13_outputs.png"), full_page=True)
    check("infographic built", pg.locator(".page .answer img.preview-img").count() == 1)
    pg.click("nav >> text=Settings")
    pg.wait_for_selector("text=Financial year starts in")
    check("settings page", True)
    # 9. mobile
    m = b.new_page(viewport={"width": 390, "height": 844})
    m.on("pageerror", lambda e: errors.append(str(e)))
    m.goto(BASE)
    m.wait_for_selector(".dash-card")
    m.screenshot(path=str(OUT / "14_mobile.png"))
    sw = m.evaluate("document.documentElement.scrollWidth")
    m.click("nav >> text=Pipeline")
    m.wait_for_selector(".run")
    sw2 = m.evaluate("document.documentElement.scrollWidth")
    check("mobile: no horizontal page scroll", sw <= 392 and sw2 <= 392)
    check("no JS errors", not errors)
    if errors:
        print(errors[:5])
    b.close()

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} UI checks passed")
sys.exit(1 if failed else 0)
