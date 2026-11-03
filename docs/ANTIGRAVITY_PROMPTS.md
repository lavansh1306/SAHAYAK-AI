# Antigravity prompts: what to build next

**Where the project stands now:**
- It works on any data.
- It has the 7-stage pipeline, the data dictionary, plain-English questions with a SQL cross-check, a dashboard, insights, alerts, decision support, reports, decks and the manual-report check.
- These prompts add what is still missing (README section 16) and make it production-ready.

**How to use this file:**
1. Open the `sahayak-ai` folder in Antigravity.
2. Give **one prompt at a time, in order**.
3. After each prompt, ask Antigravity to run the tests and paste the result:
   ```
   cd backend && python -m pytest -q
   ```
4. Then test with **your own files**:
   ```
   cd backend && SAHAYAK_TEST_FILES=/path/to/your/folder python -m pytest -q
   ```
5. Do not move on until all tests pass.

---

## The 5 golden rules (paste them at the top of every prompt)

```
GOLDEN RULES - follow them in every change:
1. The app must work on ANY data. Never hard-code table names, column names, area/item names, IDs, months or values
   from any file into backend/app or frontend/src. Read everything from the data dictionary (backend/app/store.py,
   backend/app/pipeline/dictionary.py). The test tests/test_pipeline.py::test_no_data_values_in_app_code must keep passing.
2. The LLM never calculates numbers. Numbers come from backend/app/query/engine.py (pandas) and are cross-checked by
   an independent SQL query. Any new calculation needs the same two-way check.
3. The pipeline never guesses silently. Every change to data is logged in the run; anything uncertain goes to review;
   bad data is quarantined with a clear reason.
4. Keep the app starting EMPTY, and keep "python start.py" as the single start command.
5. All existing tests must keep passing; add tests for every new feature. Tests must be generic (compare with direct
   calculations on whatever files are loaded), never hard-coded numbers.
```

---

## Part A: Get to know the code (no changes)

### Prompt 1: Explain the project back to me
```
<GOLDEN RULES>
Read README.md, backend/app/pipeline/*.py, backend/app/query/*.py and backend/app/store.py. Do not change code.
Explain in simple bullet points:
- the 7 pipeline stages and which function runs each
- how a column's type, role, unit and "adds up by" are decided (profile.py) and when the user is asked
- how a question becomes a query plan, how the plan is computed and how the SQL cross-check works
- where the LLM is used and where it is not
Then run: cd backend && python -m pytest -q   and report the result.
```

### Prompt 2: Make a safety net
```
<GOLDEN RULES>
Create a git commit called "baseline". After every later task: run the full test suite, run the browser test
(backend/tests/e2e/ui_e2e.py against an empty data folder), and give me a short bullet list of what changed.
```

---

## Part B: Make the pipeline stronger

### Prompt 3: Formulas across two tables
```
<GOLDEN RULES>
Today a custom formula can only use columns of ONE table (backend/app/pipeline/dictionary.py parse_metric,
backend/app/query/engine.py _metric_eval / sql_measure). Allow formulas that use a column from a LINKED table, e.g.
  sum(payments.amount) / sum(invoices.amount) * 100   grouped by a column both can reach (like area via customers).
Use the links in store.links (child -> parent, many-to-one). Aggregate each side separately at the requested grouping
and period, then combine - never join row by row in a way that multiplies rows. Add the SQL cross-check for it.
Add a "Cross-table formula" option on the Data dictionary page. Add generic tests that build such a formula from
whatever linked tables exist in the test files and compare it with a direct pandas calculation.
```

### Prompt 4: Scanned PDFs and tables inside documents
```
<GOLDEN RULES>
1) If a PDF has almost no text (scanned), run OCR (pytesseract if installed; otherwise show a clear warning in the
   Load stage saying OCR is not available). 2) Extract tables from PDFs (pdfplumber) and Word files (python-docx) and
   offer each one in the review step as "load as a table" (default: do not load - keep as document text). Log every
   table found in the Ingest stage. Add tests with documents from the test folder (skip if none).
```

