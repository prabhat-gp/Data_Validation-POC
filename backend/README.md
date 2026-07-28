# SMTC Data Validation Framework — Backend (V1)

## Setup
```bash
cd backend
python3 -m pip install -r requirements.txt
python3 create_tables.py          # creates the 7 tables + seeds Account/16 CDEs. NO rules seeded.
python3 -m uvicorn app.main:app --reload --port 8000
```
Default DB is a local SQLite file `smtc_dq.db` (gitignored). To point at Oracle/Postgres/MySQL instead,
set `DATABASE_URL` before running, e.g.:
```bash
export DATABASE_URL="oracle+oracledb://user:pass@host:1521/service"
```

## Prove it works against real data (before running on the office laptop)
```bash
python3 tests/test_engine_backtest.py
```
Runs the full pipeline (stage → compile rules → execute → violations → metrics) against
`../data_dump/temp.csv` (101 real Account rows) in an isolated in-memory DB — doesn't touch `smtc_dq.db`.

## Running against the real ~700MB accounts.csv (office laptop)
Nothing about the code changes. Same `create_tables.py`, same API, same upload endpoint —
just a bigger file and it takes longer. The engine never loads the whole file into memory
(chunked staging, batched violation writes), and validation runs as a background task so the
upload request itself doesn't time out.

## API surface
- `GET  /api/objects`, `GET /api/objects/{id}/elements` — catalog (read-only in V1)
- `POST /api/rules`, `GET /api/rules`, `POST /api/rules/{id}/submit|approve|reject`
- `POST /api/rules/{id}/preview?run_id=...` — dry-run a rule against already-staged data before approving
- `POST /api/runs/upload` (multipart file) — triggers a run in the background, returns immediately
- `POST /api/runs/db-fetch` (connection_url + query) — same, for the DB-fetch source path
- `GET  /api/runs`, `GET /api/runs/{id}` — poll run status
- `GET  /api/dashboard/kpis|heatmap|top-failing|trend|fix-profile` — all read from DQ_METRIC only
- `GET  /api/dashboard/object/{id}/drilldown`
- `GET  /api/violations?run_id=...` (paginated), `GET /api/violations/export` (streamed CSV)

## What's NOT built yet (by design, per current phase)
- Rule suggestion / profiling automation (Phase 6 in the earlier plan)
- Auth (explicitly deferred)
- Remediation lifecycle on violations (explicitly out of V1 scope)
