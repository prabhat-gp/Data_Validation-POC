"""
rule_checks.py
--------------
One function per rule_type. Each function takes:
    df         : pandas DataFrame of the full table being checked
    rule       : a dict-like row from validation_rules (rule_id, column_name, parameters, fix_logic, ...)
    pk_col     : name of the primary key column in df (used to identify WHICH row is bad)

Each function returns a list of violation dicts, shaped like:
    {
        "record_id": <pk value>,
        "column_name": <col checked>,
        "current_value": <the bad value, as string>,
        "suggested_fix": <proposed corrected value, or None if manual/no fix>,
    }

This keeps each rule_type isolated and easy to test/extend.
"""

import json
import re
import pandas as pd


def _params(rule):
    """parameters comes back from MySQL JSON column as a string or dict depending on driver - normalize it."""
    p = rule.get("parameters")
    if p is None:
        return {}
    if isinstance(p, dict):
        return p
    try:
        return json.loads(p)
    except (TypeError, json.JSONDecodeError):
        return {}


def check_null_check(df, rule, pk_col):
    col = rule["column_name"]
    violations = []
    bad_rows = df[df[col].isna() | (df[col].astype(str).str.strip() == "")]
    for _, row in bad_rows.iterrows():
        violations.append({
            "record_id": row[pk_col],
            "column_name": col,
            "current_value": None,
            "suggested_fix": None,  # null_check is manual in our POC - nothing to auto-fill
        })
    return violations


def check_format(df, rule, pk_col):
    """
    Handles 3 sub-cases based on fix_logic / parameters:
      - trim            -> whitespace trimming
      - upper / title_case -> case normalization
      - regex            -> format/pattern validation (e.g. email)
    """
    col = rule["column_name"]
    params = _params(rule)
    fix_logic_raw = rule.get("fix_logic")
    fix_logic = str(fix_logic_raw).lower() if pd.notna(fix_logic_raw) else ""
    violations = []

    # --- Case 1: regex validation (e.g. email format) ---
    if "regex" in params:
        pattern = re.compile(params["regex"])
        for _, row in df.iterrows():
            val = row[col]
            if pd.isna(val):
                continue  # null_check rule handles missing values separately
            if not pattern.match(str(val)):
                violations.append({
                    "record_id": row[pk_col],
                    "column_name": col,
                    "current_value": str(val),
                    "suggested_fix": None,  # invalid email is manual - can't guess the correct one
                })
        return violations

    # --- Case 2: trim whitespace (auto-fixable) ---
    if fix_logic == "trim" or params.get("trim"):
        for _, row in df.iterrows():
            val = row[col]
            if pd.isna(val):
                continue
            stripped = str(val).strip()
            if str(val) != stripped:
                violations.append({
                    "record_id": row[pk_col],
                    "column_name": col,
                    "current_value": str(val),
                    "suggested_fix": stripped,
                })
        return violations

    # --- Case 3: case normalization (auto-fixable) ---
    if fix_logic in ("upper", "lower", "title_case"):
        for _, row in df.iterrows():
            val = row[col]
            if pd.isna(val):
                continue
            val_str = str(val)
            if fix_logic == "upper":
                normalized = val_str.upper()
            elif fix_logic == "lower":
                normalized = val_str.lower()
            else:  # title_case
                normalized = val_str.title()
            if val_str != normalized:
                violations.append({
                    "record_id": row[pk_col],
                    "column_name": col,
                    "current_value": val_str,
                    "suggested_fix": normalized,
                })
        return violations

    return violations  # no matching sub-case - nothing to check


def check_allowed_values(df, rule, pk_col):
    col = rule["column_name"]
    params = _params(rule)
    allowed = set(params.get("values", []))
    violations = []
    for _, row in df.iterrows():
        val = row[col]
        if pd.isna(val):
            continue  # null_check rule handles this separately
        if str(val) not in allowed:
            violations.append({
                "record_id": row[pk_col],
                "column_name": col,
                "current_value": str(val),
                "suggested_fix": None,  # business decision - manual
            })
    return violations


def check_duplicate(df, rule, pk_col):
    """
    fix_action='manual' (default): flags ALL rows involved in a duplicate
        group (including the first occurrence) - human decides what to do.
    fix_action='auto' with fix_logic='delete_duplicates': keeps the FIRST
        occurrence (lowest pk_col / row order) untouched, flags only the
        LATER duplicate(s) with suggested_fix='DELETE' - i.e. only the
        records that should actually be removed are reported as violations.
    """
    col = rule["column_name"]
    fix_action = rule.get("fix_action", "manual")
    fix_logic_raw = rule.get("fix_logic")
    fix_logic = str(fix_logic_raw).lower() if pd.notna(fix_logic_raw) else ""

    violations = []

    if fix_action == "auto" and fix_logic == "delete_duplicates":
        # keep=first -> True marks every duplicate AFTER the first one
        dupe_mask = df[col].duplicated(keep="first") & df[col].notna()
        for _, row in df[dupe_mask].iterrows():
            violations.append({
                "record_id": row[pk_col],
                "column_name": col,
                "current_value": str(row[col]),
                "suggested_fix": "DELETE",  # this exact row should be removed; the first occurrence is kept
            })
        return violations

    # default / manual behavior - flag every row in the duplicate group, no fix proposed
    dupe_mask = df[col].duplicated(keep=False) & df[col].notna()
    for _, row in df[dupe_mask].iterrows():
        violations.append({
            "record_id": row[pk_col],
            "column_name": col,
            "current_value": str(row[col]),
            "suggested_fix": None,  # merge/separate decision - manual
        })
    return violations


