"""
apply_fixes.py
---------------
Phase 3: Auto-Fix Layer (the "Apply" step engine.py deliberately stopped
short of). Run this AFTER engine.py and AFTER you've reviewed
validation_results yourself.

What it does, per table:
  1. Load the current table from source_db
  2. Pull all OPEN validation_results rows for this table, joined to their
     rule (to know fix_action / fix_logic / rule_type)
  3. Apply each AUTO-fixable, non-destructive violation directly to the
     in-memory DataFrame (trim, case normalization, range capping)
  4. SKIP destructive fixes (suggested_fix == 'DELETE') entirely for now -
     these are left untouched and stay 'open' in validation_results.
     (Deliberately deferred - not part of this pass.)
  5. Add a has_open_manual_issues column: True if the record still has
     ANY open result tied to a manual-fix_action rule (this includes the
     skipped DELETE rows, since those are still genuinely unresolved)
  6. Write the full table (all rows, fixed + unfixed + flagged) to target_db
  7. Mark only the rows we actually applied as fix_status='auto_fixed'
     in validation_results - everything else (manual, skipped deletes)
     stays 'open'

Usage:
    python apply_fixes.py
"""

import json
import pandas as pd
from sqlalchemy import text

from db import get_engine
import rule_checks as rc

PK_MAP = {
    "b2bcustomer": "customer_id",
    "b2bsbg": "sbg_id",
    "b2bproduct": "product_id",
    "b2bprice": "price_id",
}

# Order matters for target_db writes because of FK constraints:
# b2bcustomer.sbg_id -> b2bsbg, b2bproduct.sbg_id -> b2bsbg, b2bprice.product_id -> b2bproduct (no FK, but logically still a child)
# Parent tables must be written (and NOT deleted-then-recreated out of order) before children that reference them.
TABLE_WRITE_ORDER = ["b2bsbg", "b2bcustomer", "b2bproduct", "b2bprice"]


def load_source_table(table_name):
    source_engine = get_engine("source")
    with source_engine.connect() as conn:
        return pd.read_sql(f"SELECT * FROM {table_name}", conn)


def load_open_results_with_rules(table_name):
    """
    Returns validation_results rows for this table that are still 'open',
    joined to their rule's fix_action / fix_logic / rule_type, for the
    MOST RECENT run only (so we don't reprocess stale runs).
    """
    config_engine = get_engine("config")
    query = """
        SELECT r.result_id, r.run_id, r.rule_id, r.table_name, r.record_id,
               r.column_name, r.current_value, r.suggested_fix, r.severity,
               r.fix_status,
               vr.rule_type, vr.fix_action, vr.fix_logic
        FROM validation_results r
        JOIN validation_rules vr ON r.rule_id = vr.rule_id
        WHERE r.table_name = :table_name
          AND r.fix_status = 'open'
          AND r.run_id = (SELECT MAX(run_id) FROM validation_runs WHERE status = 'completed')
    """
    with config_engine.connect() as conn:
        return pd.read_sql(text(query), conn, params={"table_name": table_name})


def apply_fixes_to_table(table_name):
    """
    Returns (fixed_df, applied_result_ids, open_manual_record_ids)
    """
    df = load_source_table(table_name)
    pk_col = PK_MAP[table_name]
    open_results = load_open_results_with_rules(table_name)

    applied_result_ids = []
    open_manual_record_ids = set()

    # Index by record_id (as string, to match how it's stored) for fast lookup
    df = df.set_index(df[pk_col].astype(str), drop=False)

    for _, res in open_results.iterrows():
        record_id = str(res["record_id"])
        fix_action = res["fix_action"]
        col = res["column_name"]
        suggested_fix = res["suggested_fix"]

        if fix_action != "auto":
            # manual issue - leave data untouched, just remember this record is flagged
            open_manual_record_ids.add(record_id)
            continue

        # Auto-fix, but skip destructive deletes for now (per current scope decision)
        if suggested_fix == "DELETE":
            open_manual_record_ids.add(record_id)  # still genuinely unresolved
            continue

        if record_id not in df.index:
            continue  # record no longer exists in source (shouldn't normally happen)

        if col is None or pd.isna(col):
            # cross-table/cross-field rule with no single column to patch (e.g. custom_sql) -
            # nothing to directly apply to a column, treat as still needing manual attention
            open_manual_record_ids.add(record_id)
            continue

        if suggested_fix is None or (isinstance(suggested_fix, float) and pd.isna(suggested_fix)):
            # auto rule but no concrete suggested value was computed - skip safely
            open_manual_record_ids.add(record_id)
            continue

        # Apply the fix directly to the DataFrame.
        # suggested_fix always comes back from validation_results as a string
        # (it's stored in a VARCHAR column for audit purposes). If the target
        # column in the source table is numeric, we must cast it back to a
        # number first - pandas raises a hard TypeError on newer versions if
        # you assign a string into a float64/int64 column.
        target_dtype = df[col].dtype
        value_to_write = suggested_fix
        if pd.api.types.is_numeric_dtype(target_dtype):
            try:
                value_to_write = float(suggested_fix)
            except (TypeError, ValueError):
                # couldn't convert - skip this one safely rather than crash the whole run
                open_manual_record_ids.add(record_id)
                continue

        df.loc[record_id, col] = value_to_write
        applied_result_ids.append(int(res["result_id"]))

    df = df.reset_index(drop=True)
    df["has_open_manual_issues"] = df[pk_col].astype(str).isin(open_manual_record_ids)

    return df, applied_result_ids, open_manual_record_ids


