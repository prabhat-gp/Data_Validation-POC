# Office laptop — build from scratch

One command builds everything: the three databases, all source tables with
their rows (correct and deliberately wrong), the full `val_*` schema, and the
20 approved B2B rules.

```bash
git pull
venv\Scripts\activate
pip install -r backend/requirements.txt
```

> macOS/Linux: `source venv/bin/activate`

Copy `backend/.env.example` to `backend/.env` and set `DB_PASSWORD` to the
office MySQL password. Then:

```bash
cd backend
python bootstrap.py --force
```

`--force` **DROPS** `source_db`, `config_db` and `target_db` and rebuilds them.
Without it the script refuses to touch databases that already exist.

Expected:

```
  source_db
    b2bsbg              7 rows
    b2bcustomer        46 rows
    b2bproduct         35 rows
    b2bprice           38 rows
    TOTAL             126 rows
  config_db
    val_rules          20 rows
  target_db
    (empty until you run)
```

Then:

```bash
python -m uvicorn app.main:app --reload --port 8000
```

```bash
cd frontend && npm install && npm run dev
```

**Runs** → source **MySQL** → all four objects → start. Expect roughly:

| KPI | Value |
|---|---|
| Overall DQ Score | ~93% |
| Objects Checked | 4 |
| CDEs Checked | 13 of 17 |
| Records Scanned | 126 |
| Rule Coverage | 76.5% |

(126 not 128 — this laptop's `b2bproduct`/`b2bsbg` carry two CSV header rows
loaded as data by an old Workbench import. `bootstrap.py` loads cleanly.)

Optional, to give Hybris / SFDC / File Dump something on the dashboard:

```bash
python seed_dummy.py --reset
```

---

## Rebuilding the rules from scratch

Both rule sets were rewritten to be simple and readable — 20 rules each, every
rule obvious from its name. If you need to reset rules and results without
touching source data:

**1. Wipe rules and every run result** (leaves `source_db` alone):

```bash
cd backend
python - <<'EOF'
from app.database import ConfigSession, ResultsSession
from sqlalchemy import text
c, r = ConfigSession(), ResultsSession()
c.execute(text('DELETE FROM val_rules')); c.commit()
for t in ['val_violations','val_metrics','val_runs','val_batches']:
    r.execute(text(f'DELETE FROM {t}'))
for t in ['val_batches','val_runs']:
    r.execute(text(f'ALTER TABLE {t} AUTO_INCREMENT = 1'))
c.execute(text('ALTER TABLE val_rules AUTO_INCREMENT = 1')); c.commit(); r.commit()
print('rules and results cleared, ids reset to 1')
EOF
```

**2. Load both rule sets.** `--direct` writes to `val_rules` without needing
the backend up; drop it to go through the API instead.

```bash
python seed_rules_b2b.py --direct
python seed_rules_account.py --direct
```

Expect **40 rules, ids 1–40** — 20 B2B (all 9 rule types) and 20 Account
(7 types; referential integrity needs a second object and range needs a
numeric column, so neither applies to Account).

**3. Start the backend, then run from the UI:**

```bash
python -m uvicorn app.main:app --reload --port 8000
```

- **Runs → MySQL → the four B2B objects → start** — that becomes run #1
- **Runs → MySQL → Account only → start** — run #2

**4. Confirm the queries still agree with the engine:**

```bash
python test_violation_query.py
```

Must print `40 match, 0 wrong`. It generates every rule's violation query,
runs it against `source_db`, and compares the row count to what the engine
recorded. Non-zero exit if any disagree.

### Numbers from a clean rebuild

| | Run #1 (B2B) | Run #2 (Account) |
|---|---|---|
| Overall DQ Score | 92.7% | 75.6% |
| Objects | 4 | 1 |
| CDEs checked | 13 of 17 | 16 of 16 |
| Records scanned | 128 | 10 |
| Records affected | 53 | 10 |
| Critical failed | 38 | 7 |
| Rule coverage | 76.5% | 100% |

