"""
prepare_account.py
------------------
Turns the 650 MB / 450-column Account export into a 17-column MySQL table.

The export is far too wide and far too large to open in Excel, and importing it
whole would move ~600 MB to store maybe 20 MB of data anyone looks at. This
streams it: one row in memory at a time, keeping only the primary key plus the
16 CDEs declared for "Account" in models.ENTITIES.

    STEP 1 -- check the header matches before moving any data (reads 1 line)
        python prepare_account.py --inspect "C:\\data\\accounts.csv"

    STEP 2 -- slice + load into source_db.account
        python prepare_account.py "C:\\data\\accounts.csv"

    Options
        --out sliced.csv    also write the 17-column CSV to disk
        --slice-only        write the CSV, do not touch MySQL
        --table NAME        target table name (default: account)
        --limit N           only process the first N rows (for a quick trial)

Memory use is flat regardless of file size -- a 650 MB or a 65 GB file both
run in a few MB of RAM.
"""

import csv
import os
import sys
import time
import urllib.parse

from sqlalchemy import create_engine, text

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(_HERE), ".env"))
    load_dotenv(os.path.join(_HERE, ".env"), override=True)
except ImportError:
    pass

from app.models import ENTITIES  # noqa: E402

ENTITY = "Account"
BATCH = 5000

# A 450-column export can carry a single field longer than Python's default
# csv limit (131 072 chars) -- a long Description will abort the read.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


def wanted_columns():
    meta = ENTITIES[ENTITY]
    return meta["primary_key_field"], list(meta["columns"])


def open_csv(path):
    """utf-8-sig strips the BOM Excel and SFDC both like to add."""
    try:
        f = open(path, newline="", encoding="utf-8-sig")
        f.readline()
        f.seek(0)
        return f
    except UnicodeDecodeError:
        print("  (not UTF-8 -- falling back to latin-1)")
        return open(path, newline="", encoding="latin-1")


def match_headers(header, key, cols):
    """
    Map each wanted column to its real position.

    Matching is case-insensitive and ignores surrounding whitespace, because
    exports routinely differ from the API name by exactly that much. Anything
    still unmatched is reported with near-misses rather than silently dropped
    -- a missing column would otherwise become a table full of NULLs and score
    0% Completeness for reasons that have nothing to do with data quality.
    """
    norm = {(h or "").strip().lower(): h for h in header}
    found, missing = {}, []
    for want in [key] + cols:
        real = norm.get(want.lower())
        if real is None:
            missing.append(want)
        else:
            found[want] = real
    return found, missing


def suggest(missing, header):
    out = {}
    for m in missing:
        low = m.lower().replace("_", "").replace(" ", "")
        near = [h for h in header
                if low in (h or "").lower().replace("_", "").replace(" ", "")
                or (h or "").lower().replace("_", "").replace(" ", "") in low]
        if near:
            out[m] = near[:4]
    return out


def inspect(path):
    key, cols = wanted_columns()
    with open_csv(path) as f:
        header = next(csv.reader(f))
    size = os.path.getsize(path) / (1024 ** 2)

    print(f"\n  file        {os.path.basename(path)}  ({size:,.0f} MB)")
    print(f"  columns     {len(header)} in the export, {len(cols) + 1} needed\n")

    found, missing = match_headers(header, key, cols)
    for want in [key] + cols:
        real = found.get(want)
        tag = "key" if want == key else ""
        if real is None:
            print(f"    MISSING  {want:<22} {tag}")
        elif real != want:
            print(f"    ok       {want:<22} {tag}  (header says '{real}')")
        else:
            print(f"    ok       {want:<22} {tag}")

    if missing:
        print(f"\n  {len(missing)} column(s) not found. Near matches in the export:")
        for m, near in suggest(missing, header).items():
            print(f"    {m:<22} -> {near}")
        print("""
  Fix by editing ENTITIES["Account"]["columns"] in app/models.py to the names
  this export actually uses. Do NOT let it load with columns missing -- they
  would arrive as NULL and read as a completeness failure.""")
    else:
        print("\n  All columns matched. Safe to run without --inspect.")
    return not missing


def source_engine():
    for v in ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "SOURCE_DB"):
        if not os.getenv(v):
            sys.exit(f"Missing {v} in .env")
    pwd = urllib.parse.quote_plus(os.getenv("DB_PASSWORD"))
    return create_engine(
        f"mysql+pymysql://{os.getenv('DB_USER')}:{pwd}@{os.getenv('DB_HOST')}:"
        f"{os.getenv('DB_PORT')}/{os.getenv('SOURCE_DB')}", future=True)


