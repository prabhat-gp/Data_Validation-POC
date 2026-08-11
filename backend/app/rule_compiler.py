"""
rule_compiler.py
----------------
Turns (rule_type, rule_definition) into ONE complete SQL statement.

EVERY rule type is QUERY-centric. There is no record-centric execution path
and no Python loop over data rows -- that is what the reference workbook
specifies (execution_type = QUERY on all 10 sample rules) and it is what the
engine does. Uniqueness is query-centric too: GROUP BY / HAVING inside a
subquery that the main query joins back to.

Every compiled rule returns rows shaped:  (record_key, current_value)

The SQL is NOT persisted. It is regenerated from rule_definition on every run
so the two can never drift apart.

SECURITY: SQL has no parameter binding for identifiers, so every column and
table name is validated against a strict pattern AND (where the entity is
known) against that entity's declared column list. All VALUES are bound
parameters. Free-text expression rules (CROSS_FIELD_SIMPLE, CUSTOM_SQL) run
through a token whitelist -- see _assert_safe_expression.
"""

import json
import re
from dataclasses import dataclass, field as dc_field
from typing import Optional

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class RuleCompileError(ValueError):
    pass


def assert_safe_identifier(name: str) -> str:
    if not _IDENTIFIER_RE.match(name or ""):
        raise RuleCompileError(f"Unsafe/invalid identifier: {name!r}")
    return name


@dataclass
class CompiledRule:
    """A complete SELECT returning (record_key, current_value)."""
    sql: str
    params: dict = dc_field(default_factory=dict)
    # True when a violation represents a GROUP rather than a single record
    # (AGGREGATION) -- scored against group count, not record count.
    group_level: bool = False
    # Optional COUNT(*) query giving the correct denominator for group rules.
    denominator_sql: Optional[str] = None


