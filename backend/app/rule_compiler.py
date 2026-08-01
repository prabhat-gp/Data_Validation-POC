"""
rule_compiler.py
----------------
Turns a rule's (rule_type, rule_definition) into SQL. This is the "rules as
data, not code" boundary: adding a rule NEVER requires a code change -- it
requires picking one of the rule types below and filling in its config.

The compiled SQL is NOT persisted. It is regenerated from rule_definition on
every run, so the stored config and the executed SQL can never drift apart.

EXECUTION MODE -- derived here, never taken from user input:
  "predicate" (7 of 8 types) -- produces a WHERE fragment matching FAILING
      rows. The engine runs ONE query per rule:
          SELECT record_key, {field} FROM {staging}
          WHERE run_id = :run_id AND ({condition})
      pushed down to the database. No Python loop over data rows.

  "duplicate" (Unique only) -- still ordinary SQL (GROUP BY / HAVING), not a
      special "record-centric" engine. Needs two passes because you cannot
      tell whether a value is duplicated by looking at one row.

OPTIONAL FILTER -- any rule type may carry a "filter" in its definition to
scope it to a subset of records, e.g. only check Name is present for
Owner/Operator accounts. Empty/absent filter == the whole table.

SECURITY: SQL does not allow parameterized identifiers, so every column name
is validated against a strict identifier pattern before being spliced into
SQL text. Rule VALUES (allowed values, lengths, patterns, filter values) are
ALWAYS passed as bound parameters, never string-formatted into the query.
"""

import json
import re
from dataclasses import dataclass, field as dc_field
from typing import Any, Optional

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class RuleCompileError(ValueError):
    pass


def assert_safe_identifier(name: str) -> str:
    if not _IDENTIFIER_RE.match(name or ""):
        raise RuleCompileError(f"Unsafe/invalid column identifier: {name!r}")
    return name


@dataclass
class CompiledRule:
    mode: str                 # "predicate" | "duplicate"
    condition_sql: str        # WHERE-fragment (predicate) / column name (duplicate)
    params: dict
    # Optional scope filter, applied IN ADDITION to condition_sql. Kept
    # separate because the duplicate path must apply it to BOTH of its passes.
    filter_sql: Optional[str] = None
    filter_params: dict = dc_field(default_factory=dict)


# ---- optional scope filter ---------------------------------------------------

_CONDITION_OPS = {"=", "!=", "in", "is_null", "is_not_null"}


def _compile_one_condition(cond: dict, prefix: str, idx: int) -> tuple:
    col = assert_safe_identifier(cond.get("field", ""))
    op = cond.get("operator")
    val = cond.get("value")
    key = f"{prefix}_{idx}"
    if op not in _CONDITION_OPS:
        raise RuleCompileError(f"unsupported operator {op!r}; use one of {sorted(_CONDITION_OPS)}")
    if op == "=":
        return f"{col} = :{key}", {key: val}
    if op == "!=":
        return f"{col} <> :{key}", {key: val}
    if op == "is_null":
        return f"({col} IS NULL OR TRIM({col}) = '')", {}
    if op == "is_not_null":
        return f"({col} IS NOT NULL AND TRIM({col}) <> '')", {}
    values = val if isinstance(val, list) else [v.strip() for v in str(val).split(",") if v.strip()]
    if not values:
        raise RuleCompileError(f"'in' operator on {col} requires at least one value")
    placeholders = ", ".join(f":{key}_{j}" for j in range(len(values)))
    params = {f"{key}_{j}": v for j, v in enumerate(values)}
    return f"{col} IN ({placeholders})", params


def _compile_filter(config: dict) -> tuple:
    """
    Optional scope filter shared by every rule type:
        {"filter": {"conditions": [...], "logic": "AND"}}
    Returns (sql_or_None, params). Absent/empty filter -> (None, {}).
    """
    flt = config.get("filter") or {}
    conditions = flt.get("conditions") or []
    if not conditions:
        return None, {}
    logic = (flt.get("logic") or "AND").upper()
    if logic not in ("AND", "OR"):
        raise RuleCompileError("filter.logic must be 'AND' or 'OR'")
    clauses, params = [], {}
    for i, cond in enumerate(conditions):
        clause, p = _compile_one_condition(cond, "flt", i)
        clauses.append(clause)
        params.update(p)
    return f"({f' {logic} '.join(clauses)})", params


