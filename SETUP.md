# Office laptop — bringing it up to date

Assumes the office laptop **already has** `source_db` with the original CSV
import:

| Table | Rows before |
|---|---|
| `b2bsbg` | 5 |
| `b2bcustomer` | 20 |
| `b2bproduct` | 11 |
| `b2bprice` | 10 |

Those rows stay. This adds the new rows and the new rules on top, then you run
a validation there to confirm the office laptop works end to end. Nothing is
copied across from the home laptop's run.

Commands shown for **Windows**; macOS/Linux note under each.

---

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