# ---------------------------------------------------------------------------
# free-text expression safety (CROSS_FIELD_SIMPLE / CUSTOM_SQL)
# ---------------------------------------------------------------------------
_EXPR_ALLOWED_WORDS = {
    "AND", "OR", "NOT", "IS", "NULL", "IN", "LIKE", "BETWEEN", "TRIM",
    "UPPER", "LOWER", "LENGTH", "COALESCE", "CAST", "AS", "REAL", "INTEGER",
    "TEXT", "ABS", "TRUE", "FALSE",
}
_EXPR_BANNED = re.compile(
    r"(;|--|/\*|\*/|\b(DROP|DELETE|INSERT|UPDATE|ALTER|CREATE|TRUNCATE|GRANT|"
    r"REVOKE|ATTACH|DETACH|PRAGMA|UNION|SELECT|FROM|JOIN|EXEC)\b)",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _assert_safe_expression(expr: str, allowed_columns) -> str:
    """
    Whitelist check for user-written SQL fragments. Rejects statement
    terminators, comments, and any DDL/DML keyword, then requires every bare
    identifier to be either an allowed SQL word or a real column of the entity.
    A typo'd column is therefore rejected at save time instead of silently
    compiling into SQL that matches nothing.
    """
    if not expr or not expr.strip():
        raise RuleCompileError("expression is required")
    if _EXPR_BANNED.search(expr):
        raise RuleCompileError(
            "expression contains a disallowed keyword or character "
            "(statements, comments and DDL/DML are not permitted)"
        )
    # Strip quoted string literals BEFORE tokenising -- otherwise the contents
    # of a literal like 'USA' are mistaken for a column name and rejected.
    stripped = re.sub(r"'[^']*'", "''", expr)
    known = {c.upper() for c in (allowed_columns or [])}
    for tok in _TOKEN_RE.findall(stripped):
        up = tok.upper()
        if up in _EXPR_ALLOWED_WORDS or up in known:
            continue
        raise RuleCompileError(
            f"'{tok}' is not a column of this object (or an allowed SQL keyword)"
        )
    return expr


# ---------------------------------------------------------------------------
# optional scope filter -- available on EVERY rule type
# ---------------------------------------------------------------------------
_CONDITION_OPS = {"=", "!=", "in", "is_null", "is_not_null", ">", "<", ">=", "<="}


def _one_condition(cond: dict, prefix: str, idx: int, alias: str = ""):
    col = assert_safe_identifier(cond.get("field", ""))
    q = f"{alias}{col}" if alias else col
    op = cond.get("operator")
    val = cond.get("value")
    key = f"{prefix}_{idx}"
    if op not in _CONDITION_OPS:
        raise RuleCompileError(f"unsupported operator {op!r}")
    if op in ("=", "!=", ">", "<", ">=", "<="):
        sql_op = "<>" if op == "!=" else op
        return f"{q} {sql_op} :{key}", {key: val}
    if op == "is_null":
        return f"({q} IS NULL OR TRIM({q}) = '')", {}
    if op == "is_not_null":
        return f"({q} IS NOT NULL AND TRIM({q}) <> '')", {}
    values = val if isinstance(val, list) else [v.strip() for v in str(val).split(",") if v.strip()]
    if not values:
        raise RuleCompileError(f"'in' operator on {col} requires at least one value")
    ph = ", ".join(f":{key}_{j}" for j in range(len(values)))
    return f"{q} IN ({ph})", {f"{key}_{j}": v for j, v in enumerate(values)}


def _compile_filter(config: dict, alias: str = ""):
    """{"filter": {"conditions":[...], "logic":"AND"}} -> (sql|None, params)"""
    flt = config.get("filter") or {}
    conds = flt.get("conditions") or []
    if not conds:
        return None, {}
    logic = (flt.get("logic") or "AND").upper()
    if logic not in ("AND", "OR"):
        raise RuleCompileError("filter.logic must be 'AND' or 'OR'")
    parts, params = [], {}
    for i, c in enumerate(conds):
        s, p = _one_condition(c, "flt", i, alias)
        parts.append(s)
        params.update(p)
    return f"({f' {logic} '.join(parts)})", params


def _and(*clauses) -> str:
    return " AND ".join(c for c in clauses if c)


EMPTY = "''"          # SQL empty-string literal, kept out of f-strings:
                      # Python 3.9 forbids backslashes inside f-string exprs.


def _is_blank(col: str) -> str:
    return "({c} IS NULL OR TRIM({c}) = {e})".format(c=col, e=EMPTY)


def _regex_sql(col: str, param: str) -> str:
    """
    Regex matching is the ONE genuinely dialect-specific piece of the engine.

        MySQL     value REGEXP pattern     -- operator   (what we run on)
        Postgres  value ~ pattern          -- operator
        Oracle    REGEXP_LIKE(value, pat)  -- function

    Resolved from the live connection so the same rule works on every backend.
    """
    from .database import results_engine
    name = results_engine.dialect.name
    if name in ("postgresql", "postgres"):
        return "{c} ~ :{p}".format(c=col, p=param)
    if name == "oracle":
        return "REGEXP_LIKE({c}, :{p})".format(c=col, p=param)
    return "{c} REGEXP :{p}".format(c=col, p=param)     # MySQL


def _not_blank(col: str) -> str:
    return "{c} IS NOT NULL AND TRIM({c}) <> {e}".format(c=col, e=EMPTY)


# ---------------------------------------------------------------------------
# CONTEXT passed to every compiler
# ---------------------------------------------------------------------------
@dataclass
class CompileContext:
    table: str                      # this entity's staging table
    columns: list                   # this entity's declared columns
    lookup_table: Optional[str] = None   # referenced entity's staging table
    lookup_run_id: Optional[int] = None  # its run in THIS batch
    lookup_key_field: Optional[str] = None   # referenced entity's primary key
    lookup_columns: Optional[list] = None    # its data columns


# ---------------------------------------------------------------------------
# one compiler per rule type -- all QUERY-centric
# ---------------------------------------------------------------------------
def _c_completeness(field, cfg, ctx: CompileContext) -> CompiledRule:
    col = assert_safe_identifier(field)
    f, fp = _compile_filter(cfg)
    return CompiledRule(
        sql=f"SELECT record_key, {col} AS current_value FROM {ctx.table} "
            f"WHERE {_and('run_id = :run_id', f, _is_blank(col))}",
        params=fp,
    )


def _c_validity(field, cfg, ctx: CompileContext) -> CompiledRule:
    """Regex / format check."""
    col = assert_safe_identifier(field)
    pattern = cfg.get("pattern") or cfg.get("regex")
    if not pattern:
        raise RuleCompileError("VALIDITY requires a 'pattern'")
    f, fp = _compile_filter(cfg)
    return CompiledRule(
        sql=f"SELECT record_key, {col} AS current_value FROM {ctx.table} "
            f"WHERE {_and('run_id = :run_id', f, _not_blank(col), 'NOT (' + _regex_sql(col, 'pattern') + ')')}",
        params={"pattern": pattern, **fp},
    )


def _c_range(field, cfg, ctx: CompileContext) -> CompiledRule:
    """R004: ORDER_AMOUNT < 0 OR ORDER_AMOUNT > 100000"""
    col = assert_safe_identifier(field)
    lo, hi = cfg.get("min"), cfg.get("max")
    if lo is None and hi is None:
        raise RuleCompileError("RANGE requires 'min' and/or 'max'")
    bounds, params = [], {}
    if lo is not None:
        bounds.append(f"CAST({col} AS REAL) < :rmin"); params["rmin"] = lo
    if hi is not None:
        bounds.append(f"CAST({col} AS REAL) > :rmax"); params["rmax"] = hi

    # CAST('L7E 1J9' AS REAL) silently returns 0.0 (verified on MySQL 8.3),
    # which would be reported as "below minimum" rather than "not a number".
    # Guard on an actual numeric pattern so a range rule only judges numbers.
    #   onNonNumeric = "skip" (default) -> ignore; it is a VALIDITY problem
    #   onNonNumeric = "flag"           -> report it as a range violation
    numeric = _regex_sql("TRIM({c})".format(c=col), "num_re")
    params["num_re"] = r"^-?[0-9]+(\.[0-9]+)?$"
    on_bad = str(cfg.get("onNonNumeric") or "skip").lower()
    if on_bad == "flag":
        cond = "((" + numeric + " AND (" + " OR ".join(bounds) + ")) OR NOT " + numeric + ")"
    else:
        cond = "(" + numeric + " AND (" + " OR ".join(bounds) + "))"

    f, fp = _compile_filter(cfg)
    return CompiledRule(
        sql=f"SELECT record_key, {col} AS current_value FROM {ctx.table} "
            f"WHERE {_and('run_id = :run_id', f, _not_blank(col), cond)}",
        params={**params, **fp},
    )


def _c_uniqueness(field, cfg, ctx: CompileContext) -> CompiledRule:
    """
    Sheet 2 pattern -- ONE query, GROUP BY/HAVING in a subquery joined back so
    BOTH sides of a duplicate are reported:

        SELECT C.ID, C.EMAIL FROM CUSTOMER C
        JOIN (SELECT EMAIL FROM CUSTOMER GROUP BY EMAIL HAVING COUNT(*)>1) D
          ON C.EMAIL = D.EMAIL

    Supports single field (field_name) or multi-field via
    rule_definition {"fields": ["FIRST_NAME","LAST_NAME","DOB"]} (R006).
    """
    fields = cfg.get("fields") or ([field] if field else [])
    fields = [assert_safe_identifier(f) for f in fields if f]
    if not fields:
        raise RuleCompileError("UNIQUENESS requires an element, or two or more in rule_definition.fields")

    f, fp = _compile_filter(cfg)                 # applies to the inner scan
    f_outer, _ = _compile_filter(cfg, alias="C.")  # and to the outer scan

    cols = ", ".join(fields)
    not_blank = _and(*[_not_blank(c) for c in fields])
    inner = (
        f"SELECT {cols} FROM {ctx.table} "
        f"WHERE {_and('run_id = :run_id', f, not_blank)} "
        f"GROUP BY {cols} HAVING COUNT(*) > 1"
    )
    on = " AND ".join(f"C.{c} = D.{c}" for c in fields)
    shown = fields[0] if len(fields) == 1 else " || '|' || ".join(f"C.{c}" for c in fields)
    shown_expr = f"C.{fields[0]}" if len(fields) == 1 else shown
    return CompiledRule(
        sql=f"SELECT C.record_key, {shown_expr} AS current_value "
            f"FROM {ctx.table} C JOIN ({inner}) D ON {on} "
            f"WHERE {_and('C.run_id = :run_id', f_outer)}",
        params=fp,
    )


def _c_referential_integrity(field, cfg, ctx: CompileContext) -> CompiledRule:
    """
    Sheet 2 pattern -- LEFT JOIN to the lookup table, keep the misses:

        SELECT O.ORDER_ID, O.PART_NUMBER FROM ORDERS O
        LEFT JOIN PART_MASTER P ON O.PART_NUMBER = P.PART_NUMBER
        WHERE P.PART_NUMBER IS NULL

    The lookup table is the referenced entity's STAGING table, which is why
    the batch stages every entity before validating any of them.
    """
    col = assert_safe_identifier(field)
    lookup_field = cfg.get("lookupField") or cfg.get("ref_field_name")
    if not lookup_field:
        raise RuleCompileError("REFERENTIAL_INTEGRITY requires 'lookupField'")
    lookup_field = assert_safe_identifier(lookup_field)
    if not ctx.lookup_table:
        raise RuleCompileError(
            "the referenced object is not part of this run -- include it in the batch"
        )
    # A foreign key normally points at the other table's PRIMARY KEY, and
    # staging keeps the primary key in `record_key`, not as a data column.
    # Without this the join would reference a column that does not exist.
    if ctx.lookup_key_field and lookup_field == ctx.lookup_key_field:
        lookup_col = "record_key"
    elif ctx.lookup_columns is not None and lookup_field not in ctx.lookup_columns:
        raise RuleCompileError(
            f"'{lookup_field}' is not a column (or the key) of the referenced object"
        )
    else:
        lookup_col = lookup_field
    f, fp = _compile_filter(cfg, alias="O.")
    return CompiledRule(
        sql=f"SELECT O.record_key, O.{col} AS current_value "
            f"FROM {ctx.table} O "
            f"LEFT JOIN {ctx.lookup_table} P "
            f"  ON O.{col} = P.{lookup_col} AND P.run_id = :lookup_run_id "
            f"WHERE {_and('O.run_id = :run_id', f, _not_blank('O.' + col), f'P.{lookup_col} IS NULL')}",
        params={"lookup_run_id": ctx.lookup_run_id, **fp},
    )


def _c_aggregation(field, cfg, ctx: CompileContext) -> CompiledRule:
    """
    R008: COUNT(*) per CUSTOMER_ID > 3.
    The violation is a GROUP, not a record -- record_key holds a representative
    key and current_value carries the group + its measure.
    """
    fn = str(cfg.get("aggregateFunction") or "COUNT").upper()
    if fn not in {"COUNT", "SUM", "AVG", "MIN", "MAX"}:
        raise RuleCompileError(f"unsupported aggregateFunction {fn!r}")
    agg_field = cfg.get("aggregateField") or "*"
    if agg_field != "*":
        agg_field = assert_safe_identifier(agg_field)
        if fn != "COUNT":
            agg_field = f"CAST({agg_field} AS REAL)"
    group_by = [assert_safe_identifier(g) for g in (cfg.get("groupBy") or []) if g]
    if not group_by:
        raise RuleCompileError("AGGREGATION requires 'groupBy'")
    op = cfg.get("operator") or ">"
    if op not in {">", "<", ">=", "<=", "=", "!="}:
        raise RuleCompileError(f"unsupported operator {op!r}")
    op = "<>" if op == "!=" else op
    threshold = cfg.get("threshold")
    if threshold is None:
        raise RuleCompileError("AGGREGATION requires 'threshold'")

    f, fp = _compile_filter(cfg)
    gcols = ", ".join(group_by)
    label = " || ' | ' || ".join(group_by)
    return CompiledRule(
        sql=f"SELECT MIN(record_key) AS record_key, "
            f"({label}) || ' -> {fn}=' || {fn}({agg_field}) AS current_value "
            f"FROM {ctx.table} WHERE {_and('run_id = :run_id', f)} "
            f"GROUP BY {gcols} HAVING {fn}({agg_field}) {op} :threshold",
        params={"threshold": threshold, **fp},
        group_level=True,
        # denominator for the score: total groups, not total records. "1 bad
        # group out of 101 records" is meaningless; "1 bad group out of 12
        # groups" is the honest number.
        denominator_sql=f"SELECT COUNT(*) FROM (SELECT 1 FROM {ctx.table} "
                        f"WHERE {_and('run_id = :run_id', f)} GROUP BY {gcols}) AS g",
    )


def _c_allowed_values(field, cfg, ctx: CompileContext) -> CompiledRule:
    """Sheet 2: WHERE COUNTRY NOT IN ('US','IN','CA','UK')"""
    col = assert_safe_identifier(field)
    values = cfg.get("allowedValues") or cfg.get("values") or []
    if not values:
        raise RuleCompileError("ALLOWED_VALUES requires 'allowedValues'")
    ph = ", ".join(f":av_{i}" for i in range(len(values)))
    params = {f"av_{i}": v for i, v in enumerate(values)}
    f, fp = _compile_filter(cfg)
    return CompiledRule(
        sql=f"SELECT record_key, {col} AS current_value FROM {ctx.table} "
            f"WHERE {_and('run_id = :run_id', f, _not_blank(col), f'{col} NOT IN ({ph})')}",
        params={**params, **fp},
    )


def _c_cross_field_simple(field, cfg, ctx: CompileContext) -> CompiledRule:
    """R009: {"expression": "COUNTRY='US' AND STATE IS NULL"}"""
    col = assert_safe_identifier(field) if field else "record_key"
    expr = _assert_safe_expression(cfg.get("expression", ""), ctx.columns)
    f, fp = _compile_filter(cfg)
    return CompiledRule(
        sql=f"SELECT record_key, {col} AS current_value FROM {ctx.table} "
            f"WHERE {_and('run_id = :run_id', f, f'({expr})')}",
        params=fp,
    )


def _c_custom_sql(field, cfg, ctx: CompileContext) -> CompiledRule:
    """
    Escape hatch: a raw WHERE expression. Same whitelist as CROSS_FIELD_SIMPLE
    -- no statements, no DDL/DML, only this entity's own columns. It is NOT an
    arbitrary-SQL backdoor.
    """
    col = assert_safe_identifier(field) if field else "record_key"
    expr = _assert_safe_expression(cfg.get("expression") or cfg.get("sql") or "", ctx.columns)
    f, fp = _compile_filter(cfg)
    return CompiledRule(
        sql=f"SELECT record_key, {col} AS current_value FROM {ctx.table} "
            f"WHERE {_and('run_id = :run_id', f, f'({expr})')}",
        params=fp,
    )


_COMPILERS = {
    "COMPLETENESS":          _c_completeness,
    "VALIDITY":              _c_validity,
    "RANGE":                 _c_range,
    "UNIQUENESS":            _c_uniqueness,
    "REFERENTIAL_INTEGRITY": _c_referential_integrity,
    "AGGREGATION":           _c_aggregation,
    "ALLOWED_VALUES":        _c_allowed_values,
    "CROSS_FIELD_SIMPLE":    _c_cross_field_simple,
    "CUSTOM_SQL":            _c_custom_sql,
}

RULE_TYPES = list(_COMPILERS.keys())

# rule_type -> (dashboard dimension, execution_type).
# execution_type is QUERY for every type -- matching the reference workbook.
# The six quality dimensions the dashboard reports on. This list is the single
# source of truth -- the heatmap columns, the rule form dropdown and the
# lookup table are all driven from it, so a dimension can never again exist in
# one place and not the other.
#
# Deliberately NOT included: Timeliness. No rule type measures freshness (no
# as-of date, no SLA), so a Timeliness column would be permanently empty --
# which is the exact bug the old "Format" and "Relationship" columns were.
# Adding it means adding a rule type first.
DIMENSIONS = [
    "Completeness",   # is the value there
    "Validity",       # does it conform to a defined format or domain
    "Uniqueness",     # is it recorded exactly once
    "Consistency",    # do fields agree with each other
    "Integrity",      # does the reference resolve
    "Accuracy",       # is the value plausible against reality
]

# DEFAULT dimension per rule type. It is only a default: dimension is stored on
# val_rules and the author can override it, because the same rule type serves
# different dimensions depending on intent --
#   RANGE on discount_pct 0-100      -> Validity  (a percent above 100 is invalid)
#   RANGE on price_amount 0-100000   -> Accuracy  (100001 is legal, just implausible)
#   AGGREGATION "accounts per website" -> Accuracy   (reasonableness)
#   AGGREGATION "one price per part"   -> Uniqueness (group-level duplicate)
RULE_TYPE_META = {
    "COMPLETENESS":          ("Completeness", "QUERY"),
    "VALIDITY":              ("Validity",     "QUERY"),
    "RANGE":                 ("Validity",     "QUERY"),
    "ALLOWED_VALUES":        ("Validity",     "QUERY"),
    "UNIQUENESS":            ("Uniqueness",   "QUERY"),
    "CROSS_FIELD_SIMPLE":    ("Consistency",  "QUERY"),
    "CUSTOM_SQL":            ("Consistency",  "QUERY"),
    "REFERENTIAL_INTEGRITY": ("Integrity",    "QUERY"),
    "AGGREGATION":           ("Accuracy",     "QUERY"),
}

# Labels retired in the dimension rework, kept only so historical val_metrics
# rows can be re-pointed. See migrate_db.py.
RETIRED_DIMENSIONS = {
    "Ref Integrity": "Integrity",
    "Relationship":  "Integrity",
    "Format":        "Validity",
}

RULE_TYPE_DESCRIPTIONS = {
    "COMPLETENESS":          "Field must not be empty",
    "VALIDITY":              "Value must match a format / pattern",
    "RANGE":                 "Numeric value must fall inside min/max",
    "UNIQUENESS":            "No duplicates on one field or a combination of fields",
    "REFERENTIAL_INTEGRITY": "Value must exist in a lookup entity (LEFT JOIN)",
    "AGGREGATION":           "Grouped measure must satisfy a threshold",
    "ALLOWED_VALUES":        "Value must be one of a fixed list",
    "CROSS_FIELD_SIMPLE":    "Condition across fields of the same record",
    "CUSTOM_SQL":            "Custom expression over this entity's columns",
}


def dimension_for(rule_type: str) -> str:
    return RULE_TYPE_META.get(rule_type, ("Validity", "QUERY"))[0]


def execution_type_for(rule_type: str) -> str:
    return "QUERY"      # every rule type is query-centric


def referenced_entity(rule_type: str, rule_definition) -> Optional[str]:
    """Which OTHER entity this rule needs staged (REFERENTIAL_INTEGRITY only)."""
    if rule_type != "REFERENTIAL_INTEGRITY":
        return None
    cfg = json.loads(rule_definition) if isinstance(rule_definition, str) else (rule_definition or {})
    return cfg.get("lookupTable") or cfg.get("ref_entity_name")


def fields_referenced(rule_type: str, field_name: str, rule_definition) -> set:
    cfg = json.loads(rule_definition) if isinstance(rule_definition, str) else (rule_definition or {})
    cfg = cfg or {}
    out = {field_name} if field_name else set()
    out.update(cfg.get("fields") or [])
    out.update(cfg.get("groupBy") or [])
    for c in (cfg.get("filter") or {}).get("conditions") or []:
        if c.get("field"):
            out.add(c["field"])
    return {f for f in out if f}


def compile_rule(rule_type: str, field_name: str, rule_definition, ctx: CompileContext) -> CompiledRule:
    fn = _COMPILERS.get(rule_type)
    if fn is None:
        raise RuleCompileError(f"Unknown rule_type {rule_type!r}. Supported: {RULE_TYPES}")
    cfg = json.loads(rule_definition) if isinstance(rule_definition, str) and rule_definition else (rule_definition or {})
    return fn(field_name, cfg or {}, ctx)