### Prompt 5: Big files
```
<GOLDEN RULES>
Make the pipeline handle 5 million rows on a laptop: read CSV in chunks, profile on a sample of 50,000 rows, write
Parquet in row groups, and let engine.py run queries through DuckDB directly on the Parquet files for large tables
(keep the pandas path for small tables and keep the cross-check by running a second, differently written SQL).
Report timings per stage in the run log. Add a test that generates a large CSV inside the test from an existing test
file (repeat its rows) and checks the totals are exact.
```

### Prompt 6: Connect to databases and folders, not only uploads
```
<GOLDEN RULES>
Add "sources" on the Pipeline page: (1) a watch folder (path in .env SAHAYAK_INBOX) checked every 10 minutes,
(2) a database connection (PostgreSQL / SQL Server / MySQL via SQLAlchemy, read-only user, a list of tables or a
SELECT query), (3) a REST/JSON URL. Each fetch becomes a normal pipeline run with the same 7 stages, review and
quarantine. Store connection secrets only in .env, never in the data folder. Add tests with SQLite via SQLAlchemy
and a temporary folder.
```

### Prompt 7: Smarter column matching for next month's file
```
<GOLDEN RULES>
When a new file partly matches an existing table (runner._match_table), also compare VALUES, not only names:
a renamed column whose values overlap the existing column's values (categories) or whose range/distribution is very
similar (numbers, dates) should be proposed as the match with a confidence. Show the evidence in the review panel
("92% of values also appear in 'region'"). Remember confirmed renames per source file name so next month is automatic.
Add generic tests that rename columns of a test file and check they are matched back correctly.
```

### Prompt 8: More date and period formats
```
<GOLDEN RULES>
Support in profile.py / clean.py / query/periods.py: fiscal labels like "Q1 FY27", "2026-27 Q2", "H1 2026-27",
week numbers ("2026-W14"), and Excel dates stored as text numbers. In questions support "last 2 quarters", "same
month last year", "year to date", "previous financial year", Hindi month names. Add unit tests for each format and
check the period shown in answers is right.
```

---

## Part C: Make answers better

### Prompt 9: Forecasting
```
<GOLDEN RULES>
Add "forecast" questions ("forecast sales for the next 3 months"). Use a simple, explainable method chosen by the data:
seasonal naive or Holt-Winters (statsmodels) when 24+ months exist, else linear trend. Always show a range (80% interval),
the method used, and the back-test error on the last 6 months. Mark forecasts clearly as estimates on charts (dashed
line) and never mix them into actual totals. Add generic tests using the monthly series in the test files.
```

### Prompt 10: Clarify instead of guess
```
<GOLDEN RULES>
In query/understand.py: when two tables or two numbers score within 10% of each other, or a value matches two
different columns, return ONE clarifying question with clickable options (built from the dictionary labels) instead of
picking one. Remember the user's choice for the session. Add generic tests that create such ambiguity from the test
files (e.g. two tables with a column of the same name).
```

### Prompt 11: Hindi script and voice
```
<GOLDEN RULES>
Accept questions in Devanagari Hindi (translate common words with the vocabulary file and, if the LLM is available,
let it rewrite the question into English before planning - numbers still computed by code). Add a microphone button
in Ask using the browser's speech recognition (Hindi and English). Add tests for 20 Hindi/Hinglish questions built
from dictionary labels.
```

### Prompt 12: Better LLM use with free models
```
<GOLDEN RULES>
In backend/app/llm.py: allow a list of models in .env (OPENROUTER_MODELS=openrouter/free,model2,...) tried in order
when one is rate-limited; record which model answered; show it in Settings. Add a "self-check" step: after the LLM
writes a plan, ask it (cheaply) whether the plan answers the question; if not, ask the user. Keep the fake-LLM tests
working and add new ones.
```

