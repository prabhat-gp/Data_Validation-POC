"""
bootstrap.py
------------
Builds the ENTIRE application from nothing, in one command.

    python bootstrap.py            # build; refuses if databases already exist
    python bootstrap.py --force    # DROP the three databases first, then build

What it creates:

    source_db   b2bsbg / b2bcustomer / b2bproduct / b2bprice
                44 base rows loaded from data_dump/*.csv
                + 82 seeded rows so every rule type has passes AND failures
                = 126 rows
    config_db   val_rules (23 approved rules) + the three lookup tables
    target_db   val_batches / val_runs / val_metrics / val_violations + staging

After this, start the backend and the frontend and trigger a run from the UI.
Nothing else needs seeding.

Reads DB_HOST / DB_PORT / DB_USER / DB_PASSWORD and SOURCE_DB / CONFIG_DB /
TARGET_DB from the repo-root .env, overridden by backend/.env.
"""

import csv
import json
import os
import sys
import urllib.parse
from datetime import datetime, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, ".env"))
    load_dotenv(os.path.join(_HERE, ".env"), override=True)
except ImportError:
    pass

DUMP = os.path.join(_ROOT, "data_dump")

# The four source tables. Column types match the original CSV import: TEXT
# everywhere except the two numeric price columns, because the engine's CAST
# behaviour on RANGE rules was tuned against exactly these types.
SOURCE_TABLES = {
    "b2bsbg": (
        "(`sbg_id` text, `sbg_name` text)",
        ["sbg_id", "sbg_name"], set(),
    ),
    "b2bcustomer": (
        "(`customer_id` text, `customer_name` text, `email` text, "
        "`status` text, `region` text, `sbg_id` text)",
        ["customer_id", "customer_name", "email", "status", "region", "sbg_id"], set(),
    ),
    "b2bproduct": (
        "(`product_id` text, `product_code` text, `product_name` text, "
        "`category` text, `status` text, `sbg_id` text)",
        ["product_id", "product_code", "product_name", "category", "status", "sbg_id"], set(),
    ),
    "b2bprice": (
        "(`price_id` text, `product_id` text, `price_amount` double DEFAULT NULL, "
        "`discount_pct` double DEFAULT NULL, `status` text, `eff_date` text, "
        "`end_date` text)",
        ["price_id", "product_id", "price_amount", "discount_pct", "status",
         "eff_date", "end_date"],
        {"price_amount", "discount_pct"},      # cast to float, blank -> NULL
    ),
}


def env(name, default=None):
    v = os.getenv(name, default)
    if not v:
        sys.exit(f"Missing {name} in .env")
    return v


def server_engine():
    """Connects to the SERVER, not a database -- needed to CREATE DATABASE."""
    pwd = urllib.parse.quote_plus(env("DB_PASSWORD"))
    return create_engine(
        f"mysql+pymysql://{env('DB_USER')}:{pwd}@{env('DB_HOST')}:{env('DB_PORT', '3306')}/",
        future=True,
    )


def step(n, msg):
    print(f"\n[{n}] {msg}")


# ---------------------------------------------------------------------------
def create_databases(force):
    names = [env("SOURCE_DB"), env("CONFIG_DB"), env("TARGET_DB")]
    eng = server_engine()
    with eng.connect() as c:
        existing = {r[0] for r in c.execute(text("SHOW DATABASES"))}
        clash = [n for n in names if n in existing]
        if clash and not force:
            sys.exit(
                f"  These databases already exist: {', '.join(clash)}\n"
                f"  Re-run with --force to DROP and rebuild them, or drop them yourself.\n"
                f"  --force DELETES every rule, run and result in them."
            )
        for n in names:
            if force and n in existing:
                c.execute(text(f"DROP DATABASE `{n}`"))
                print(f"  dropped   {n}")
            c.execute(text(f"CREATE DATABASE IF NOT EXISTS `{n}` "
                           f"DEFAULT CHARACTER SET utf8mb4"))
            print(f"  created   {n}")
        c.commit()
    eng.dispose()


