# SMTC Data Validation Framework — Backend

FastAPI + SQLAlchemy. MySQL only — there is no local-file fallback, so a
missing `.env` fails at startup instead of silently starting against an empty
database.

## Databases

| DB | Holds |
|---|---|
| `source_db` | the `b2b*` tables being validated — the app only ever reads these |
| `config_db` | `val_rules` + the rule-type / severity / status lookups |
| `target_db` | `val_batches`, `val_runs`, `val_metrics`, `val_violations`, staging |

Connection comes from `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` plus
`SOURCE_DB` / `CONFIG_DB` / `TARGET_DB`. Repo-root `.env` is read first,
`backend/.env` overrides it. See `.env.example`.

## Setup

```bash
pip install -r requirements.txt
python create_tables.py
python -m uvicorn app.main:app --reload --port 8000
```

`create_tables.py` creates every `val_*` table and seeds the 9 rule types, 4
severities and 6 statuses. It creates **zero rules** — rules are authored and
approved by users through the UI. `--reset` drops and recreates.

## Scripts

| Script | Does |
|---|---|
| `prepare_account.py` | slices the 650MB / 450-column Account export down to the 17 declared columns and loads it into `source_db.account`; `--inspect` checks the header first |
| `bootstrap.py` | **builds everything from nothing** — 3 databases, source tables + rows, val_* schema, 23 approved rules. `--force` drops the databases first |
| `create_tables.py` | schema + lookup seeding |
| `migrate_db.py` | adds columns the models declare but the DB lacks, and backfills rule/metric dimensions after a reclassification — `create_all()` never alters an existing table, so this is what fixes `Unknown column ...` after a pull. Only ever ADDs; `--apply` to commit |
| `seed_source_data.py` | appends the extra `b2b*` rows so every rule type has both passes and failures; re-running is safe, `--reset` removes them |
| `seed_rules_b2b.py` | creates + approves 23 rules covering all 9 types, through the API (backend must be up) |
| `seed_dummy.py` | dashboard demo runs for Hybris / SFDC / File Dump only — no rules, no violations |

## How execution works

Every rule compiles to **one SQL statement**. Python loops over rules (dozens),
never over data rows. A batch runs in three phases so referential integrity can
be a real `LEFT JOIN`:

```
stage all entities  ->  validate all  ->  clear staging
```

Only rules with `status = 'APPROVED' AND active = 1` execute.

`rule_definition` JSON is the source of truth — SQL is regenerated at run time
and never persisted, so a rule change takes effect on the next run with no
migration.

## API surface

- `POST|GET /api/rules`, `POST /api/rules/{id}/submit|approve|reject`
- `POST /api/rules/{id}/preview?run_id=...` — dry-run against staged data before approving
- `GET /api/entities`, `GET /api/entities/meta/rule-types`
- `POST /api/runs/db-fetch`, `POST /api/runs/upload` — start a run in the background
- `GET /api/runs`, `GET /api/runs/{id}` — poll status and progress
- `GET /api/dashboard/summary` — the whole overview in one round trip
- `GET /api/dashboard/object/{name}/drilldown`
- `GET /api/violations?run_id=...`, `GET /api/violations/export`

## Not built yet (by design)

- Auth — Azure Entra ID SSO, deferred
- Rule suggestion / profiling automation
- Remediation lifecycle on violations
