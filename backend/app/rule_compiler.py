"""
rule_compiler.py
----------------
Turns a rule's (rule_type, rule_config_json) into SQL. This is the "rules as
data, not code" boundary: adding a new rule NEVER requires a code change --
it requires picking one of the rule types below and filling in its config in
the UI.

Every compiled rule has a `mode`:
  "predicate" -- most rule types. Produces a WHERE fragment that matches
                 FAILING rows. The engine runs:
                     SELECT record_key, {element} FROM stg_source_record
                     WHERE run_id = :run_id AND ({condition})
                 as ONE query, pushed down to the database. No Python loop
                 over rows.
  "duplicate" -- the Unique rule type. Per direction: still a single,
                 ordinary SQL query (GROUP BY / HAVING), not a special
                 "record-centric" execution path. The engine runs:
                     SELECT {element}, COUNT(*) AS c
                     FROM stg_source_record
                     WHERE run_id = :run_id
                     GROUP BY {element}
                     HAVING COUNT(*) > 1
                 then a second pass fetches every record_key sharing each
                 duplicated value.

SECURITY NOTE: element/column names come from our own DQ_ELEMENT catalog, but
because SQL doesn't allow parameterized identifiers, every column name is
still validated against a strict identifier pattern (and, in practice,
against the known CDE_COLUMNS whitelist) before being spliced into SQL text.
Rule CONFIG VALUES (allowed values, lengths, patterns) are always passed as
bound parameters, never string-formatted into the query.
"""

import json
import re
from dataclasses import dataclass
from typing import Any

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class RuleCompileError(ValueError):
    pass


def assert_safe_identifier(name: str) -> str:
    if not _IDENTIFIER_RE.match(name or ""):
        raise RuleCompileError(f"Unsafe/invalid column identifier: {name!r}")
    return name


@dataclass
class CompiledRule:
    mode: str            # "predicate" | "duplicate"
    condition_sql: str    # WHERE-fragment (predicate mode) -- failing-row condition
    params: dict          # bind parameters referenced by condition_sql


# ---- one compiler function per rule_type ------------------------------------

def _compile_required(element: str, config: dict) -> CompiledRule:
    col = assert_safe_identifier(element)
    return CompiledRule(
        mode="predicate",
        condition_sql=f"({col} IS NULL OR TRIM({col}) = '')",
        params={},
    )


def _compile_allowed_values(element: str, config: dict) -> CompiledRule:
    col = assert_safe_identifier(element)
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


def _compile_format_pattern(element: str, config: dict) -> CompiledRule:
    col = assert_safe_identifier(element)
    pattern = config.get("pattern")
    if not pattern:
        raise RuleCompileError("format_pattern rule requires a 'pattern' regex")
    # REGEXP is registered as a custom SQLite function (see database.py / engine setup).
    # On Oracle/Postgres this becomes REGEXP_LIKE / operator ~ -- swap at the dialect layer.
    return CompiledRule(
        mode="predicate",
        condition_sql=f"({col} IS NOT NULL AND TRIM({col}) <> '' AND NOT REGEXP(:pattern, {col}))",
        params={"pattern": pattern},
    )


def _compile_max_length(element: str, config: dict) -> CompiledRule:
    col = assert_safe_identifier(element)
    max_len = config.get("max_length")
    if not isinstance(max_len, int) or max_len <= 0:
        raise RuleCompileError("max_length rule requires a positive integer 'max_length'")
    return CompiledRule(
        mode="predicate",
        condition_sql=f"(LENGTH({col}) > :max_len)",
        params={"max_len": max_len},
    )


def _compile_unique(element: str, config: dict) -> CompiledRule:
    col = assert_safe_identifier(element)
    # duplicate check -- plain SQL GROUP BY/HAVING, run the same way as every
    # other rule (per direction: not a separate "record-centric" path).
    return CompiledRule(mode="duplicate", condition_sql=col, params={})