def delete_target_rows(table_name):
    target_engine = get_engine("target")
    with target_engine.begin() as conn:
        conn.execute(text(f"DELETE FROM {table_name}"))


def insert_target_rows(table_name, df):
    target_engine = get_engine("target")
    with target_engine.begin() as conn:
        df.to_sql(table_name, conn, if_exists="append", index=False)


def mark_results_applied(applied_result_ids):
    if not applied_result_ids:
        return
    config_engine = get_engine("config")
    ids_csv = ",".join(str(int(i)) for i in applied_result_ids)  # ids are our own DB ints - safe to inline
    with config_engine.begin() as conn:
        conn.execute(text(
            "UPDATE validation_results "
            "SET fix_status = 'auto_fixed', fixed_by = 'apply_fixes.py', "
            "    fixed_date = CURRENT_TIMESTAMP "
            f"WHERE result_id IN ({ids_csv})"
        ))


def ensure_target_has_flag_column(table_name):
    """Adds has_open_manual_issues column to target_db table if it doesn't exist yet."""
    target_engine = get_engine("target")
    with target_engine.connect() as conn:
        existing_cols = pd.read_sql(text(f"SHOW COLUMNS FROM {table_name}"), conn)["Field"].tolist()
    if "has_open_manual_issues" not in existing_cols:
        with target_engine.begin() as conn:
            conn.execute(text(
                f"ALTER TABLE {table_name} ADD COLUMN has_open_manual_issues TINYINT(1) DEFAULT 0"
            ))


def run_apply(verbose=True):
    tables = TABLE_WRITE_ORDER
    summary = []

    # PASS 1: delete existing target_db rows, in CHILD-to-PARENT order
    # (so we never try to delete a parent row while a child still references it)
    if verbose:
        print("Clearing previous target_db snapshot...")
    for table_name in reversed(tables):
        ensure_target_has_flag_column(table_name)
        delete_target_rows(table_name)

    # PASS 2: compute fixes and insert, in PARENT-to-CHILD order
    # (so FK columns always point at rows that already exist in target_db)
    for table_name in tables:
        if verbose:
            print(f"\n--- {table_name} ---")

        fixed_df, applied_ids, open_manual_ids = apply_fixes_to_table(table_name)
        insert_target_rows(table_name, fixed_df)
        mark_results_applied(applied_ids)

        n_flagged = fixed_df["has_open_manual_issues"].sum()
        if verbose:
            print(f"  rows written to target_db: {len(fixed_df)}")
            print(f"  auto-fixes applied: {len(applied_ids)}")
            print(f"  rows still flagged (open manual issues): {n_flagged}")

        summary.append({
            "table_name": table_name,
            "rows_written": len(fixed_df),
            "auto_fixes_applied": len(applied_ids),
            "rows_with_open_manual_issues": int(n_flagged),
        })

    summary_df = pd.DataFrame(summary)
    if verbose:
        print("\n=== Apply Summary ===")
        print(summary_df.to_string(index=False))
        print("\nNOTE: duplicate-email DELETE suggestions were intentionally skipped this pass.")
        print("Those records remain in target_db, still flagged as having an open manual issue.")

    return summary_df


if __name__ == "__main__":
    run_apply()