"""
migrate_db.py
-------------
Brings an existing database up to what the models now declare.

create_tables.py uses SQLAlchemy's create_all(), which only ever creates
MISSING TABLES -- it never alters an existing one. So a machine that was set up
before a change keeps the old table and fails at query time with:

    (1054, "Unknown column 'val_runs.total_records' in 'field_list'")

Three kinds of drift are reconciled, across all THREE databases:

  1. missing columns   ALTER TABLE ... ADD COLUMN
  2. changed types     ALTER TABLE ... MODIFY COLUMN
                       Specifically TEXT -> VARCHAR(n) on the staging tables:
                       a TEXT column cannot carry an ordinary index (and is a
                       CLOB on Oracle, where it cannot be joined at all).
  3. missing indexes   CREATE INDEX / DROP INDEX
                       Staging join keys, and the trailing violation_id that
                       makes keyset paging an index range scan.

SAFETY: no column and no row is ever dropped. Indexes ARE dropped when they
are no longer declared -- an index holds no data, and a redundant one costs
write throughput on every staged row. Everything is idempotent, so running it
twice is a no-op.

Usage:
    python migrate_db.py            # show the plan, change nothing
    python migrate_db.py --apply    # execute it
"""

import os
import sys

# this script lives in extra/, the app package lives in backend/
BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
sys.path.insert(0, BACKEND)

import sys

from sqlalchemy import bindparam, inspect, text

from app.database import config_engine, results_engine, source_engine
from app.models import ConfigBase, ResultsBase, StagingBase
from app.rule_compiler import RETIRED_DIMENSIONS, RULE_TYPE_META

# TEXT-family types that must become VARCHAR so they can be indexed.
_TEXTY = ("TEXT", "TINYTEXT", "MEDIUMTEXT", "LONGTEXT", "CLOB", "NCLOB")


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


def plan_types(engine, base, label):
    """
    Columns declared VARCHAR(n) that are still TEXT (or the wrong length).

    Deliberately narrow. A general "does the declared type match" diff throws
    false positives on every backend (INT vs INTEGER, DATETIME vs TIMESTAMP)
    and would emit pointless ALTERs. This looks only for the change that
    actually matters: a column that must become indexable.
    """
    insp = inspect(engine)
    existing = set(insp.get_table_names())
    todo = []

    for table in base.metadata.sorted_tables:
        if table.name not in existing:
            continue
        actual = {c["name"]: c for c in insp.get_columns(table.name)}
        for col in table.columns:
            a = actual.get(col.name)
            if a is None:
                continue                       # plan() already reports it
            want = col.type.compile(engine.dialect).upper()
            have = str(a["type"]).upper()
            if not want.startswith("VARCHAR"):
                continue
            if have.split("(")[0] in _TEXTY or (have.startswith("VARCHAR") and have != want):
                todo.append((table.name, col.name, have, want, col))

    print(f"\n=== {label} column types -> {engine.url.database} ===")
    if not todo:
        print("  all declared types match")
    for t, c, have, want, _ in todo:
        print(f"  ~ {t}.{c:<20} {have} -> {want}")
    return todo


def apply_types(engine, todo):
    """
    MODIFY COLUMN, but never silently truncate. A value longer than the target
    length would be cut (or rejected outright in strict mode), so any such
    column is reported and SKIPPED rather than quietly losing data.
    """
    for table, col, have, want, column in todo:
        limit = getattr(column.type, "length", None)
        with engine.connect() as conn:
            rows = conn.execute(text(f"SELECT COUNT(*) FROM `{table}`")).scalar() or 0
            too_long = 0
            if rows and limit:
                too_long = conn.execute(
                    text(f"SELECT COUNT(*) FROM `{table}` "
                         f"WHERE CHAR_LENGTH(`{col}`) > :n"), {"n": limit}).scalar() or 0
        if too_long:
            print(f"  SKIPPED {table}.{col} -- {too_long} value(s) longer than {limit} chars. "
                  f"Widen it via ENTITIES['...']['column_lengths'] and re-run.")
            continue
        null_sql = "NULL" if column.nullable else "NOT NULL"
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE `{table}` MODIFY `{col}` {want} {null_sql}"))
        print(f"  changed {table}.{col} -> {want}")