def _compile_conditional_required(element: str, config: dict) -> CompiledRule:
    then_col = assert_safe_identifier(element)
    if_field = assert_safe_identifier(config.get("if_field", ""))
    if_value = config.get("if_value")
    if if_value is None:
        raise RuleCompileError("conditional_required requires 'if_field' and 'if_value'")
    return CompiledRule(
        mode="predicate",
        condition_sql=(
            f"({if_field} = :if_value AND ({then_col} IS NULL OR TRIM({then_col}) = ''))"
        ),
        params={"if_value": if_value},
    )


def _compile_ref_integrity(element: str, config: dict) -> CompiledRule:
    """
    Query-centric per the classification you shared (LEFT JOIN / NOT IN
    pattern) -- one SQL query, no special execution path. It checks this
    row's FK column against dq_reference_value, NOT against the referenced
    object's live staging (which may already be cleared -- see models.py).
    """
    col = assert_safe_identifier(element)
    ref_object_id = config.get("ref_object_id")
    ref_element_id = config.get("ref_element_id")
    if not ref_object_id or not ref_element_id:
        raise RuleCompileError("ref_integrity requires 'ref_object_id' and 'ref_element_id'")
    return CompiledRule(
        mode="predicate",
        condition_sql=(
            f"({col} IS NOT NULL AND TRIM({col}) <> '' AND {col} NOT IN ("
            f"SELECT value FROM dq_reference_value "
            f"WHERE object_id = :ref_object_id AND element_id = :ref_element_id"
            f"))"
        ),
        params={"ref_object_id": ref_object_id, "ref_element_id": ref_element_id},
    )


_CONDITION_OPS = {"=", "!=", "in", "is_null", "is_not_null"}


def _compile_one_condition(cond: dict, idx: int) -> tuple[str, dict]:
    col = assert_safe_identifier(cond.get("field", ""))
    op = cond.get("operator")
    val = cond.get("value")
    if op not in _CONDITION_OPS:
        raise RuleCompileError(f"unsupported operator {op!r}; use one of {sorted(_CONDITION_OPS)}")
    if op == "=":
        return f"{col} = :cv_{idx}", {f"cv_{idx}": val}
    if op == "!=":
        return f"{col} <> :cv_{idx}", {f"cv_{idx}": val}
    if op == "is_null":
        return f"({col} IS NULL OR TRIM({col}) = '')", {}
    if op == "is_not_null":
        return f"({col} IS NOT NULL AND TRIM({col}) <> '')", {}
    # op == "in"
    values = val if isinstance(val, list) else [v.strip() for v in str(val).split(",") if v.strip()]
    if not values:
        raise RuleCompileError(f"'in' operator on {col} requires at least one value")
    placeholders = ", ".join(f":cv_{idx}_{j}" for j in range(len(values)))
    params = {f"cv_{idx}_{j}": v for j, v in enumerate(values)}
    return f"{col} IN ({placeholders})", params


def _compile_multi_condition(element: str, config: dict) -> CompiledRule:
    """
    Chained, multi-field business rules (e.g. "if Type=Hotel AND Brand=X
    AND Country IN [...] then require export_license"). Still query-centric
    under the hood -- every field referenced belongs to the SAME row, so it
    compiles to one multi-clause SQL WHERE, same execution path as every
    other predicate rule. NOT a record-loaded rules-engine (Drools/BRF+
    style) -- that's a different, heavier thing, not what this builds.
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
        clause, p = _compile_one_condition(cond, i)
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


def compile_rule(rule_type: str, source_column_name: str, rule_config_json: str) -> CompiledRule:
    fn = _COMPILERS.get(rule_type)
    if fn is None:
        raise RuleCompileError(f"Unknown rule_type: {rule_type!r}. Supported: {RULE_TYPES}")
    config: dict[str, Any] = json.loads(rule_config_json) if rule_config_json else {}
    return fn(source_column_name, config)