Account's 10-row sample lives in `extra/acc.csv`; load it with
`python prepare_account.py ../extra/acc.csv`. On the office laptop point that
at the real 650 MB export instead.

## Adding Account (the 650 MB export)

Do this AFTER the b2b bootstrap above. It adds a 5th object; the b2b data and
rules are untouched.

**A. Check the header matches** — reads one line, takes a second:

```bash
python prepare_account.py --inspect "C:\path\accounts.csv"
```

Every one of the 17 needed columns must say `ok`. For anything `MISSING` it
prints the near matches actually in the file — fix
`ENTITIES["Account"]["columns"]` in `app/models.py` before loading. A column
that does not match arrives as NULL and scores 0% Completeness for reasons
that have nothing to do with data quality.

**B. Slice and load** — streams the file, keeps 17 of 450 columns:

```bash
python prepare_account.py "C:\path\accounts.csv"
```

~2,700 rows/sec. 150,000 rows takes about a minute and produces a 23 MB table
from a 580 MB file. Do NOT use the Workbench import wizard — it is what put
CSV header rows into `b2bproduct` and `b2bsbg` as data.

**C. Load the 20 Account rules:**

```bash
python seed_rules_account.py            # backend running
python seed_rules_account.py --direct   # backend not running
```

**D. Run it.** Runs → source MySQL → tick **Account only** → start.
That becomes its own run, separate from the b2b run.

Expected: Rule Coverage **100% (16 of 16 elements)** — Account declares 16 CDEs
and the 20 rules judge every one. Coverage is scoped to the objects in
the selected run, so an Account-only run is measured against Account's 16
columns, not against all 33 across both datasets.

Before trusting the two ALLOWED_VALUES rules, check the real domains — a value
missing from the list reports as a failure:

```sql
SELECT Type,      COUNT(*) FROM account GROUP BY Type      ORDER BY 2 DESC;
SELECT Region__c, COUNT(*) FROM account GROUP BY Region__c ORDER BY 2 DESC;
```

---

## Doing it step by step instead

`bootstrap.py` just runs these in order. Use them individually if you only need
one part.

## 1. Pull

```bash
git pull
```

## 2. Dependencies

```bash
venv\Scripts\activate
pip install -r backend/requirements.txt
```

> macOS/Linux: `source venv/bin/activate`

```bash
cd frontend && npm install
```

## 3. Databases

`source_db` already exists. Add the other two if they are not there:

```sql
CREATE DATABASE IF NOT EXISTS config_db;
CREATE DATABASE IF NOT EXISTS target_db;
```

## 4. Config

Copy `backend/.env.example` to `backend/.env` and set `DB_PASSWORD` to the
office MySQL password. `backend/.env` is gitignored and overrides the repo-root
`.env`, so only the values that differ need to be in it.

There is no local-file fallback any more — if the config is wrong the backend
refuses to start with a clear message, instead of quietly running against an
empty database and showing a blank dashboard.

## 5. Add the new source rows

```bash
cd backend
python seed_source_data.py
```

Appends +3 SBG, +26 customers, +25 products, +28 prices. Every block is
commented with the rule it exercises, so each failure count traces back to the
rows that caused it. Safe to re-run — it deletes its own IDs first.
`--reset` removes them and leaves your originals untouched.

Expect afterwards: `b2bsbg 8 · b2bcustomer 46 · b2bproduct 36 · b2bprice 38`

## 6. Create the val_* schema

```bash
python create_tables.py
python migrate_db.py --apply
```

`create_tables.py` creates every table in `config_db` and `target_db` and seeds
the 9 rule types, 4 severities, 6 statuses. Creates **no rules**.

`migrate_db.py` is needed because `create_all()` only creates *missing tables* —
it never alters one that already exists. If `target_db` was set up before a
column was added to the models, the table stays on the old shape and every
query against it fails at run time:

```
(1054, "Unknown column 'val_runs.total_records' in 'field list'")
```

