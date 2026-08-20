# SMTC Data Validation Framework — Setup

Rules are authored against declared objects, compiled to SQL, and run against
staged copies of the source data. Every rule is one statement; Python loops
over rules, never over data rows.

## Databases

Three, all on one server.

| Database | Holds |
|---|---|
| `source_db` | the imported SFDC / Hybris tables **and** the `stg_*` staging tables |
| `config_db` | `val_rules` + the rule-type / severity / status lookups |
| `results_db` | `val_batches`, `val_runs`, `val_metrics`, `val_violations` |

Staging sits in `source_db` beside the data it stages from, so a compiled rule
never crosses a database boundary while it runs. Violations are written with
`INSERT..SELECT` straight from staging, which needs `results_db` on the same
server — if it is not, the engine falls back to a slower row-by-row path
automatically.

## Objects

| Object | Source | Table | Elements | Rules |
|---|---|---|---|---|
| Account | SFDC | `account` | 16 | 20 |
| B2B Customer | Hybris | `b2bcustomer` | 13 | 20 |
| B2B Unit | Hybris | `b2bunit` | 8 | 13 |
| Address | Hybris | `address` | 5 | 12 |

SFDC and Hybris are systems of record. Both are landed into `source_db` by
ETL — what an Oracle staging layer does in production.

Two foreign keys, both declared on the child:

    B2B Customer.defaultB2BUnit  ->  B2B Unit.uid
    B2B Unit.addresses           ->  Address.pk

Both objects must be in the **same run** — the join reads the lookup object's
staging table, which only exists while the batch is in flight.

---

## First-time setup

**1. Clone and install**

```bash
git clone <repo-url>
cd Data_Validation-POC
python -m venv venv
venv\Scripts\activate          # macOS/Linux: source venv/bin/activate
pip install -r backend/requirements.txt
cd frontend && npm install && cd ..
```

**2. Set the password**

`backend/.env` is tracked, so the clone already has it. Change `DB_PASSWORD`
to this machine's MySQL password — everything else is correct as shipped.

**3. Create the databases**

```sql
CREATE DATABASE IF NOT EXISTS source_db;
CREATE DATABASE IF NOT EXISTS config_db;
CREATE DATABASE IF NOT EXISTS results_db;
```

**4. Load the source data**

Put the wide exports in `data_dump/` — `Accounts.csv`, `B2BCustomer.csv`,
`B2BUnit.csv`, `Address.csv`. Names are matched loosely, a leading `sep=;`
line is handled, and Hybris's `# pk` header resolves to `pk`.

```bash
cd extra
python prepare_dump.py --inspect      # check every header, write nothing
python prepare_dump.py                # slice -> final_dump/
```

Open `final_dump/` and confirm the columns look right, then:

```bash
python prepare_dump.py --load-only    # load into source_db
```

`--load` does both steps at once if you do not need to look first.

**5. Build the schema and load the rules**

```bash
python after_pull.py --apply
```

Creates every table, migrates anything that already existed, loads all 65
rules, then verifies that each one compiles.

**6. Start it**

```bash
cd ../backend && python -m uvicorn app.main:app --reload --port 8000
cd frontend && npm run dev
```

<http://localhost:3000> → **Runs** → tick the objects → **Run Selected**.
Tick B2B Customer, B2B Unit and Address together or their foreign-key rules
have nothing to join to.

---

## After pulling changes

```bash
cd extra && python after_pull.py --apply
```

Then **restart the backend**. `ENTITIES` is read at import, so a running
server will not see catalog changes.

Add `--fresh` to wipe all rules and run history and reset the id counters, and
`--rules account|hybris|all|none` to choose what gets loaded.

---

## Server settings that matter

Measured on a 5,000,000-row run, these are not optional at scale:

| Setting | Default | Use | Why |
|---|---|---|---|
| `innodb_buffer_pool_size` | 128 MB | **3 GB+** | the working set is ~1.4 GB; at the default every read is a disk read |

On Oracle the equivalent is `SGA_TARGET` / `PGA_AGGREGATE_TARGET`. On an
8 GB box shared with the app, roughly 3 GB SGA and 1 GB PGA.

Do **not** raise `join_buffer_size`. It is allocated per connection per join
and multiplies with concurrency; an index on the join key is the real fix and
is already declared.

---

## Scripts

Everything in `extra/` is standalone — none of it is imported by the app.

| Script | Does |
|---|---|
| `after_pull.py` | runs the four below in order, then verifies. **Start here.** |
| `create_tables.py` | builds the schema across all three databases |
| `migrate_db.py` | reconciles columns, types and indexes on databases that already exist |
| `reset_db.py` | truncates rules and run history, resets ids to 1 |
| `seed_rules_account.py` | the 20 Account rules, already approved |
| `seed_rules_hybris.py` | the 45 Hybris rules, already approved |
| `prepare_dump.py` | slices wide CSV exports to the declared columns, then loads them |
| `cleanup_source.py` | finds tables in `source_db` the catalog no longer knows about |
| `test_violation_query.py` | proves every generated "show failing rows" query matches the engine |
| `predict_failures.py` | computes what each rule *should* find, independently, and compares |
| `make_scale_data.py` | generates a realistically-shaped dataset at any size, for capacity testing |

### Verifying a deployment

```bash
cd extra
python after_pull.py                  # dry run: compiles every rule
python test_violation_query.py        # every query matches the engine
python predict_failures.py --compare  # every rule matches an independent count
```

All three must be clean before a run is trusted.
