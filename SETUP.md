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

**5. Import the source exports**

Put the wide CSVs in `data_dump/` — `Accounts.csv`, `B2BCustomer.csv`,
`B2BUnit.csv`, `Address.csv`. Names are matched loosely, so `account.csv` and
`B2B Customer.csv` work too.

Check the headers first — reads one line per file, takes a second:

```bash
python prepare_dump.py --inspect
```

Every column must say `ok`. Anything `MISSING`, fix that object's `columns` in
`backend/app/models.py` **before** loading — a column that does not match
arrives as NULL and scores 0% Completeness for reasons that have nothing to do
with data quality.

```bash
python prepare_dump.py
```

Streams each file, keeps the primary key + that object's CDEs, and writes
`final_dump/<table>.csv`. A 525 MB / 450-column Account export comes out at
9 MB in ~20s. Open them and confirm the columns look right, then load:

```bash
python prepare_dump.py --load-only
```

Creates `source_db.account` / `.b2bcustomer` / `.b2bunit` / `.address`.
`--load` does both steps in one pass if you do not need to look first.

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
| `prepare_dump.py` | slices wide CSV exports to the declared columns, then loads them |
| `cleanup_source.py` | finds tables in source_db the catalog no longer knows about |
| `after_pull.py` | runs the above in order after a pull, then verifies |
| `seed_rules_account.py` | loads the 20 Account rules, already approved |
| `seed_rules_hybris.py` | loads the 39 Hybris rules, already approved |
| `test_violation_query.py` | proves every generated query matches the engine |
