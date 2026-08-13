"""
prepare_dump.py
---------------
Turns the wide source exports into narrow CSVs, then loads them into source_db.

    data_dump/Accounts.csv      450 cols -> final_dump/account.csv     17 cols
    data_dump/B2BCustomer.csv    54 cols -> final_dump/b2bcustomer.csv 14 cols
    data_dump/B2BUnit.csv        65 cols -> final_dump/b2bunit.csv      9 cols
    data_dump/Address.csv        46 cols -> final_dump/address.csv      6 cols

Each output keeps the primary key plus that object's CDEs, in the order
models.ENTITIES declares them, named after `source_object_name`.

THE USUAL SEQUENCE

    1. check every header, write nothing
        python prepare_dump.py --inspect

    2. slice -- open final_dump/ and confirm the columns look right
        python prepare_dump.py

    3. load those exact CSVs into source_db
        python prepare_dump.py --load-only

    Or steps 2 and 3 in one go
        python prepare_dump.py --load

    One object only
        python prepare_dump.py Accounts

    --in PATH     wide CSVs        (default: ../data_dump)
    --out PATH    sliced CSVs      (default: ../final_dump)
    --limit N     first N rows only, for a quick trial
    --keep        append to the source table instead of replacing it

File names are matched loosely: "Accounts.csv", "account.csv" and
"B2B Customer.csv" all resolve. The '# pk' header Hybris writes for its key
column is handled.

One row is held in memory at a time -- a 650 MB input costs a few MB of RAM.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(os.path.dirname(_HERE), "backend")
sys.path.insert(0, BACKEND)

import csv
import re
import time
import urllib.parse

from sqlalchemy import create_engine, text

try:
    from dotenv import load_dotenv
    # backend/.env is the canonical location -- the one the app reads and the
    # one that is tracked. The others are legacy, loaded first so a machine
    # that still has them keeps working.
    load_dotenv(os.path.join(os.path.dirname(_HERE), ".env"))
    load_dotenv(os.path.join(_HERE, ".env"), override=True)
    load_dotenv(os.path.join(BACKEND, ".env"), override=True)
except ImportError:
    pass

from app.models import ENTITIES            # noqa: E402

DEFAULT_IN = os.path.join(os.path.dirname(_HERE), "data_dump")
DEFAULT_OUT = os.path.join(os.path.dirname(_HERE), "final_dump")
BATCH = 5000

# A 450-column export can hold a single field longer than Python's default
# csv limit (131 072 chars) -- one long Description would abort the read.
csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))


# --------------------------------------------------------------- headers ---
def norm_header(h):
    """
    'BillingCity ' -> 'billingcity',  '# pk' -> 'pk'.

    The leading '#' is a Hybris impex artifact: its exports mark the key
    column "# pk". Stripping it here means Hybris needs no special case, and
    it cannot affect an SFDC export, which has no '#'.
    """
    return (h or "").strip().lstrip("#").strip().lower()


def match_headers(header, key, cols):
    """
    Map each wanted column to its real header. Case- and whitespace-
    insensitive, because exports routinely differ by exactly that much.
    Anything unmatched is reported rather than silently dropped -- a missing
    column becomes a table of NULLs and scores 0% Completeness for reasons
    that have nothing to do with data quality.
    """
    norm = {norm_header(h): h for h in header}
    found, missing = {}, []
    for want in [key] + cols:
        real = norm.get(want.lower())
        (missing.append(want) if real is None else found.__setitem__(want, real))
    return found, missing


def suggest(missing, header):
    out = {}
    for m in missing:
        low = re.sub(r"[^a-z0-9]", "", m.lower())
        near = [h for h in header
                if low in re.sub(r"[^a-z0-9]", "", (h or "").lower())
                or re.sub(r"[^a-z0-9]", "", (h or "").lower()) in low]
        if near:
            out[m] = near[:4]
    return out


def open_csv(path):
    """utf-8-sig strips the BOM Excel and SFDC both like to add."""
    try:
        f = open(path, newline="", encoding="utf-8-sig")
        f.readline()
        f.seek(0)
        return f
    except UnicodeDecodeError:
        print("      (not UTF-8 -- falling back to latin-1)")
        return open(path, newline="", encoding="latin-1")


# ----------------------------------------------------------- file lookup ---
def _slug(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _matches(stem, entity):
    """
    A file resolves to an entity if it slugs to the object name or the entity
    name, with or without a trailing 's' -- exports are named Accounts.csv
    about as often as account.csv.
    """
    want = {_slug(ENTITIES[entity]["source_object_name"]), _slug(entity)}
    got = _slug(stem)
    return got in want or got.rstrip("s") in {w.rstrip("s") for w in want}


def find_files(directory, only):
    out = {}
    if not os.path.isdir(directory):
        return out
    for f in sorted(os.listdir(directory)):
        stem, ext = os.path.splitext(f)
        if ext.lower() != ".csv":
            continue
        for entity in ENTITIES:
            if _matches(stem, entity) and entity not in out:
                if only and entity not in only:
                    continue
                out[entity] = os.path.join(directory, f)
                break
    return out


# ---------------------------------------------------------------- checks ---
def check(entity, path):
    meta = ENTITIES[entity]
    key, cols = meta["primary_key_field"], list(meta["columns"])
    with open_csv(path) as f:
        header = next(csv.reader(f))
    found, missing = match_headers(header, key, cols)
    mb = os.path.getsize(path) / (1024 ** 2)

    print(f"\n  {entity}  <-  {os.path.basename(path)}  "
          f"({mb:,.1f} MB, {len(header)} columns -> {len(cols) + 1})")
    renamed = 0
    for want in [key] + cols:
        real = found.get(want)
        tag = "key" if want == key else ""
        if real is None:
            print(f"      MISSING  {want:<20} {tag}")
        elif real != want:
            print(f"      ok       {want:<20} {tag}  (header says '{real}')")
            renamed += 1
    if missing:
        print(f"      {len(missing)} column(s) not found. Near matches:")
        for m, near in suggest(missing, header).items():
            print(f"        {m:<20} -> {near}")
        print(f'      Fix ENTITIES["{entity}"]["columns"] in app/models.py to the '
              "names this\n      export actually uses.")
    elif renamed == 0:
        print(f"      all {len(cols) + 1} columns matched exactly")
    return (not missing), found, [key] + cols


# ----------------------------------------------------------------- slice ---
def slice_one(entity, path, out_dir, limit):
    ok, found, order = check(entity, path)
    if not ok:
        print("      SKIPPED -- fix the header first")
        return None

    out_path = os.path.join(out_dir, f"{ENTITIES[entity]['source_object_name']}.csv")
    t0, n, blank = time.time(), 0, 0
    key = order[0]
    with open_csv(path) as fin, open(out_path, "w", newline="", encoding="utf-8") as fout:
        w = csv.writer(fout)
        w.writerow(order)
        for row in csv.DictReader(fin):
            rec = [(row.get(found[c]) or "").strip() for c in order]
            if not rec[0]:
                blank += 1
            w.writerow(rec)
            n += 1
            if n % 100000 == 0:
                print(f"      {n:>9,} rows  ({n / (time.time() - t0):,.0f}/sec)")
            if limit and n >= limit:
                break

    src_mb = os.path.getsize(path) / (1024 ** 2)
    out_mb = os.path.getsize(out_path) / (1024 ** 2)
    saved = (1 - out_mb / src_mb) * 100 if src_mb else 0
    print(f"      {n:,} rows -> {os.path.basename(out_path)}  "
          f"({src_mb:,.1f} MB -> {out_mb:,.1f} MB, {saved:,.0f}% smaller, "
          f"{time.time() - t0:,.0f}s)")
    if blank:
        print(f"      WARNING: {blank:,} rows have a blank {key} -- violations on "
              "those cannot be traced back to a record")
    return out_path


# ------------------------------------------------------------------ load ---
def source_engine():
    for v in ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "SOURCE_DB"):
        if not os.getenv(v):
            sys.exit(f"Missing {v} in backend/.env")
    pwd = urllib.parse.quote_plus(os.getenv("DB_PASSWORD"))
    return create_engine(
        f"mysql+pymysql://{os.getenv('DB_USER')}:{pwd}@{os.getenv('DB_HOST')}:"
        f"{os.getenv('DB_PORT')}/{os.getenv('SOURCE_DB')}", future=True)


def load_one(entity, csv_path, conn, keep, limit):
    """
    Loads an ALREADY-SLICED csv. Columns are text: the engine copies into
    stg_* before validating, so a source column type never reaches a rule.
    """
    meta = ENTITIES[entity]
    key, cols = meta["primary_key_field"], list(meta["columns"])
    table = meta["source_object_name"]
    order = [key] + cols

    with open_csv(csv_path) as f:
        header = next(csv.reader(f))
    found, missing = match_headers(header, key, cols)
    if missing:
        print(f"  {entity:<14} ABORTED -- sliced file is missing {missing}")
        return False

    if not keep:
        conn.execute(text(f"DROP TABLE IF EXISTS `{table}`"))
        body = ", ".join(f"`{c}` text" for c in order)
        conn.execute(text(
            f"CREATE TABLE `{table}` ({body}, KEY `ix_{table}_key` (`{key}`(64))) "
            f"ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"))
        conn.commit()

    stmt = text(f"INSERT INTO `{table}` ({', '.join(f'`{c}`' for c in order)}) "
                f"VALUES ({', '.join(f':{c}' for c in order)})")
    t0, n, batch = time.time(), 0, []
    with open_csv(csv_path) as f:
        for row in csv.DictReader(f):
            batch.append({c: (row.get(found[c]) or "").strip() for c in order})
            if len(batch) >= BATCH:
                conn.execute(stmt, batch); conn.commit(); batch = []
            n += 1
            if n % 100000 == 0:
                print(f"      {n:>9,} rows  ({n / (time.time() - t0):,.0f}/sec)")
            if limit and n >= limit:
                break
    if batch:
        conn.execute(stmt, batch); conn.commit()

    got = conn.execute(text(f"SELECT COUNT(*) FROM `{table}`")).scalar()
    print(f"  {entity:<14} {n:>9,} rows -> {os.getenv('SOURCE_DB')}.{table} "
          f"holds {got:,}  ({time.time() - t0:,.0f}s)")
    if got != n and not keep:
        print(f"      WARNING: read {n:,} but table holds {got:,}")
    return True


# ------------------------------------------------------------------ main ---
def main():
    argv = sys.argv[1:]

    def opt(name, default=None):
        flag = f"--{name}"
        if flag in argv:
            i = argv.index(flag)
            return argv[i + 1] if i + 1 < len(argv) else default
        return default

    in_dir, out_dir = opt("in", DEFAULT_IN), opt("out", DEFAULT_OUT)
    limit = int(opt("limit", 0) or 0)
    keep = "--keep" in argv
    load_only = "--load-only" in argv
    do_load = load_only or "--load" in argv

    consumed = {opt("in"), opt("out"), opt("limit")}
    only = set()
    for a in [x for x in argv if not x.startswith("--") and x not in consumed]:
        hit = next((e for e in ENTITIES if _matches(os.path.splitext(a)[0], e)), None)
        if hit is None:
            sys.exit(f"{a!r} matches no object. Known: {list(ENTITIES)}")
        only.add(hit)

    # --load-only reads the SLICED folder; everything else reads the wide one
    read_dir = out_dir if load_only else in_dir
    print(f"\n  reading from  {os.path.abspath(read_dir)}")
    files = find_files(read_dir, only)
    for e in [x for x in (only or ENTITIES) if x not in files]:
        print(f"  {e:<14} no CSV found (expected "
              f"{ENTITIES[e]['source_object_name']}.csv or similar)")
    if not files:
        sys.exit("\n  Nothing to do.")

    if "--inspect" in argv:
        ok = all(check(e, p)[0] for e, p in files.items())
        print("\n  All headers matched -- safe to run without --inspect."
              if ok else "\n  Fix the above before slicing.")
        return

    sliced = {}
    if not load_only:
        os.makedirs(out_dir, exist_ok=True)
        print(f"  writing to    {os.path.abspath(out_dir)}")
        for e, p in files.items():
            out = slice_one(e, p, out_dir, limit)
            if out:
                sliced[e] = out
        print(f"\n  {len(sliced)}/{len(files)} file(s) written to "
              f"{os.path.abspath(out_dir)}")
        if not do_load:
            print("\n  Open them and confirm the columns look right, then:")
            print("      python prepare_dump.py --load-only")
            return
    else:
        sliced = files

    if not sliced:
        return
    print(f"\n  loading into {os.getenv('SOURCE_DB')}\n")
    engine = source_engine()
    conn = engine.connect()
    try:
        done = [e for e, p in sliced.items() if load_one(e, p, conn, keep, limit)]
    finally:
        conn.close()
        engine.dispose()

    print(f"\n  {len(done)}/{len(sliced)} table(s) loaded.")
    if len(done) < len(ENTITIES):
        print("  NOTE: referential integrity needs the related objects in ONE run --\n"
              "        B2B Customer -> B2B Unit -> Address.")


if __name__ == "__main__":
    main()