def load_source_data():
    pwd = urllib.parse.quote_plus(env("DB_PASSWORD"))
    eng = create_engine(
        f"mysql+pymysql://{env('DB_USER')}:{pwd}@{env('DB_HOST')}:"
        f"{env('DB_PORT', '3306')}/{env('SOURCE_DB')}", future=True)

    with eng.begin() as c:
        for table, (ddl, cols, numeric) in SOURCE_TABLES.items():
            c.execute(text(f"DROP TABLE IF EXISTS {table}"))
            c.execute(text(f"CREATE TABLE {table} {ddl} "
                           f"ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"))

            path = os.path.join(DUMP, f"{table}.csv")
            if not os.path.exists(path):
                sys.exit(f"  Missing {path}")
            with open(path, newline="", encoding="utf-8-sig") as f:
                # DictReader consumes the header, so it can never be loaded as
                # a data row -- which is what happened on the original import
                # and left "sbg_id" sitting in b2bsbg as if it were an SBG.
                rows = []
                for r in csv.DictReader(f):
                    row = {}
                    for col in cols:
                        v = (r.get(col) or "").strip()
                        if col in numeric:
                            row[col] = float(v) if v else None
                        else:
                            row[col] = v
                    rows.append(row)
            ph = ", ".join(f":{c_}" for c_ in cols)
            c.execute(text(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({ph})"), rows)
            print(f"  {table:<14} {len(rows):>3} base rows")
    eng.dispose()


def seed_extra_rows():
    """The rows that give every rule type both passes and failures."""
    import seed_source_data
    seed_source_data.main()


def create_val_schema():
    import create_tables
    create_tables.main()


def seed_rules():
    """
    Inserts the 23 rules directly, already APPROVED.

    seed_rules_b2b.py goes through the API, which needs the backend running.
    Bootstrap must work before anything is started, so the same RULES list is
    written straight to val_rules here -- with every definition compiled first,
    so a broken rule fails loudly instead of landing in the table.
    """
    from app.database import ConfigSession
    from app.models import ENTITIES, ValRule
    from app.rule_compiler import (
        CompileContext, RuleCompileError, compile_rule, dimension_for,
        execution_type_for,
    )
    from seed_rules_b2b import RULES

    now = datetime.now(timezone.utc)
    db = ConfigSession()
    by_type = {}
    try:
        for name, entity, field, rtype, sev, defn in RULES:
            meta = ENTITIES[entity]
            definition_json = json.dumps(defn)
            ctx = CompileContext(table="stg_x", columns=meta["columns"],
                                 lookup_table="stg_lookup", lookup_run_id=0)
            try:
                compile_rule(rtype, field, definition_json, ctx)
            except RuleCompileError as exc:
                sys.exit(f"  Rule '{name}' does not compile: {exc}")

            db.add(ValRule(
                rule_name=name,
                source_system=meta["source_system"],
                rule_type=rtype,
                entity_name=entity,
                field_name=field,
                primary_key_field=meta["primary_key_field"],
                execution_type=execution_type_for(rtype),
                dimension=dimension_for(rtype),      # from RULE_TYPE_META only
                rule_definition=definition_json,
                severity=sev,
                status="APPROVED",
                active=True,
                created_by="prabhat",
                created_date=now,
                approved_by="prabhat",
                approved_date=now,
            ))
            by_type[rtype] = by_type.get(rtype, 0) + 1
        db.commit()
    finally:
        db.close()
    print(f"  {sum(by_type.values())} rules, APPROVED and active")
    for t, n in sorted(by_type.items()):
        print(f"    {t:<24} {n}")


def summary():
    from app.database import ConfigSession, ResultsSession
    pwd = urllib.parse.quote_plus(env("DB_PASSWORD"))
    src = create_engine(
        f"mysql+pymysql://{env('DB_USER')}:{pwd}@{env('DB_HOST')}:"
        f"{env('DB_PORT', '3306')}/{env('SOURCE_DB')}", future=True)
    total = 0
    print(f"\n  {env('SOURCE_DB')}")
    with src.connect() as c:
        for t in SOURCE_TABLES:
            n = c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
            total += n
            print(f"    {t:<16} {n:>4} rows")
    print(f"    {'TOTAL':<16} {total:>4} rows")
    src.dispose()

    cdb = ConfigSession()
    print(f"\n  {env('CONFIG_DB')}")
    for t in ("val_rules", "val_rule_types", "val_severities", "val_statuses"):
        print(f"    {t:<16} {cdb.execute(text(f'SELECT COUNT(*) FROM {t}')).scalar():>4} rows")
    cdb.close()

    rdb = ResultsSession()
    print(f"\n  {env('TARGET_DB')}")
    for t in ("val_batches", "val_runs", "val_metrics", "val_violations"):
        print(f"    {t:<16} {rdb.execute(text(f'SELECT COUNT(*) FROM {t}')).scalar():>4} rows  (empty until you run)")
    rdb.close()


def main():
    force = "--force" in sys.argv
    print("=" * 62)
    print(" BOOTSTRAP -- building source_db, config_db and target_db")
    print("=" * 62)
    print(f"  server : {env('DB_USER')}@{env('DB_HOST')}:{env('DB_PORT', '3306')}")
    if force:
        print("  --force: the three databases will be DROPPED first")

    step(1, "Creating databases")
    create_databases(force)

    step(2, "Loading base source rows from data_dump/*.csv")
    load_source_data()

    step(3, "Adding rows that make every rule type fail somewhere")
    seed_extra_rows()

    step(4, "Creating val_* schema and lookup tables")
    create_val_schema()

    step(5, "Loading validation rules")
    seed_rules()

    print("\n" + "=" * 62)
    print(" DONE")
    print("=" * 62)
    summary()
    print("""
  Next:
    python -m uvicorn app.main:app --reload --port 8000
    cd ../frontend && npm run dev

  Then Runs -> source MySQL -> select all four objects -> start.

  Optional demo runs for the other three sources:
    python seed_dummy.py --reset
""")


if __name__ == "__main__":
    main()
