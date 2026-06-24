"""
engine.py
---------
The orchestrator. Does the "Detect" + "Preview fixes" stages only.
Does NOT write to target_db - that's a separate, later, explicit step
(per your decision to review suggested fixes before they go live).

What it does, in order:
  1. Open a validation_run record (status='running')
  2. Load active rules from config_db.validation_rules
  3. For each rule, load the relevant table(s) - from source_db by default,
     or from a file if a source_override is supplied for that table
  4. Dispatch to the correct rule_check function
  5. Write every violation found into config_db.validation_results
     (fix_status = 'open' for everything at this stage - nothing applied yet)
  6. Close out the run record (status='completed')
"""

import pandas as pd
from sqlalchemy import text

from db import get_engine
from source_loader import load_source
import rule_checks as rc

PK_MAP = {
    "b2bcustomer": "customer_id",
    "b2bsbg": "sbg_id",
    "b2bproduct": "product_id",
    "b2bprice": "price_id",
}


def load_active_rules():
    config_engine = get_engine("config")
    query = "SELECT * FROM validation_rules WHERE is_active = 1"
    try:
        with config_engine.connect() as conn:
            rules_df = pd.read_sql(query, conn)
    except Exception as e:
        raise RuntimeError(
            "Could not load rules from config_db.validation_rules. "
            "Make sure config_db has been set up and seeded with rules."
        ) from e
    return rules_df


def load_table(table_name, source_overrides=None):
    """
    Loads a table's data either from source_db (default) or from a file,
    if an override was supplied for this specific table_name.

    source_overrides example:
        {"b2bcustomer": {"kind": "file", "file_path": "data_dump/b2bcustomer.csv"}}

    Any table_name NOT in source_overrides falls back to the normal
    database load - so existing behavior is unchanged unless you opt in.
    """
    if source_overrides and table_name in source_overrides:
        spec = source_overrides[table_name]
        return load_source(**spec)
    return load_source(kind="database", table_name=table_name)


def start_run(triggered_by="python_engine"):
    config_engine = get_engine("config")
    with config_engine.begin() as conn:
        result = conn.execute(
            text("INSERT INTO validation_runs (triggered_by, status) VALUES (:by, 'running')"),
            {"by": triggered_by},
        )
        run_id = result.lastrowid
    return run_id


def finish_run(run_id, status="completed"):
    config_engine = get_engine("config")
    with config_engine.begin() as conn:
        conn.execute(
            text("UPDATE validation_runs SET status = :status WHERE run_id = :run_id"),
            {"status": status, "run_id": run_id},
        )


def write_results(run_id, rule, table_name, violations):
    """violations: list of dicts from a rule_check function."""
    if not violations:
        return
    config_engine = get_engine("config")
    rows = []
    for v in violations:
        rows.append({
            "run_id": run_id,
            "rule_id": int(rule["rule_id"]),
            "table_name": table_name,
            "record_id": str(v["record_id"]),
            "column_name": v["column_name"],
            "current_value": v["current_value"],
            "suggested_fix": v["suggested_fix"],
            "severity": rule["severity"],
            "fix_status": "open",
        })
    results_df = pd.DataFrame(rows)
    with config_engine.begin() as conn:
        results_df.to_sql("validation_results", conn, if_exists="append", index=False)


def run_validation(triggered_by="python_engine", verbose=True, source_overrides=None):
    """
    Main entry point. Returns (run_id, summary_df).

    source_overrides : optional dict, e.g.
        {"b2bcustomer": {"kind": "file", "file_path": "data_dump/b2bcustomer.csv"}}
        Lets you point specific tables at a file instead of source_db,
        while everything else still loads from the database as normal.
    """
    run_id = start_run(triggered_by)
    if verbose:
        print(f"Started run_id={run_id}")

    rules_df = load_active_rules()

    # Pre-load tables we need (cache so we don't re-query per rule)
    tables_needed = set(rules_df["table_name"].unique())
    table_cache = {t: load_table(t, source_overrides=source_overrides) for t in tables_needed}

    summary_rows = []

    try:
        for _, rule in rules_df.iterrows():
            rule_type = rule["rule_type"]
            table_name = rule["table_name"]
            df = table_cache[table_name]
            pk_col = PK_MAP.get(table_name)
            if not pk_col:
                raise ValueError(
                    f"No PK mapping found for table '{table_name}'. "
                    "Add it to PK_MAP in engine.py."
                )

            if rule_type == "ref_integrity":
                # needs the referenced table too
                import json
                params = json.loads(rule["parameters"]) if isinstance(rule["parameters"], str) else rule["parameters"]
                ref_table = params["ref_table"]
                ref_df = table_cache.get(ref_table)
                if ref_df is None:
                    ref_df = load_table(ref_table, source_overrides=source_overrides)
                    table_cache[ref_table] = ref_df
                violations = rc.check_ref_integrity(df, rule, pk_col, ref_df)
            elif rule_type == "custom_sql":
                # some custom_sql checks (e.g. active_on_discontinued) need a reference
                # table too - if parameters includes ref_table, load it and attach it
                # to the rule dict under "_ref_df" so check_custom_sql can use it.
                import json
                params = json.loads(rule["parameters"]) if isinstance(rule["parameters"], str) else rule["parameters"]
                rule_with_ref = dict(rule)
                if params.get("ref_table"):
                    ref_table = params["ref_table"]
                    ref_df = table_cache.get(ref_table)
                    if ref_df is None:
                        ref_df = load_table(ref_table, source_overrides=source_overrides)
                        table_cache[ref_table] = ref_df
                    rule_with_ref["_ref_df"] = ref_df
                violations = rc.check_custom_sql(df, rule_with_ref, pk_col)
            else:
                checker = rc.RULE_DISPATCH.get(rule_type)
                if checker is None:
                    if verbose:
                        print(f"  [skip] rule_id={rule['rule_id']} unknown rule_type '{rule_type}'")
                    continue
                violations = checker(df, rule, pk_col)

            write_results(run_id, rule, table_name, violations)

            summary_rows.append({
                "rule_id": rule["rule_id"],
                "rule_name": rule["rule_name"],
                "table_name": table_name,
                "rule_type": rule_type,
                "fix_action": rule["fix_action"],
                "violations_found": len(violations),
            })
            if verbose:
                print(f"  rule_id={rule['rule_id']:<3} {rule['rule_name']:<35} -> {len(violations)} violation(s)")

        finish_run(run_id, status="completed")

    except Exception as e:
        finish_run(run_id, status="failed")
        raise e

    summary_df = pd.DataFrame(summary_rows)
    if verbose:
        print(f"\nRun {run_id} completed. Total violations: {summary_df['violations_found'].sum()}")
    return run_id, summary_df


if __name__ == "__main__":
    run_id, summary = run_validation(triggered_by="manual_script_run")
    print("\n--- Summary ---")
    print(summary.to_string(index=False))