def create_table(conn, table, key, cols):
    conn.execute(text(f"DROP TABLE IF EXISTS `{table}`"))
    # TEXT throughout, matching the b2b* tables. Nothing is truncated, and the
    # engine copies into stg_account before validating anyway, so the source
    # column types never reach a rule. The Id index keeps the initial SELECT
    # and any later spot-check fast at 150k rows.
    body = ", ".join(f"`{c}` text" for c in [key] + cols)
    conn.execute(text(
        f"CREATE TABLE `{table}` ({body}, KEY `ix_{table}_key` (`{key}`(64))) "
        f"ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"))


def run(path, table, out_path, slice_only, limit):
    key, cols = wanted_columns()
    with open_csv(path) as f:
        header = next(csv.reader(f))
    found, missing = match_headers(header, key, cols)
    if missing:
        print(f"\n  ABORTED -- {len(missing)} column(s) missing: {missing}")
        print("  Run with --inspect to see near matches.")
        sys.exit(1)

    order = [key] + cols
    engine = writer = out_f = None
    if not slice_only:
        engine = source_engine()
        conn = engine.connect()
        create_table(conn, table, key, cols)
        conn.commit()
        print(f"  created {os.getenv('SOURCE_DB')}.{table} "
              f"({len(order)} columns)")
    if out_path:
        out_f = open(out_path, "w", newline="", encoding="utf-8")
        writer = csv.writer(out_f)
        writer.writerow(order)

    ph = ", ".join(f":{c}" for c in order)
    stmt = text(f"INSERT INTO `{table}` ({', '.join(f'`{c}`' for c in order)}) "
                f"VALUES ({ph})")

    t0, n, batch, blank_keys = time.time(), 0, [], 0
    with open_csv(path) as f:
        for row in csv.DictReader(f):
            rec = {c: (row.get(found[c]) or "").strip() for c in order}
            if not rec[key]:
                blank_keys += 1
            if writer:
                writer.writerow([rec[c] for c in order])
            if not slice_only:
                batch.append(rec)
                if len(batch) >= BATCH:
                    conn.execute(stmt, batch)
                    conn.commit()
                    batch = []
            n += 1
            if n % 25000 == 0:
                print(f"    {n:>9,} rows  ({n / (time.time() - t0):,.0f}/sec)")
            if limit and n >= limit:
                break

    if batch:
        conn.execute(stmt, batch)
        conn.commit()
    if out_f:
        out_f.close()
    took = time.time() - t0

    print(f"\n  {n:,} rows in {took:,.0f}s")
    if blank_keys:
        print(f"  WARNING: {blank_keys:,} rows have a blank {key} -- violations "
              f"on those cannot be traced back to a record")
    if out_path:
        mb = os.path.getsize(out_path) / (1024 ** 2)
        print(f"  wrote {out_path}  ({mb:,.1f} MB, down from "
              f"{os.path.getsize(path) / (1024 ** 2):,.0f} MB)")
    if not slice_only:
        got = conn.execute(text(f"SELECT COUNT(*) FROM `{table}`")).scalar()
        print(f"  {os.getenv('SOURCE_DB')}.{table}: {got:,} rows")
        if got != n:
            print(f"  WARNING: read {n:,} but table holds {got:,}")
        conn.close()
        engine.dispose()
        print(f"""
  Next:
    1. models.py ENTITIES["Account"] must say
           "source_system": "MySQL",
           "source_object_name": "{table}",
       so the run reads this table instead of expecting an SFDC connection.
    2. Write Account rules on the Manage Rules page and approve them.
    3. Runs -> source MySQL -> tick Account -> start.""")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        sys.exit(1)
    path = args[0]
    if not os.path.exists(path):
        sys.exit(f"No such file: {path}")

    def opt(name, default=None):
        flag = f"--{name}"
        if flag in sys.argv:
            i = sys.argv.index(flag)
            return sys.argv[i + 1] if i + 1 < len(sys.argv) else default
        return default

    if "--inspect" in sys.argv:
        inspect(path)
        return

    run(
        path,
        table=opt("table", "account"),
        out_path=opt("out"),
        slice_only="--slice-only" in sys.argv,
        limit=int(opt("limit", 0) or 0),
    )


if __name__ == "__main__":
    main()