def plan_indexes(engine, base, label):
    """Declared indexes the database is missing, and stale ones it still has."""
    insp = inspect(engine)
    existing = set(insp.get_table_names())
    create, drop = [], []

    for table in base.metadata.sorted_tables:
        if table.name not in existing:
            continue
        have = {ix["name"]: list(ix["column_names"])
                for ix in insp.get_indexes(table.name) if ix.get("name")}
        want = {ix.name: [c.name for c in ix.columns] for ix in table.indexes}

        for name, cols in want.items():
            if name not in have:
                create.append((table.name, name, cols))
            elif have[name] != cols:
                # column list changed -- e.g. violation_id appended for keyset
                drop.append((table.name, name, f"columns changed {have[name]} -> {cols}"))
                create.append((table.name, name, cols))
        for name, cols in have.items():
            # only our own ix_* indexes; never touch PRIMARY or a unique
            # constraint backing an application invariant
            if name not in want and name.startswith("ix_"):
                drop.append((table.name, name, "no longer declared -- redundant"))

    print(f"\n=== {label} indexes -> {engine.url.database} ===")
    if not create and not drop:
        print("  all declared indexes present")
    for t, n, why in drop:
        print(f"  - {t}.{n:<32} {why}")
    for t, n, cols in create:
        print(f"  + {t}.{n:<32} ({', '.join(cols)})")
    return create, drop


def apply_indexes(engine, create, drop):
    """
    Order matters, and not just for tidiness.

    val_violations.run_id carries a foreign key to val_runs, and MySQL refuses
    to drop the LAST index that a foreign key can use:

        (1553, "Cannot drop index 'ix_violation_run_severity':
                needed in a foreign key constraint")

    Dropping all three run_id-leading indexes before creating any replacement
    hits exactly that. So brand-new indexes go in FIRST -- ix_violation_run
    (run_id, violation_id) satisfies the constraint -- and only then is
    anything removed. Indexes being rebuilt under the same name still have to
    be dropped immediately before their create, so they are handled as pairs.
    """
    def sql_cols(cols):
        return ", ".join(f"`{c}`" for c in cols)

    rebuilt = {(t, n) for t, n, _ in drop} & {(t, n) for t, n, _ in create}

    # 1. brand-new names -- nothing to drop, and these keep any FK satisfied
    for table, name, cols in create:
        if (table, name) in rebuilt:
            continue
        with engine.begin() as conn:
            conn.execute(text(f"CREATE INDEX `{name}` ON `{table}` ({sql_cols(cols)})"))
        print(f"  created {table}.{name}")

    # 2. same name, different columns -- drop and recreate as one step
    for table, name, cols in create:
        if (table, name) not in rebuilt:
            continue
        with engine.begin() as conn:
            conn.execute(text(f"DROP INDEX `{name}` ON `{table}`"))
            conn.execute(text(f"CREATE INDEX `{name}` ON `{table}` ({sql_cols(cols)})"))
        print(f"  rebuilt {table}.{name}")

    # 3. genuinely stale, nothing replacing them
    for table, name, _ in drop:
        if (table, name) in rebuilt:
            continue
        with engine.begin() as conn:
            conn.execute(text(f"DROP INDEX `{name}` ON `{table}`"))
        print(f"  dropped {table}.{name}")


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