---

## Part D: Make outputs better

### Prompt 13: Saved report templates and scheduled reports
```
<GOLDEN RULES>
Let users save a report layout (tables, sections/questions, unit, title) as a named template on the Reports page and
re-run it for any month. Add scheduling: e.g. "every month on the 5th" - the report is built automatically for the
previous month and kept in Generated files; if SMTP settings exist in .env, email it to a list. Add tests that build a
saved template for two different months and check the figures change with the month.
```

### Prompt 14: Excel export and chart images
```
<GOLDEN RULES>
Under every answer add "Download as Excel" (the full table with a second sheet listing the steps, sources and the
SQL used) and "Download chart as PNG". Add tests that open the Excel and compare it with the answer table.
```

### Prompt 15: Deck templates with placeholders
```
<GOLDEN RULES>
When a corporate template is used, map content into its named placeholders/layouts (title, content, chart, footer)
instead of drawing boxes on top; keep fonts and colours from the template. Let the user pick which layout is used for
chart slides. Test with any .potx/.pptx in the test folder (skip if none): every slide must use a template layout.
```

---

## Part E: Ready for real use

### Prompt 16: Logins and roles
```
<GOLDEN RULES>
Add login (hashed passwords in data/state/users.json; first start creates an admin from .env). Roles: viewer (Ask,
Overview, Insights, Reports), data_admin (+ Pipeline, review, dictionary edits, rollback, delete), admin (+ settings,
delete all data). Protect every API endpoint. Keep "python start.py" working. Add tests for each role.
```

### Prompt 17: Audit trail
```
<GOLDEN RULES>
Log every upload, review decision, dictionary edit, rollback, delete, alert change and report/deck build with user,
time and details to data/state/audit_log.jsonl and show it on a "History" tab of the Data dictionary page. Add tests.
```

### Prompt 18: Deployment
```
<GOLDEN RULES>
Add a Dockerfile and docker-compose.yml (backend serving the built frontend on one port, data folder as a volume),
a backup script for the data folder, upload size limits, and a health check. Document it in README. Keep
"python start.py" for laptops.
```

### Prompt 19: Your own golden questions
```
<GOLDEN RULES>
Create backend/tests/test_golden.py that reads an Excel file named by the env variable SAHAYAK_GOLDEN (columns:
question, expected value, tolerance, unit) and, with the files in SAHAYAK_TEST_FILES loaded, asks every question and
compares the first headline number with the expected value. Skip when the env variables are not set (so no real data
or answers are ever committed). Print a score table.
```

### Prompt 20: Final check before a demo
```
<GOLDEN RULES>
Do a full release check and give me a bullet list of results:
1) cd backend && python -m pytest -q  (all pass)
2) SAHAYAK_TEST_FILES=<my folder> python -m pytest -q  (all pass)
3) start the app on an empty data folder and run backend/tests/e2e/ui_e2e.py with my folder (all pass)
4) copy the project to a new folder without .venv and node_modules, run "python start.py --no-browser" and confirm
   both backend and frontend start and the app is empty
5) update README.md (features, limits) and docs/TEST_REPORT.md
```

---

## Tips when working with Antigravity

- **One prompt at a time.** Wait for tests to pass before the next one.
- **If it breaks something:** say *"Revert to the last commit and do it in smaller steps."*
- **If it hard-codes names:** say *"Remove every hard-coded name and read it from the data dictionary instead (golden rule 1)."*
- **If it lets the LLM produce numbers:** say *"Move the calculation into query/engine.py and add the SQL cross-check (golden rule 2)."*
- **If a change quietly alters data:** say *"Log it in the run and, if unsure, send it to review (golden rule 3)."*
- **Never paste real company data into code or tests.** Use `SAHAYAK_TEST_FILES` and `SAHAYAK_GOLDEN` (Prompt 19).
