# SAHAYAK-AI

An AI decision-support assistant that works on **any data you give it**.

- **Starts empty.** No sample data is built in.
- **Any file in, checked data out.** Every file goes through a **7-stage pipeline** that works out what each column means, cleans it, checks it and stores it.
- **Ask in plain English or Hinglish.** Get exact numbers with charts, tables and the source rows used.
- **Builds the outputs for you:** dashboards, insights, alerts, MIS reports (PDF/Word), infographics and PowerPoint decks.
- **Checks manual reports.** It compares a hand-made Excel summary with your data, cell by cell.
- **Numbers always come from code**, are calculated twice, and every step is shown.

Built for Assam Gas Company Ltd (AGCL) under the C-DAC SAHAYAK-AI brief, but nothing in it is tied to one company's files or column names.

---

## Contents

1. [What you need](#1-what-you-need)
2. [Start it (one command)](#2-start-it-one-command)
3. [First use, step by step](#3-first-use-step-by-step)
4. [Architecture](#4-architecture)
5. [Workflow A: the data pipeline (7 stages)](#5-workflow-a-the-data-pipeline-7-stages)
6. [Workflow B: asking a question](#6-workflow-b-asking-a-question)
7. [Workflow C: insights, alerts and decision support](#7-workflow-c-insights-alerts-and-decision-support)
8. [Workflow D: reports, decks and checking manual reports](#8-workflow-d-reports-decks-and-checking-manual-reports)
9. [The data dictionary](#9-the-data-dictionary)
10. [How accuracy is protected](#10-how-accuracy-is-protected)
11. [Requirements covered](#11-requirements-covered)
12. [Settings and configuration](#12-settings-and-configuration)
13. [Testing with your own data](#13-testing-with-your-own-data)
14. [Folder structure](#14-folder-structure)
15. [API reference](#15-api-reference)
16. [Known limits](#16-known-limits)
17. [Troubleshooting](#17-troubleshooting)

---

## 1. What you need

- **Python 3.10 or newer**
- **Node.js 18 or newer** (optional)
  - Without Node, the app serves the ready-built screens from the backend.
  - Run `python start.py --prod` once with Node to build them.
- **Internet on first start**, to install packages.
- **An OpenRouter key** (optional) for the language model.
  - Get one free at https://openrouter.ai/keys.
  - Without a key the app still works, using rules only.

---

## 2. Start it (one command)

```bash
python start.py
```

**What happens:**
1. Creates `.venv` and installs the Python packages (first time only, a few minutes).
2. Installs the screen packages with `npm install` (first time only).
3. Creates `.env` from `.env.example` if it does not exist.
4. Starts the **backend** at http://127.0.0.1:8000.
5. Starts the **screens** at http://localhost:5173 and opens your browser.
6. Prints `Backend ready - no data yet` on a fresh install.

**Options:**

| Command | Use it to |
|---|---|
| `python start.py --no-browser` | start without opening a browser |
| `python start.py --skip-install` | restart faster (skip installs) |
| `python start.py --prod` | build the screens once and serve everything on port 8000 |
| `python start.py --backend-port 8001 --frontend-port 5174` | use other ports |
| `start.bat` (Windows) / `./start.sh` (Mac, Linux) | same as `python start.py` |

**Turn on the language model:**
1. Open `.env`.
2. Paste your key after `OPENROUTER_API_KEY=`.
3. Restart.

The model is `openrouter/free` by default.

---

## 3. First use, step by step

1. **Open the app.** The Overview page says **"No data yet"**. Nothing is pre-loaded.
2. **Go to Pipeline** and drop your files. You can drop several at once.
   - **Tables:** CSV/TSV, Excel (.xlsx, .xls, every sheet), SQLite database, JSON / JSON lines, Parquet
   - **Documents:** PDF, Word
   - **Templates:** PowerPoint .pptx / .potx
3. **Watch each file move through the 7 stages.** Click a run to see every stage's log.
4. **If a run says "Needs your review"**, open it and check the highlighted items:
   - whether the file is a new table or more rows for an existing one
   - each column's type
   - the day/month order of dates
   - which column in the file matches which existing column

   Then press **Confirm and continue**.
5. **Open the Data dictionary** and check what the app understood:
   - rename columns
   - add the words your team uses (synonyms)
   - change how a number adds up
   - add your own formulas
6. **Go to Ask.** The suggested questions are built from your data. Click one, or type your own:
   - "Which region had the highest sales last month?"
   - "Monthly trend of complaints"
   - "kaun sa area sabse zyada loss kar raha hai?"
7. **Look at Overview.** It is now an automatic dashboard: headline numbers, trends and breakdowns for each table, plus alerts and pinned questions.
8. **Open Insights.** It lists what stands out, with evidence: trends, seasonality, sudden changes, abnormal periods, outliers, spikes and data problems.
9. **Set alerts.** Use the Alerts page or type "alert me when ... goes above ...".
10. **Build outputs.** On **Reports and decks**:
    - MIS report as PDF and Word
    - one-page infographic
    - PowerPoint deck
    - check of a manual Excel report

    Or ask in chat, e.g. "prepare the monthly MIS report" or "make 5 slides on ...". Then refine: "redo it in crore", "add a section on ...", "replace the pie chart with a bar chart".

To start over: **Data dictionary → Delete all data** (type `DELETE ALL DATA`).

---

## 4. Architecture

```
┌──────────────────────────────────────────────────────────────────────────────────────┐
│  SCREENS (React + Vite + ECharts)                              http://localhost:5173 │
│  Overview · Pipeline · Ask · Data dictionary · Insights · Alerts · Reports · Settings│
└──────────────────────────────────────┬───────────────────────────────────────────────┘
                                       │ /api/*  (JSON)
┌──────────────────────────────────────▼───────────────────────────────────────────────┐
│  BACKEND (FastAPI, Python)                                     http://127.0.0.1:8000 │
│                                                                                      │
│  1. INGESTION PIPELINE  (app/pipeline)                                               │
│     readers → profile → clean → validate → warehouse → dictionary → ready            │
│                     │ asks the user when unsure (review)                             │
│                     ▼                                                                │
│  2. WAREHOUSE + DATA DICTIONARY  (Parquet tables, versions, lineage; column meaning, │
│     units, how numbers add up, formulas found, links between tables, your edits)     │
│                     │  everything below reads only the dictionary                    │
│        ┌────────────┼──────────────┬──────────────────┬───────────────────┐          │
│        ▼            ▼              ▼                  ▼                   ▼          │
│  3. QUESTION     4. INSIGHTS    5. ALERTS +       6. REPORTS +        7. MANUAL      │
│     ENGINE          (generic       DECISION          DECKS               REPORT      │
│  (app/query)        detectors)     SUPPORT        (PDF, Word, PNG,       CHECK       │
│  rules / LLM →                     (rules, top     PPTX with native     (app/audit)  │
│  query plan →                      actions,        charts + notes)                   │
│  pandas + SQL →                    what-if,                                          │
│  answer + chart                    plan vs actual)                                   │
└──────────────────────────────────────────────────────────────────────────────────────┘
                 │ only to understand wording / write a one-line summary
                 ▼
        OpenRouter LLM (openrouter/free) - optional; never calculates numbers
```

### Who does what

| Job | Done by | Why |
|---|---|---|
| Work out what a column is | Code (profiler) | Same file → same result, every time |
| Understand a question | Rules first, LLM as backup | Works offline; the LLM handles unusual wording |
| Turn a question into a calculation | A **query plan** (table, number, how it adds up, filters, grouping, period) | Checked against the dictionary before running |
| Calculate | pandas | Exact |
| Double-check | An independent DuckDB SQL query | Catches bugs; shows "Verified: N of N" |
| One-line summary | LLM (optional) | Dropped if it contains any number not in the result |
| Find insights, alerts, actions | Code (statistics) | Every finding has its numbers and source |

### Backend modules (`backend/app/`)

| Folder / file | What it does |
|---|---|
| `pipeline/readers.py` | Opens any supported file: all sheets, header rows (including two-row headers), stacked tables, notes, JSON lists, database tables |
| `pipeline/profile.py` | Works out each column's type, role, unit, how it adds up and a confidence; detects wide layouts and per-row unit columns |
| `pipeline/clean.py` | Parsing and cleaning: numbers, dates (day/month order from evidence), months, units, spelling variants, total rows, blanks, duplicates; keys |
| `pipeline/validate.py` | Checks, quality score, quarantine; finds formulas between columns (sum, difference, product, ratio) |
| `pipeline/warehouse.py` | Parquet storage, merge by key, versions, rollback, row lineage |
| `pipeline/dictionary.py` | Data dictionary entries, your edits, custom formulas, links between tables |
| `pipeline/runner.py` | The 7 stages, logs, timing, review and resume |
| `store.py` | Loads tables + dictionary + documents; builds the catalogue and suggested questions |
| `query/understand.py` | Rules parser, LLM planner, follow-ups, validation |
| `query/engine.py` | Runs a plan in pandas **and** SQL; periods, joins through links, comparisons |
| `query/answer.py` | Answer text, headline numbers, charts, tables, sources, steps, follow-ups |
| `query/router.py` | One entry point for chat: guardrails → special requests → plan → answer |
| `query/docs.py` | Document Q&A with citations; checks a document's numbers against the data |
| `insights/engine.py` | Generic detectors (section 7) |
| `alerts/engine.py` | Alert rules on any number, automatic alerts, notifications |
| `decision/support.py` | What to act on first, what-if, plan/budget vs actual |
| `reports/` | MIS report (PDF + Word), infographic, edits by request |
| `decks/` | PowerPoint decks (overview or any topic), templates, edits by request |
| `audit/checker.py` | Checks a manual Excel report cell by cell |
| `vocabulary.yaml` | Optional words and Hinglish → English; editable |

---

## 5. Workflow A: the data pipeline (7 stages)

```
 Upload ─► 1 Ingest ─► 2 Profile & match ─► 3 Clean ─► 4 Validate ─► 5 Load ─► 6 Update dictionary ─► 7 Ready
                              │                              │
                       unsure? ▼                      bad?   ▼
                    NEEDS YOUR REVIEW                  QUARANTINED
             (new table or existing? types?          (not loaded; the reason
              date order? which column is which?)     is shown in the log)
```

| # | Stage | What happens | What you see |
|---|---|---|---|
| 1 | **Ingest** | Opens the file. Finds every sheet / table, the real header row (even if split over two rows or below a title), and separate tables stacked in one sheet. Readme/notes sheets and note lines are kept as notes. | Tables found, rows × columns, header row |
| 2 | **Profile & match** | For every column: type (number, date, date-time, month, year, financial-year label, category, ID, text, yes/no), role (number to measure / grouping / ID / time / text), how it adds up, unit (Rs, lakh, crore, SCM, MMSCM, kg, %, hours, km, bar …) and a confidence. Finds the main time column. Spots "wide" layouts (one column per month/quarter) and per-row unit columns. Decides whether the file is a **new table** or **more rows for an existing table** (by comparing columns). | One line per column; **review** if a date order is ambiguous, a type is uncertain, or the file partly matches an existing table |
| 3 | **Clean & standardise** | Numbers with commas, (brackets) as negatives, %, Rs signs. Dates in day/month order decided from the values (e.g. 23/08 proves day-first). Months like "Apr-2026", "04/2026". Units converted (per-row unit column, or file vs table unit). Spelling/case variants unified, likely typos matched. Total and sub-total rows removed. Blank rows and exact duplicates removed. Wide layouts turned into rows. Unreadable cells left blank and counted, **never guessed**. | Every change logged with counts and examples |
| 4 | **Validate** | Readable values, empty columns, time gaps, future dates, duplicate keys, sign flips, unusual values, financial-year labels vs dates. **Finds formulas between columns** (e.g. `amount = quantity × rate`, `loss = input − output`, `loss % = loss ÷ input × 100`) and counts rows that break them. Quality score. **Quarantine** if the time column is unreadable, every number is empty, or the file cannot be matched to its target table. | Checks with pass / warn / fail; formulas found |
| 5 | **Load to warehouse** | Writes Parquet. **Add/update mode:** rows with the same key (e.g. month + area, or an ID) are replaced, new rows added, so the same file twice changes nothing. **Replace mode:** the table is replaced. The last 5 versions are kept for **Roll back**. Every row remembers its source file. | New / updated / total rows, version number |
| 6 | **Update dictionary** | Rebuilds labels, units, values, time range and the formulas for averages (rates as ratio of totals). Finds **links between tables** (e.g. `invoices.customer_id → customers.customer_id`) so questions can group by columns from a linked table. Re-checks alert rules. | Tables, numbers, groupings, links found |
| 7 | **Ready** | Data is live everywhere. | Three suggested questions |

**Run results:** Loaded · Loaded with warnings · Needs your review · Quarantined · Failed.

**Where data lives.** Everything is under `data/` (or the folder set in `SAHAYAK_DATA_DIR`):

```
data/
  landing/     files exactly as uploaded
  warehouse/   tables/*.parquet, _versions/, documents/, templates/, drafts/, manifest.json, dictionary_overrides.json
  state/       run history, settings.yaml, alert rules, notifications, pins, query log, LLM cache
  outputs/     generated reports, decks, infographics, checked drafts
```

---

## 6. Workflow B: asking a question

```
Question ─► Guardrails ─► Special request? ─► Understand ─► Query plan ─► Compute (pandas) ─► Cross-check (SQL) ─► Answer
           (read-only,    (report, deck,     (rules, then   (checked     (periods, joins,      (must match         (numbers, chart,
            outside        alert, insights,   LLM backup)    against the   comparisons)          exactly)            table, source,
            topics)        what-if, docs)                    dictionary)                                             steps, follow-ups)
```

1. **Guardrails.**
   - Requests to delete or change data are refused; the app is read-only.
   - Outside topics (weather, cricket …) are declined.
2. **Special requests.** These go to their own modules: reports, decks and their edits, alerts, insights, "what to act on", what-if, plan vs actual, checking a draft or a document, and document questions.
3. **Understand.** The rules match your words to:
   - table names, column names, labels and your synonyms
   - values in the data (e.g. a region name)
   - time words: "last month", "FY 2025-26", "H1", "Q2", "between 10 Aug and 5 Sep", "last 7 days"
   - grouping: "by", "which", "-wise"
   - ranking: "top 5", "lowest"
   - comparison: "vs last year", "growth"
   - share and pie, conditions ("above 2%"), averages per day/month, counts and lists

   Follow-ups ("only for X", "and last year?", "as a pie chart") change only that part of the last question.

   If the rules are unsure and a key is set, the LLM writes the plan instead. It must use only columns and values that exist.
4. **Compute.**
   - **Rates and percentages:** total ÷ total (weighted), never a plain average.
   - **Point-in-time numbers** (balances, active connections): the latest value, never summed over time.
   - **Joins:** columns from linked tables are joined automatically.
   - **Periods:** fitted to the data, and the answer says so (e.g. "data covers Apr–Sep").
5. **Cross-check.** The same figures are computed again by an independent SQL query. The answer shows **"Verified: N of N"** or a visible mismatch.
6. **Answer.** You get:
   - headline numbers and plain-language lines
   - a chart you can switch between bar, line and pie
   - the full table
   - **the steps used** (table, measure, filters, period, grouping)
   - the source files and row counts
   - notes (e.g. "the latest month is incomplete")
   - follow-up suggestions

   You can **pin** the question to Overview.

---

## 7. Workflow C: insights, alerts and decision support

**Insights** run on every table with no knowledge of what the data is about:

| Check | Finds |
|---|---|
| Trend | Steady rise or fall over time |
| Seasonality | Months that are always high or low (needs 2+ years) |
| Step change | A jump that stays (e.g. a price revision, a new site) |
| Item jump/drop | One item's latest month far from its own usual level |
| Getting worse | An item worsening period after period while others don't |
| Abnormal window | Runs of abnormal days/hours for one item (e.g. a leak, a meter fault, an outage), with excess quantity |
| Peer outlier | An item far from its peers (rates, or per-record counts) |
| Spike in counts | Event counts (e.g. tickets by category) several times normal |
| Plan gap | Actual vs planned columns (e.g. actual vs planned minutes) per item |
| Concentration | One item making up most of the total |
| New / stopped items | Items that start late or stop reporting |
| Data quality | Pipeline warnings worth fixing at the source |

The latest month is ignored when it is only partly covered.

**Alerts:**
- **Your rules:** any number, for each item (optional), per day/week/month/quarter/year, above/below an amber and a red limit, with an owner.
- **Automatic alerts:** serious findings from Insights.
- **When they run:** after every upload and every 15 minutes. New breaches become notifications (badge on the Alerts menu).

**Decision support:**
- **"What should we act on first?"** Red alerts and the biggest findings, ranked. Each comes with a suggested action and evidence. Rupee impact is estimated when the data has both the excess quantity and a price for it.
- **"What if the price increases by 5%?"** Applies the change and carries it through the formulas found in the data (e.g. amount = quantity × rate).
- **"Did we meet the budget?"** Pairs plan/budget/target columns with the actual column that has the same unit. Shows month by month, % achieved and the months missed.

---

## 8. Workflow D: reports, decks and checking manual reports

**MIS report (PDF and Word).** Built from the most important tables, or the ones you tick. It contains:
- headline figures for the reporting month and financial year to date, each vs last year
- for each table: key numbers, a breakdown table and chart, a 24-month trend and what stands out
- alerts and action points
- a data sources and quality appendix
- "how each headline figure was computed"

Tables and figures are numbered, with a source line under each.

**Report options:** reporting month, money in Rs lakh or crore, extra sections (any question), PDF and/or Word.

**Edit by asking:** "redo it in crore", "add a section on …", "remove the section on …", "only for <item>", "for August 2026", "Word only". Only those parts change.

**One-page infographic** (PNG + PDF): four headline figures, a 12-month trend, a breakdown and the top findings.

**PowerPoint decks:**
- **Overview deck:** title, key numbers, trend and breakdown slides for the main tables, what stands out, alerts, recommended actions, data used.
- **Topic deck:** "make 5 slides on the gas loss problem in <area> in August and what we should do" gives:
  1. title
  2. the situation (that item vs its peers)
  3. what happened day by day (the abnormal period marked)
  4. what we found
  5. what we should do
- **Charts** are native and editable in PowerPoint. **Every slide has speaker notes** with the numbers and their source.
- **Your template.** Upload a .pptx/.potx on Pipeline, then say "in our template".
- **Edit by asking:** "replace the pie chart with a bar chart", "change the chart on slide 3 to line", "add a slide on …", "remove slide 4", "rename slide 2 to …". The reply lists exactly what changed.

**Check a manual report** (Reports page → *Upload and check*, or ask "check the draft"):
1. The app finds the row-name column and the number columns in your Excel.
2. It works out which table and grouping the row names belong to.
3. For every column it finds the calculation (number × how it adds up × period × unit) that reproduces most cells.
4. It flags cells that disagree, with a likely cause:
   - unit slip (×100, ×1000 …)
   - digits swapped
   - typing error
   - stale value from another month
   - value from another row
   - SUM range missing rows
   - total ≠ sum of rows
   - row missing from the draft
5. It returns a **checked copy** of your Excel: wrong cells are red, with a comment giving the correct value.

**Documents (PDF, Word):**
- Ask about them and get quoted passages with page/section.
- Ask "are the numbers in the <document> correct?". Each sentence with a number is recomputed from the data where possible.

---

## 9. The data dictionary

One page lists every table. For each column you can:
- change the **label**
- change the **role** (measure, grouping, ID, time, text, ignore)
- change **how it adds up**:
  - sum
  - average
  - ratio of totals (offered when the app found the formula)
  - latest value
  - min, max or median
- set the **unit**
- add **synonyms** (the words your team uses, e.g. "leakage" for a loss column)
- **hide** it

Saved changes apply at once to questions, dashboards, reports and alerts.

**Your own formulas.** Example: `sum(paid) / sum(billed) * 100`.
- Allowed: `sum, avg, count, count_distinct, min, max, median, last`, the operators `+ - * /` and numbers.
- After saving, ask for it by name like any column.

The page also shows the formulas found in the data, quality checks, load history (with rollback), links between tables, documents and templates.

---

## 10. How accuracy is protected

- **No fixed schema.** Every column is profiled from its values.
- **Ask, don't guess.** Ambiguous dates, uncertain types and partial table matches pause for your review.
- **Nothing is changed silently.** Every cleaning step is logged with counts and examples. Unreadable cells are left blank, never invented.
- **Totals are never double counted.** Total/sub-total rows and total columns are removed. Re-uploads update rows by key instead of adding them again.
- **Units are made consistent:** per-row unit columns, files in a different unit from their table, and lakh/crore in reports.
- **Correct averages.** Rates and percentages use total ÷ total, from formulas found in the data. Point-in-time numbers use the latest value.
- **Two independent calculations.** Every figure is computed in pandas and again in SQL; mismatches are shown, not hidden.
- **The LLM never calculates.**
  - Its plans must use columns and values that exist.
  - Its summary is dropped if it contains any number not in the result.
  - Anything it answers with free-form SQL is marked **Unverified**, and that SQL is read-only and checked.
- **Transparent answers.** Each answer shows the steps used, the source files and rows, the period actually used, and notes about gaps or incomplete months.
- **Read-only.** The app never changes your source data. A bad load can be rolled back.

---

## 11. Requirements covered

| Req | Requirement | How it is met |
|---|---|---|
| R1 | Upload many formats | CSV/TSV, Excel (.xlsx/.xls, every sheet), SQLite, JSON/JSONL, Parquet, PDF, Word, PowerPoint templates; 7-stage pipeline with review and quarantine |
| R2 | Natural-language analysis | Rules + optional LLM over the data dictionary; English and Hinglish; follow-ups; counts, totals, averages, rankings, trends, shares, comparisons, conditions, lists |
| R3 | No-code use | Every feature works from screens or chat; clarifying questions; refusal of writes; outside-topic handling |
| R4 | Rich outputs | Headline numbers, tables, bar/line/pie charts (switchable), sources, steps, cross-check stamp, pin to dashboard |
| R5 | Automated reports | MIS PDF + Word with numbered tables/figures and sources; infographic; edit by request |
| R6 | Presentations | Overview and topic decks with native charts and speaker notes; corporate template; edit by request |
| R7 | Insights | 12 generic detectors with evidence, charts and severity |
| R8 | Decision support | Alert rules + automatic alerts + notifications; what to act on first; what-if; plan vs actual |
| R9 | Fewer manual errors | Cell-by-cell check of manual Excel reports with causes and an annotated copy; document number check |
| LLM | OpenRouter `openrouter/free` | JSON-schema plans, caching, retry and rate-limit breaker, number-checked summaries; rule-based fallback |
| Start | One command | `python start.py` |

---

## 12. Settings and configuration

| Where | What |
|---|---|
| **Settings page** (saved to `data/state/settings.yaml`) | Company name, short name, financial-year start month, number style (Indian/International), pipeline strictness (when to ask, when a file joins an existing table, quarantine level) |
| `.env` | `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` (default `openrouter/free`), `LLM_ENABLED`, `LLM_NARRATION`, `LLM_TIMEOUT_SECONDS`, `AS_OF_DATE` (fix "today" for testing), `SAHAYAK_DATA_DIR` (move the data folder) |
| `backend/app/vocabulary.yaml` | Extra words people use (synonyms), Hinglish → English, and words that mean "higher is worse" (loss, delay, complaints …) |
| Data dictionary page | Everything about your tables (section 9) |

---

## 13. Testing with your own data

```bash
cd backend
python -m pytest -q                                          # uses tests/fixtures/sample_files
SAHAYAK_TEST_FILES=/path/to/your/folder python -m pytest -q  # same checks on YOUR files
```

- The tests start from an **empty** data folder and upload every file through the real pipeline.
- They are **generic**: they compare the app with direct calculations on the loaded tables and never use hard-coded answers.
- **Pipeline tests:**
  - every file loads with 7 stages
  - numbers are preserved
  - re-upload changes nothing
  - split files merge
  - data survives a restart
  - rollback works
  - unreadable dates are quarantined
  - broken files fail cleanly
  - documents are indexed
  - no value from your data appears in the app code
- **Question tests:**
  - every total matches a direct calculation
  - grouped and monthly results pass the SQL cross-check
  - counts are right
  - every suggested question answers
  - follow-ups work
  - writes are refused
  - the SQL guard blocks unsafe queries
- **Output tests:**
  - PDF/Word report, edit to crore + section
  - infographic, deck with notes and charts, deck edit
  - insights and actions, alert rule
  - **a manual report built from your data with two planted mistakes must flag exactly those two**
- **LLM tests** (fake model): valid plans are used, unknown columns are rejected, and invented numbers in summaries are dropped.

**Browser test** (start from an empty folder):

```bash
SAHAYAK_DATA_DIR=/tmp/sahayak_e2e python start.py --no-browser
python backend/tests/e2e/ui_e2e.py http://localhost:5173 shots /path/to/your/folder
```

`tests/fixtures/sample_files` holds the mentor's simulated files. They are **only used by the tests**; the app never loads them by itself. Replace them with your own files at any time.

---

## 14. Folder structure

```
sahayak-ai/
  start.py, start.sh, start.bat   one-command starter
  .env.example                    settings template (copied to .env on first start)
  data/                           your data - empty at first, never in git
  docs/                           TEST_REPORT.md, ANTIGRAVITY_PROMPTS.md
  backend/
    requirements.txt
    app/
      main.py                     API
      config.py, util.py, store.py, llm.py, verify.py, documents.py, vocabulary.yaml
      pipeline/                   readers, profile, clean, validate, warehouse, dictionary, runner
      query/                      understand, engine, answer, router, periods, docs, guardrails, nl2sql
      insights/  alerts/  decision/  reports/  decks/  audit/
    tests/                        pytest suite, e2e browser test, fixtures/sample_files
  frontend/
    src/pages/                    Overview, Pipeline, Ask, Data (dictionary), Pages (Insights, Alerts, Reports, Settings)
    src/components/               AnswerCard, Chart, DataTable, Md
```

---

## 15. API reference

Interactive docs are at **http://127.0.0.1:8000/docs**.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Status, empty or not, LLM status |
| GET / POST | `/api/settings` | Read / save settings |
| POST | `/api/pipeline/upload` | Upload files (`mode` = merge or replace) |
| GET | `/api/pipeline/runs`, `/api/pipeline/runs/{id}` | Runs and full stage logs |
| POST | `/api/pipeline/runs/{id}/resolve` | Confirm a run waiting for review |
| GET | `/api/tables`, `/api/tables/{key}` | Dictionary of all tables / one table |
| GET | `/api/tables/{key}/preview`, `/download` | First rows / clean CSV |
| POST | `/api/tables/{key}/edit` | Save dictionary edits |
| POST / DELETE | `/api/tables/{key}/metrics` | Add / remove your own formula |
| POST / DELETE | `/api/tables/{key}/rollback`, `/api/tables/{key}` | Undo the last load / remove a table |
| POST | `/api/reset?confirm=DELETE ALL DATA` | Delete everything |
| POST | `/api/ask` | Ask a question |
| GET | `/api/overview`, `/api/examples`, `/api/catalog` | Dashboard, suggested questions, catalogue |
| GET | `/api/insights`, `/api/actions` | Findings, what to act on first |
| GET / POST / DELETE | `/api/alerts`, `/api/alerts/rules` | Alerts, rules, notifications |
| POST | `/api/reports`, `/api/decks`, `/api/audit` | Build report/infographic, deck, check a draft |
| GET | `/api/files`, `/api/files/{name}` | Generated files |

---

## 16. Known limits

- **Formulas across two tables** (e.g. paid amount from one table ÷ billed amount from another) are not yet supported as custom formulas. Same-table formulas and grouping by linked tables work.
- **Scanned PDFs** (images of text) are not read; there is no OCR. Tables inside PDFs/Word files are indexed as text, not loaded as data.
- **Size.** Data is held in memory. It is comfortable up to roughly 1–2 million rows per table on a normal laptop.
- **No forecasting** yet; trends describe the past only.
- **No logins or roles** yet; anyone who can open the page can upload or delete.
- **The free LLM** can be slow or rate-limited. Simple questions are answered by rules; complex wording may need rephrasing until the model is back.

The Antigravity prompts in `docs/ANTIGRAVITY_PROMPTS.md` cover each of these.

---

## 17. Troubleshooting

| Problem | Fix |
|---|---|
| "Backend not reachable" | Check the terminal for errors; make sure port 8000 is free or use `--backend-port` |
| A run says **Needs your review** | Open it on Pipeline, check the highlighted items, press Confirm |
| A run is **Quarantined** | Open the run: the Validate stage says why. Fix the export or load it as a new table |
| A number looks wrong | Open *Details: how it was computed* under the answer. Check the table, period, filters and how the number adds up. Fix labels or how it adds up on the Data dictionary page |
| A question is not understood | Add the words you use as synonyms on the Data dictionary page, or rephrase with the column name |
| Wrong file loaded | Data dictionary → the table → **Roll back last load** |
| LLM shows "off" or "paused" | Add the key in `.env`; the free model may be rate-limited and the app works rule-based meanwhile |
| Start over | Data dictionary → Delete all data |
# AGCL_CDAC