def backfill_source_system(do_it):
    """
    Re-stamp val_rules.source_system and val_runs.source_system from ENTITIES.

    source_system is denormalized onto both tables at write time, so changing
    the catalog never reaches rows that already exist. Runs recorded before
    "MySQL" was retired still carry it, and the dashboard's source filter then
    hides them under a system that no longer appears in the picker.

    Only rows that disagree with the catalog are touched.
    """
    from sqlalchemy import inspect as sa_inspect
    from app.models import ENTITIES

    print("\n=== source_system backfill ===")
    want = {e: m["source_system"] for e, m in ENTITIES.items()}
    touched = 0

    for engine, table in ((config_engine, "val_rules"), (results_engine, "val_runs")):
        if table not in sa_inspect(engine).get_table_names():
            continue
        with engine.begin() as c:
            for entity, src in want.items():
                n = c.execute(
                    text(f"SELECT COUNT(*) FROM {table} WHERE entity_name = :e "
                         "AND (source_system IS NULL OR source_system <> :s)"),
                    {"e": entity, "s": src}).scalar()
                if not n:
                    continue
                touched += n
                print(f"  {table:<12} {entity:<14} {n:>4} -> {src}")
                if do_it:
                    c.execute(
                        text(f"UPDATE {table} SET source_system = :s "
                             "WHERE entity_name = :e "
                             "AND (source_system IS NULL OR source_system <> :s)"),
                        {"s": src, "e": entity})

    # A batch keeps a source only when every run in it agrees.
    if "val_batches" in sa_inspect(results_engine).get_table_names():
        with results_engine.begin() as c:
            rows = c.execute(text(
                "SELECT b.batch_id, COUNT(DISTINCT r.source_system) AS n, "
                "       MIN(r.source_system) AS only_one "
                "FROM val_batches b JOIN val_runs r ON r.batch_id = b.batch_id "
                "GROUP BY b.batch_id")).fetchall()
            for batch_id, n, only_one in rows:
                target = only_one if n == 1 else None
                cur = c.execute(text("SELECT source_system FROM val_batches WHERE batch_id = :b"),
                                {"b": batch_id}).scalar()
                if cur == target:
                    continue
                touched += 1
                print(f"  val_batches  #{batch_id:<13} {cur} -> {target or 'NULL (mixed)'}")
                if do_it:
                    c.execute(text("UPDATE val_batches SET source_system = :s WHERE batch_id = :b"),
                              {"s": target, "b": batch_id})

    if not touched:
        print("  every row already matches the catalog")


def main():
    do_it = "--apply" in sys.argv
    targets = [
        (source_engine, StagingBase, "SOURCE_DB"),
        (config_engine, ConfigBase, "CONFIG_DB"),
        (results_engine, ResultsBase, "RESULTS_DB"),
    ]

    cols = [(e, plan(e, b, l)) for e, b, l in targets]
    types = [(e, plan_types(e, b, l)) for e, b, l in targets]
    idx = [(e, *plan_indexes(e, b, l)) for e, b, l in targets]

    n_cols = sum(len(t) for _, t in cols)
    n_types = sum(len(t) for _, t in types)
    n_idx = sum(len(c) + len(d) for _, c, d in idx)
    total = n_cols + n_types + n_idx

    if do_it:
        # order matters: add columns, then fix their types, then index them
        if n_cols:
            print()
            for engine, todo in cols:
                if todo:
                    apply(engine, todo)
        if n_types:
            print()
            for engine, todo in types:
                if todo:
                    apply_types(engine, todo)
        if n_idx:
            print()
            for engine, create, drop in idx:
                if create or drop:
                    apply_indexes(engine, create, drop)
        print(f"\n{n_cols} column(s) added, {n_types} type(s) changed, "
              f"{n_idx} index change(s).")
    elif total:
        print(f"\n{n_cols} column(s) missing, {n_types} type(s) wrong, "
              f"{n_idx} index change(s) pending.")

    # runs after the columns exist, so a fresh DB migrates in one pass
    if not n_cols or do_it:
        backfill_dimensions(do_it)
        backfill_source_system(do_it)

    if do_it:
        print("\nDone. Restart the backend.")
    else:
        print("\nDry run -- nothing changed. Re-run with --apply:")
        print("    python migrate_db.py --apply")


if __name__ == "__main__":
    main()
