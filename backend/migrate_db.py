"""
migrate_db.py
-------------
Adds columns that exist in the models but not yet in the database.

create_tables.py uses SQLAlchemy's create_all(), which only ever creates
MISSING TABLES -- it never alters an existing one. So a machine that was set up
before a column was added keeps the old table and fails at query time with:

    (1054, "Unknown column 'val_runs.total_records' in 'field_list'")

This compares every declared column against information_schema and issues
ALTER TABLE ... ADD COLUMN for the gaps. Existing rows and columns are never
touched, and nothing is ever dropped -- so it is safe to run on a database you
care about, and safe to run twice.

Usage:
    python migrate_db.py            # show what is missing, change nothing
    python migrate_db.py --apply    # actually add the columns
"""

import sys

from sqlalchemy import bindparam, inspect, text

from app.database import config_engine, results_engine
from app.models import ConfigBase, ResultsBase
from app.rule_compiler import RETIRED_DIMENSIONS, RULE_TYPE_META


def column_ddl(col, dialect):
    """Render one column the way CREATE TABLE would have."""
    type_sql = col.type.compile(dialect)
    parts = [f"`{col.name}`", type_sql]

    # A NOT NULL column cannot be added to a table that already has rows
    # unless it carries a default. Where the model gives no default we add it
    # nullable instead of failing -- flagged in the output so it is visible.
    default = col.default.arg if col.default is not None and not callable(col.default.arg) else None
    if not col.nullable and default is None:
        parts.append("NULL")
        note = "  (model says NOT NULL; added nullable to protect existing rows)"
    else:
        parts.append("NULL" if col.nullable else "NOT NULL")
        note = ""
        if default is not None:
            lit = f"'{default}'" if isinstance(default, str) else str(default)
            parts.append(f"DEFAULT {lit}")
    return " ".join(parts), note


def plan(engine, base, label):
    """Return [(table, column_name, ddl, note)] for everything missing."""
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    todo, missing_tables = [], []

    for table in base.metadata.sorted_tables:
        if table.name not in existing_tables:
            missing_tables.append(table.name)
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name not in have:
                ddl, note = column_ddl(col, engine.dialect)
                todo.append((table.name, col.name, ddl, note))

    print(f"\n=== {label} -> {engine.url.database} ===")
    if missing_tables:
        print(f"  tables not created yet (run create_tables.py): {', '.join(missing_tables)}")
    if not todo:
        print("  all declared columns present")
    for t, c, ddl, note in todo:
        print(f"  + {t}.{c:<20} {ddl}{note}")
    return todo


def apply(engine, todo):
    with engine.begin() as conn:
        for table, col, ddl, _ in todo:
            conn.execute(text(f"ALTER TABLE `{table}` ADD COLUMN {ddl}"))
            print(f"  added {table}.{col}")


def backfill_dimensions(do_it):
    """
    Fill val_rules.dimension, then re-point historical val_metrics rows.

    Dimension used to be derived from rule_type at read time and the retired
    labels ("Ref Integrity", "Format", "Relationship") are baked into metrics
    rows from earlier runs. Without this, old runs keep dimensions the heatmap
    no longer has a column for -- the bug this rework fixed.

    Both steps are idempotent: they only touch rows that are still wrong.
    """
    from sqlalchemy import inspect as sa_inspect

    print("\n=== dimension backfill ===")
    # 1. Re-stamp every rule from RULE_TYPE_META. Dimension is not user input,
    #    so the mapping in rule_compiler.py is the only authority -- editing it
    #    and running this is how a reclassification is rolled out.
    if "val_rules" in sa_inspect(config_engine).get_table_names():
        with config_engine.begin() as c:
            for rtype, (dim, _) in RULE_TYPE_META.items():
                n = c.execute(
                    text("SELECT COUNT(*) FROM val_rules WHERE rule_type = :t "
                         "AND (dimension IS NULL OR dimension <> :d)"),
                    {"t": rtype, "d": dim},
                ).scalar()
                if not n:
                    continue
                print(f"  val_rules   {rtype:<22} {n:>3} -> {dim}")
                if do_it:
                    c.execute(text("UPDATE val_rules SET dimension = :d "
                                   "WHERE rule_type = :t "
                                   "AND (dimension IS NULL OR dimension <> :d)"),
                              {"d": dim, "t": rtype})

    # 2. results rows still carrying a retired label. BOTH tables denormalize
    #    dimension -- val_metrics drives the heatmap, val_violations drives the
    #    violation list and CSV export -- so both have to be re-pointed.
    have = set(sa_inspect(results_engine).get_table_names())
    with config_engine.connect() as cc:
        agg_ids = [r[0] for r in cc.execute(text(
            "SELECT rule_id FROM val_rules WHERE rule_type = 'AGGREGATION'"))]

    for table in ("val_metrics", "val_violations"):
        if table not in have:
            continue
        with results_engine.begin() as c:
            for old, new in RETIRED_DIMENSIONS.items():
                n = c.execute(text(f"SELECT COUNT(*) FROM {table} WHERE dimension = :o"),
                              {"o": old}).scalar()
                if not n:
                    continue
                print(f"  {table:<15} {old:<16} {n:>3} -> {new}")
                if do_it:
                    c.execute(text(f"UPDATE {table} SET dimension = :n WHERE dimension = :o"),
                              {"n": new, "o": old})
            # AGGREGATION moved Consistency -> Accuracy. Identify by the RULE,
            # not the label, since Consistency is still valid for the two
            # expression rule types.
            if agg_ids:
                n = c.execute(
                    text(f"SELECT COUNT(*) FROM {table} WHERE dimension = 'Consistency' "
                         "AND rule_id IN :ids").bindparams(bindparam("ids", expanding=True)),
                    {"ids": agg_ids}).scalar()
                if n:
                    print(f"  {table:<15} {'AGGREGATION rows':<16} {n:>3} -> Accuracy")
                    if do_it:
                        c.execute(
                            text(f"UPDATE {table} SET dimension = 'Accuracy' "
                                 "WHERE dimension = 'Consistency' AND rule_id IN :ids")
                            .bindparams(bindparam("ids", expanding=True)), {"ids": agg_ids})


def main():
    do_it = "--apply" in sys.argv
    work = [
        (config_engine, plan(config_engine, ConfigBase, "CONFIG_DB")),
        (results_engine, plan(results_engine, ResultsBase, "TARGET_DB")),
    ]
    total = sum(len(t) for _, t in work)

    if total and do_it:
        print()
        for engine, todo in work:
            if todo:
                apply(engine, todo)
        print(f"\n{total} column(s) added.")
    elif total:
        print(f"\n{total} column(s) missing -- run with --apply to add them.")

    # runs after the columns exist, so a fresh DB migrates in one pass
    if not total or do_it:
        backfill_dimensions(do_it)

    if do_it:
        print("\nDone. Restart the backend.")
    else:
        print("\nDry run -- nothing changed. Re-run with --apply:")
        print("    python migrate_db.py --apply")


if __name__ == "__main__":
    main()
