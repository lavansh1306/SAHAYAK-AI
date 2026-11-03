# Test report

**What was tested:** the full app, starting from an empty data folder.
**Test files used:** the mentor sample files in `backend/tests/fixtures/sample_files` (used only by tests, never shown in the app).

## Results

| Check | Result |
|---|---|
| Backend tests (`cd backend && python -m pytest -q`) | 39 passed |
| Fake-LLM tests (`tests/test_llm_mock.py`) | 3 passed |
| Browser test (`tests/e2e/ui_e2e.py`) | 26 / 26 steps passed |
| Fresh copy + `python start.py --no-browser` | Installs everything, starts backend + frontend, app is empty |
| Upload all 9 sample files from empty | All loaded through the 7 stages |
| Manual report check (seeded errors) | 6 / 6 errors found |

## What the tests check (all generic, no hard-coded numbers)

- **Pipeline:** every file loads; column types, units and "adds up by" make sense; total rows removed; bad rows quarantined with a reason; re-upload merges instead of duplicating; rollback works.
- **No data in code:** scans `backend/app` and `frontend/src` for any value from the test files (test fails if found).
- **Questions:** answers are compared with a direct pandas calculation **and** an independent SQL query.
- **Outputs:** reports (PDF / Word / infographic) and decks (PowerPoint) build and contain the right numbers.
- **LLM:** a fake LLM is used to check that wrong LLM numbers are caught and replaced.

## Test with your own data

```
cd backend
SAHAYAK_TEST_FILES=/path/to/your/folder python -m pytest -q
```