It only ever ADDs columns — never drops or rewrites anything — so it is safe on
a database with data in it, and safe to run twice. Run without `--apply` first
to see what it would do.

## 7. Start the backend

```bash
python -m uvicorn app.main:app --reload --port 8000
```

Leave it running. Open a second terminal (activate the venv there too).
Check <http://localhost:8000/docs>.

## 8. Add the rules

```bash
cd backend
python seed_rules_b2b.py
```

23 rules across the 4 entities, all 9 rule types, created **and approved**
through the API — the same path the UI uses. Only approved + active rules
execute, so this is required before a run produces anything.

## 9. Frontend

```bash
cd frontend
npm run dev
```

<http://localhost:3000>

## 10. Validate

**Runs** → Data Source **MySQL** → select all four entities → start.
Then **Dashboard** → source **MySQL** → pick that run.

You should see six KPI cards populated, a heatmap with all four objects, and
clicking any object gives the element drilldown; clicking an element row opens
the rule behind it.

The exact numbers do not need to match the home laptop — the office `val_runs`
history is its own. What confirms it works: all four objects present, scores
below 100%, and every rule type represented in the drilldown dimensions
(Completeness, Validity, Uniqueness, Consistency, Ref Integrity).

---

### Optional — demo runs for the other sources

```bash
python seed_dummy.py --reset
```

Gives Hybris / SFDC / File Dump something on the dashboard. Creates no rules
and no violations; it is dashboard filler for the demo only.

### If something looks empty

- Backend won't start → `.env` is wrong; the error names the missing variable.
- Run finishes but dashboard is blank → check the rules are `APPROVED`
  (Manage Rules page), since only those execute.
- Wrong objects showing → check the Data Source picker in the sidebar; the
  dashboard is filtered by source.

---

## Preparing the Account CSV (650 MB, 450 columns)

The export is too wide and too large for Excel, and loading it whole would move
~600 MB to store ~23 MB anyone looks at. `prepare_account.py` streams it —
one row in memory at a time — keeping only `Id` plus the 16 CDEs declared for
Account in `models.ENTITIES`.

**Step 1 — check the header before moving any data.** Reads one line, takes a
second. A 450-column export rarely uses the exact API names.

```bash
python prepare_account.py --inspect "C:\path\accounts.csv"
```

It prints every needed column as `ok` or `MISSING`, and for anything missing
suggests the near matches actually present:

```
  MISSING  Region__c
    Region__c  ->  ['REGION']
```

If anything is missing, edit `ENTITIES["Account"]["columns"]` in
`app/models.py` to the real names **before** loading. A missing column arrives
as NULL and scores 0% Completeness for reasons that have nothing to do with
data quality.

**Step 2 — slice and load.**

```bash
python prepare_account.py "C:\path\accounts.csv"
```

Creates `source_db.account` (17 TEXT columns, index on `Id`) and batch-inserts
5,000 rows at a time. Add `--out account_17col.csv` to also write the small CSV,
`--slice-only` to skip MySQL, `--limit 5000` for a trial run, `--table NAME`
for a different table name.

Measured on a synthetic 580 MB / 450-column / 150,000-row file:

| Stage | Time |
|---|---|
| slice + load to MySQL | 55 s (~2,700 rows/sec) |
| staging into `stg_account` | ~35 s |
| validating 6 rules | ~15 s |

Output CSV was 23 MB, down from 580 MB.

**Do not use the Workbench import wizard for this.** It is what put CSV header
rows into `b2bproduct` and `b2bsbg` as data, and it mangles fields containing
both a comma and embedded quotes. This script uses `DictReader`, so the header
is consumed and `0 Main St, Suite "A"` round-trips intact.

**Step 3 — the entity already points at the new table.** `models.ENTITIES`
now has Account as:

```python
"source_system": "MySQL",
"source_object_name": "account",
```

so it reads the MySQL table rather than expecting a live Salesforce
connection. Switch back to `"SFDC"` once one exists.

Then write Account rules on the Manage Rules page, approve them, and
**Runs → source MySQL → tick Account → start**.
