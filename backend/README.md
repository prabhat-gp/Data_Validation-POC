# SMTC Data Validation Framework — backend

FastAPI + SQLAlchemy, MySQL only. No local-file fallback: a missing or wrong
`.env` fails at startup rather than quietly running against an empty database.

## Databases

| Database | Holds |
|---|---|
| `source_db` | imported SFDC / Hybris tables **and** the `stg_*` staging tables |
| `config_db` | `val_rules` + the rule-type / severity / status lookups |
| `results_db` | `val_batches`, `val_runs`, `val_metrics`, `val_violations` |

Staging lives in `source_db` so a compiled rule — which selects from `stg_*`
and, for referential integrity, joins another `stg_*` — never crosses a
database boundary.

Connection comes from `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` plus
`SOURCE_DB` / `CONFIG_DB` / `RESULTS_DB`. See `.env.example`.

## Run

```bash
pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000
```

Setup and the standalone scripts are in `../SETUP.md` and `../extra/`.

## How execution works

Every rule compiles to **one SQL statement**. Python loops over rules (dozens),
never over data rows. A batch runs in three phases so referential integrity can
be a real `LEFT JOIN`:

```
stage all objects  ->  validate all  ->  clear staging
```

Only rules with `status = 'APPROVED' AND active = 1` execute.

`rule_definition` JSON is the source of truth — SQL is regenerated at run time
and never persisted, so a rule change takes effect on the next run with no
migration.

## Objects

`ENTITIES` in `app/models.py` is the catalog: object name, source system,
source table, primary key, and the CDE columns. Adding a Hybris object is an
entry there plus `python extra/create_tables.py` to create its staging table.

## API

- `POST|GET /api/rules`, `POST /api/rules/{id}/submit|approve|reject`
- `GET /api/rules/{id}/violation-query` — runnable SQL for the failing rows
- `GET /api/entities` — the object/element catalog the rule form uses
- `POST /api/runs/db-fetch`, `POST /api/runs/upload` — start a run
- `GET /api/runs`, `GET /api/runs/{id}` — status and progress
- `GET /api/dashboard/summary` — the whole overview in one round trip
- `GET /api/dashboard/object/{name}/drilldown`
- `GET /api/violations`, `GET /api/violations/export`

## Not built yet (by design)

- Auth — Azure Entra ID SSO, deferred
- Rule suggestion / profiling
- Remediation lifecycle on violations
