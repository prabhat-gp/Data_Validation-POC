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

from sqlalchemy import inspect, text

from app.database import config_engine, results_engine
from app.models import ConfigBase, ResultsBase


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


def main():
    do_it = "--apply" in sys.argv
    work = [
        (config_engine, plan(config_engine, ConfigBase, "CONFIG_DB")),
        (results_engine, plan(results_engine, ResultsBase, "TARGET_DB")),
    ]
    total = sum(len(t) for _, t in work)

    if total == 0:
        print("\nNothing to migrate.")
        return
    if not do_it:
        print(f"\n{total} column(s) missing. Re-run with --apply to add them:")
        print("    python migrate_db.py --apply")
        return

    print()
    for engine, todo in work:
        if todo:
            apply(engine, todo)
    print(f"\n{total} column(s) added. Restart the backend.")


if __name__ == "__main__":
    main()