# ---- one compiler function per rule_type ------------------------------------

def _compile_required(field: str, config: dict) -> CompiledRule:
    col = assert_safe_identifier(field)
    return CompiledRule(
        mode="predicate",
        condition_sql=f"({col} IS NULL OR TRIM({col}) = '')",
        params={},
    )


def _compile_allowed_values(field: str, config: dict) -> CompiledRule:
    col = assert_safe_identifier(field)
    values = config.get("values") or []
    if not values:
        raise RuleCompileError("allowed_values rule requires a non-empty 'values' list")
    placeholders = ", ".join(f":av_{i}" for i in range(len(values)))
    params = {f"av_{i}": v for i, v in enumerate(values)}
    return CompiledRule(
        mode="predicate",
        condition_sql=f"({col} IS NOT NULL AND TRIM({col}) <> '' AND {col} NOT IN ({placeholders}))",
        params=params,
    )


def _compile_format_pattern(field: str, config: dict) -> CompiledRule:
    col = assert_safe_identifier(field)
    pattern = config.get("pattern")
    if not pattern:
        raise RuleCompileError("format_pattern rule requires a 'pattern' regex")
    # REGEXP is registered as a custom SQLite function (see database.py).
    # On Oracle/Postgres this becomes REGEXP_LIKE / operator ~ at the dialect layer.
    return CompiledRule(
        mode="predicate",
        condition_sql=f"({col} IS NOT NULL AND TRIM({col}) <> '' AND NOT REGEXP(:pattern, {col}))",
        params={"pattern": pattern},
    )


def _compile_max_length(field: str, config: dict) -> CompiledRule:
    col = assert_safe_identifier(field)
    max_len = config.get("max_length")
    if not isinstance(max_len, int) or max_len <= 0:
        raise RuleCompileError("max_length rule requires a positive integer 'max_length'")
    return CompiledRule(
        mode="predicate",
        condition_sql=f"(LENGTH({col}) > :max_len)",
        params={"max_len": max_len},
    )


def _compile_unique(field: str, config: dict) -> CompiledRule:
    """
    Duplicate check -- plain SQL GROUP BY/HAVING, run through the same engine
    path as everything else. condition_sql is just the column name; the two
    passes are built in validation_engine.py.
    """
    col = assert_safe_identifier(field)
    return CompiledRule(mode="duplicate", condition_sql=col, params={})


def _compile_conditional_required(field: str, config: dict) -> CompiledRule:
    then_col = assert_safe_identifier(field)
    if_field = assert_safe_identifier(config.get("if_field", ""))
    if_value = config.get("if_value")
    if if_value is None:
        raise RuleCompileError("conditional_required requires 'if_field' and 'if_value'")
    return CompiledRule(
        mode="predicate",
        condition_sql=f"({if_field} = :if_value AND ({then_col} IS NULL OR TRIM({then_col}) = ''))",
        params={"if_value": if_value},
    )


def _compile_ref_integrity(field: str, config: dict) -> CompiledRule:
    """
    Query-centric (NOT IN / LEFT JOIN pattern) -- one SQL query, no special
    execution path. Checks this row's FK column against val_reference_values,
    NOT against the referenced entity's live staging (which may already be
    cleared -- see models.py).
    """
    col = assert_safe_identifier(field)
    ref_entity = config.get("ref_entity_name")
    ref_field = config.get("ref_field_name")
    if not ref_entity or not ref_field:
        raise RuleCompileError("ref_integrity requires 'ref_entity_name' and 'ref_field_name'")
    return CompiledRule(
        mode="predicate",
        condition_sql=(
            f"({col} IS NOT NULL AND TRIM({col}) <> '' AND {col} NOT IN ("
            f"SELECT value FROM val_reference_values "
            f"WHERE entity_name = :ref_entity AND field_name = :ref_field"
            f"))"
        ),
        params={"ref_entity": ref_entity, "ref_field": ref_field},
    )