def check_ref_integrity(df, rule, pk_col, ref_df):
    """
    df      : the table being checked (e.g. b2bunit)
    ref_df  : the table it references (e.g. b2bcustomer)
    """
    params = _params(rule)
    fk_col = params["column"]          # e.g. "customer_id" in b2bunit
    ref_pk_col = params["ref_column"]  # e.g. "customer_id" in b2bcustomer

    valid_keys = set(ref_df[ref_pk_col].dropna().astype(str))
    violations = []
    for _, row in df.iterrows():
        fk_val = row[fk_col]
        if pd.isna(fk_val):
            continue
        if str(fk_val) not in valid_keys:
            violations.append({
                "record_id": row[pk_col],
                "column_name": fk_col,
                "current_value": str(fk_val),
                "suggested_fix": None,  # reassign/delete - manual
            })
    return violations


def check_range(df, rule, pk_col):
    """
    parameters: {"min": <num, optional>, "max": <num, optional>}
    Either bound can be omitted (e.g. only check max, like discount_pct <= 100).
    Auto-fixable variant (fix_logic = "cap_min" / "cap_max") clamps the value
    to the nearest bound instead of just flagging it.
    """
    col = rule["column_name"]
    params = _params(rule)
    min_val = params.get("min")
    max_val = params.get("max")
    fix_logic = (rule.get("fix_logic") or "")
    fix_logic = fix_logic.lower() if pd.notna(fix_logic) else ""

    violations = []
    for _, row in df.iterrows():
        val = row[col]
        if pd.isna(val):
            continue
        val = float(val)
        out_of_range = (min_val is not None and val < min_val) or (max_val is not None and val > max_val)
        if not out_of_range:
            continue

        suggested_fix = None
        if fix_logic == "cap_max" and max_val is not None and val > max_val:
            suggested_fix = max_val
        elif fix_logic == "cap_min" and min_val is not None and val < min_val:
            suggested_fix = min_val

        violations.append({
            "record_id": row[pk_col],
            "column_name": col,
            "current_value": str(val),
            "suggested_fix": str(suggested_fix) if suggested_fix is not None else None,
        })
    return violations


def check_custom_sql(df, rule, pk_col):
    """
    Handles cross-field / cross-table business logic that doesn't fit the
    other rule_types. Rather than executing raw SQL (riskier, harder to
    sandbox/test), each named check is implemented as a small Python
    function and selected via parameters.check_name. This keeps things
    testable and explicit for the POC, while still being driven by config
    (you can turn checks on/off via is_active, same as any other rule).

    Supported check_name values:
      - "end_before_eff"        : end_date < eff_date on the SAME row
      - "active_on_discontinued": status='Active' but linked product.status='Discontinued'
                                   (requires ref_df = b2bproduct passed in)
    """
    params = _params(rule)
    check_name = params.get("check_name")
    violations = []

    if check_name == "end_before_eff":
        for _, row in df.iterrows():
            eff = row.get("eff_date")
            end = row.get("end_date")
            if pd.isna(eff) or pd.isna(end):
                continue
            if pd.to_datetime(end) < pd.to_datetime(eff):
                violations.append({
                    "record_id": row[pk_col],
                    "column_name": "end_date",
                    "current_value": f"end_date={end} < eff_date={eff}",
                    "suggested_fix": None,  # business clarification needed - manual
                })
        return violations

    if check_name == "active_on_discontinued":
        # caller must pass ref_df via rule["_ref_df"] (engine.py wires this up)
        ref_df = rule.get("_ref_df")
        if ref_df is None:
            return violations  # can't check without the reference table
        discontinued_ids = set(ref_df[ref_df["status"] == "Discontinued"]["product_id"].astype(str))
        for _, row in df.iterrows():
            if str(row.get("status")) == "Active" and str(row.get("product_id")) in discontinued_ids:
                violations.append({
                    "record_id": row[pk_col],
                    "column_name": "status",
                    "current_value": f"price Active on product_id={row['product_id']} (Discontinued)",
                    "suggested_fix": None,  # lifecycle decision - manual
                })
        return violations

    return violations  # unknown check_name - nothing to do


# Dispatch table - maps rule_type string to the checker function
RULE_DISPATCH = {
    "null_check": check_null_check,
    "format": check_format,
    "allowed_values": check_allowed_values,
    "duplicate": check_duplicate,
    "ref_integrity": check_ref_integrity,  # needs extra ref_df arg, handled specially in engine.py
    "range": check_range,
    "custom_sql": check_custom_sql,  # needs extra ref_df arg for some checks, handled specially in engine.py
}