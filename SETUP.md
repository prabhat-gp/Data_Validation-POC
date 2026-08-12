# Setup

## Databases

Three, all MySQL.

| Database | Holds |
|---|---|
| `source_db` | the imported SFDC / Hybris tables **and** the `stg_*` staging tables |
| `config_db` | `val_rules` + the rule-type / severity / status lookups |
| `results_db` | `val_batches`, `val_runs`, `val_metrics`, `val_violations` |

Staging sits in `source_db` beside the data it stages from, so a compiled rule
never crosses a database boundary while it runs.

## From scratch on a new machine

**1. Clone and install**

```bash
git clone <repo-url>
cd smtc-data-validation
python -m venv venv
venv\Scripts\activate
pip install -r backend/requirements.txt
```

> macOS/Linux: `source venv/bin/activate`

```bash
cd frontend && npm install && cd ..
```

**2. Create the three databases**

```sql
CREATE DATABASE IF NOT EXISTS source_db;
CREATE DATABASE IF NOT EXISTS config_db;
CREATE DATABASE IF NOT EXISTS results_db;
```

**3. Point the app at them**

`backend/.env` is tracked, so the clone already has it. Set `DB_PASSWORD` to
this machine's MySQL password — everything else is already correct.

**4. Build the schema**

```bash
cd extra
python create_tables.py
```

Creates `stg_*` in `source_db`, `val_rules` + lookups in `config_db`, and the
four result tables in `results_db`. Creates **no rules**.

**5. Import the Account export**

Check the header first — reads one line, takes a second:

```bash
python prepare_account.py --inspect "C:\path\accounts.csv"
```

All 17 columns must say `ok`. Anything `MISSING`, fix
`ENTITIES["Account"]["columns"]` in `backend/app/models.py` **before** loading —
a column that does not match arrives as NULL and scores 0% Completeness for
reasons that have nothing to do with data quality.

```bash
python prepare_account.py "C:\path\accounts.csv"
```

Streams the file, keeps `Id` + the 16 CDEs, creates `source_db.account`.
~2,700 rows/sec.

**6. Load the 20 Account rules**

```bash
python seed_rules_account.py --direct
```

**7. Start it**

```bash
cd ../backend && python -m uvicorn app.main:app --reload --port 8000
```

```bash
cd frontend && npm run dev
```

**8. Run the validation**

<http://localhost:3000> → **Runs** → source **SFDC** → tick **Account** → start.

Source is **SFDC**, not MySQL — the object is labelled by where the data came
from. Every source system reads from `source_db` unless a live connection is
configured (`SFDC_DB_URL`, `HYBRIS_DB_URL`).

**9. Verify**

```bash
cd extra && python test_violation_query.py
```

Must print `20 match, 0 wrong`. Every rule's "show the failing rows" query is
generated, run against `source_db`, and its row count compared to what the
engine recorded.

## Resetting

Clear rules and run history without touching `source_db`:

```bash
cd extra
python reset_db.py            # dry run, shows what it would delete
python reset_db.py --apply
python seed_rules_account.py --direct
```

## After a pull that changes models.py

```bash
cd extra && python migrate_db.py --apply
```

`create_all()` only creates missing *tables* — it never alters an existing one.
This adds columns the models gained, so you do not get
`Unknown column 'val_runs.x' in field list` at run time.

## Scripts

Everything in `extra/` is standalone — none of it is imported by the running
app.

| Script | Does |
|---|---|
| `create_tables.py` | builds the schema across all three databases |
| `migrate_db.py` | adds columns the models gained since the DB was made |
| `reset_db.py` | wipes rules + run history, resets ids to 1 |
| `prepare_account.py` | slices a wide CSV export to the declared columns and loads it |
| `seed_rules_account.py` | loads the 20 Account rules, already approved |
| `test_violation_query.py` | proves every generated query matches the engine |