def _compile_multi_condition(field: str, config: dict) -> CompiledRule:
    """
    Chained, multi-field business rules. Still query-centric: every field
    referenced belongs to the SAME row, so it compiles to one multi-clause
    WHERE, same execution path as every other predicate rule. NOT a
    record-loaded rules engine (Drools/BRF+ style) -- that is a different,
    heavier thing and is deliberately not built here.
    """
    conditions = config.get("conditions") or []
    logic = (config.get("logic") or "AND").upper()
    then = config.get("then") or {}
    if not conditions:
        raise RuleCompileError("multi_condition requires at least one condition")
    if logic not in ("AND", "OR"):
        raise RuleCompileError("logic must be 'AND' or 'OR'")

    clauses, params = [], {}
    for i, cond in enumerate(conditions):
        clause, p = _compile_one_condition(cond, "cv", i)
        clauses.append(clause)
        params.update(p)
    if_expr = f" {logic} ".join(clauses)

    then_type = then.get("type")
    if then_type == "require":
        then_col = assert_safe_identifier(then.get("field", ""))
        condition_sql = f"(({if_expr}) AND ({then_col} IS NULL OR TRIM({then_col}) = ''))"
    elif then_type == "flag":
        condition_sql = f"({if_expr})"
    else:
        raise RuleCompileError("then.type must be 'require' or 'flag'")

    return CompiledRule(mode="predicate", condition_sql=condition_sql, params=params)


_COMPILERS = {
    "required": _compile_required,
    "allowed_values": _compile_allowed_values,
    "format_pattern": _compile_format_pattern,
    "max_length": _compile_max_length,
    "unique": _compile_unique,
    "conditional_required": _compile_conditional_required,
    "ref_integrity": _compile_ref_integrity,
    "multi_condition": _compile_multi_condition,
}

RULE_TYPES = list(_COMPILERS.keys())

# rule_type -> (dimension, execution_type). Both DERIVED, never user input:
# storing execution_type as a free field would allow a 'required' rule to be
# marked RECORD and take the wrong engine path.
RULE_TYPE_META = {
    "required":             ("Completeness",  "QUERY"),
    "allowed_values":       ("Validity",      "QUERY"),
    "format_pattern":       ("Format",        "QUERY"),
    "max_length":           ("Format",        "QUERY"),
    "unique":               ("Uniqueness",    "RECORD"),
    "conditional_required": ("Consistency",   "QUERY"),
    "ref_integrity":        ("Ref Integrity", "QUERY"),
    "multi_condition":      ("Consistency",   "QUERY"),
}

RULE_TYPE_DESCRIPTIONS = {
    "required":             "Field must not be empty",
    "allowed_values":       "Value must be one of a fixed list",
    "format_pattern":       "Value must match a regular expression",
    "max_length":           "Value must not exceed a character limit",
    "unique":               "No two records may share the same value",
    "conditional_required": "Field is mandatory only when another field has a given value",
    "ref_integrity":        "Value must already exist in another entity's field",
    "multi_condition":      "Several conditions on the same record, chained with AND/OR",
}


def dimension_for(rule_type: str) -> str:
    return RULE_TYPE_META.get(rule_type, ("Validity", "QUERY"))[0]


def execution_type_for(rule_type: str) -> str:
    return RULE_TYPE_META.get(rule_type, ("Validity", "QUERY"))[1]


def fields_referenced(rule_type: str, field_name: str, rule_definition: str) -> set:
    """
    Every column a rule touches -- used to prune staging to only what the
    selected rules actually need, and to fail fast when a source file is
    missing a required column.
    """
    config = json.loads(rule_definition) if rule_definition else {}
    fields = {field_name}
    if rule_type == "conditional_required" and config.get("if_field"):
        fields.add(config["if_field"])
    if rule_type == "multi_condition":
        for cond in config.get("conditions") or []:
            if cond.get("field"):
                fields.add(cond["field"])
        then = config.get("then") or {}
        if then.get("field"):
            fields.add(then["field"])
    for cond in (config.get("filter") or {}).get("conditions") or []:
        if cond.get("field"):
            fields.add(cond["field"])
    return fields


def compile_rule(rule_type: str, field_name: str, rule_definition: str) -> CompiledRule:
    fn = _COMPILERS.get(rule_type)
    if fn is None:
        raise RuleCompileError(f"Unknown rule_type: {rule_type!r}. Supported: {RULE_TYPES}")
    config: dict = json.loads(rule_definition) if rule_definition else {}
    compiled = fn(field_name, config)
    compiled.filter_sql, compiled.filter_params = _compile_filter(config)
    return compiled
