"""
violation_query.py
------------------
Turns a rule into a SQL statement the user can paste straight into their own
database client to SEE the rows that failed.

WHY THIS IS NOT THE ENGINE'S SQL
    rule_compiler.py targets the STAGING tables in target_db: it selects
    `record_key`, filters on `run_id`, and joins other staging tables. None of
    that exists in the user's source database, so its SQL is unrunnable there.

    This module emits the same LOGIC against the SOURCE tables:
        stg_b2b_customer  ->  b2bcustomer          (source_object_name)
        record_key        ->  customer_id          (primary_key_field)
        run_id = :run_id  ->  dropped
        :bound params     ->  inlined as literals  (so it is copy-pasteable)
        + LIMIT n

    Every predicate is a mirror of the matching compiler in rule_compiler.py.
    If one changes, change both -- tests/test_violation_query.py fails loudly
    when the row counts stop agreeing with what the engine recorded.

DIALECT
    MySQL, because that is what source_db is. The only dialect-specific piece
    is regex: MySQL uses the `col REGEXP 'pat'` operator.
"""

import json
from typing import Optional

from .rule_compiler import assert_safe_identifier

DEFAULT_LIMIT = 10


def _lit(v) -> str:
    """A SQL literal. Strings are quoted with '' escaping; numbers pass through."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("\\", "\\\\").replace("'", "''") + "'"


def _blank(col: str) -> str:
    return f"({col} IS NULL OR TRIM({col}) = '')"


def _filled(col: str) -> str:
    return f"({col} IS NOT NULL AND TRIM({col}) <> '')"


def _and(*parts) -> str:
    return "\n  AND ".join(p for p in parts if p)


def _filter_sql(cfg: dict, alias: str = "") -> Optional[str]:
    """Mirror of rule_compiler._compile_filter, with values inlined."""
    flt = cfg.get("filter") or {}
    conds = flt.get("conditions") or []
    if not conds:
        return None
    logic = (flt.get("logic") or "AND").upper()
    out = []
    for c in conds:
        col = alias + assert_safe_identifier(c.get("field", ""))
        op = c.get("operator")
        val = c.get("value")
        if op in ("=", "!=", ">", "<", ">=", "<="):
            out.append(f"{col} {'<>' if op == '!=' else op} {_lit(val)}")
        elif op == "is_null":
            out.append(_blank(col))
        elif op == "is_not_null":
            out.append(_filled(col))
        elif op == "in":
            vals = val if isinstance(val, list) else [
                v.strip() for v in str(val).split(",") if v.strip()]
            out.append(f"{col} IN ({', '.join(_lit(v) for v in vals)})")
    return "(" + f" {logic} ".join(out) + ")" if out else None


# ---------------------------------------------------------------------------
# one builder per rule type -- each mirrors the compiler of the same name
# ---------------------------------------------------------------------------
def _q_completeness(col, cfg, t, key, meta, lk):
    return (f"SELECT {key}, {col}\nFROM {t}\nWHERE " +
            _and(_filter_sql(cfg), _blank(col)))


def _q_validity(col, cfg, t, key, meta, lk):
    pattern = cfg.get("pattern") or cfg.get("regex")
    # blanks are skipped -- missing is a Completeness problem, not a format one
    return (f"SELECT {key}, {col}\nFROM {t}\nWHERE " +
            _and(_filter_sql(cfg), _filled(col),
                 f"NOT ({col} REGEXP {_lit(pattern)})"))


def _q_range(col, cfg, t, key, meta, lk):
    lo, hi = cfg.get("min"), cfg.get("max")
    bounds = []
    if lo is not None:
        bounds.append(f"CAST({col} AS DECIMAL(38,10)) < {_lit(lo)}")
    if hi is not None:
        bounds.append(f"CAST({col} AS DECIMAL(38,10)) > {_lit(hi)}")
    numeric = f"TRIM({col}) REGEXP '^-?[0-9]+(\\\\.[0-9]+)?$'"
    on_bad = str(cfg.get("onNonNumeric") or "skip").lower()
    if on_bad == "flag":
        cond = f"(({numeric} AND ({' OR '.join(bounds)})) OR NOT {numeric})"
    else:
        cond = f"({numeric} AND ({' OR '.join(bounds)}))"
    return f"SELECT {key}, {col}\nFROM {t}\nWHERE " + _and(_filter_sql(cfg), cond)


def _q_allowed_values(col, cfg, t, key, meta, lk):
    vals = cfg.get("allowedValues") or cfg.get("values") or []
    lst = ", ".join(_lit(v) for v in vals)
    return (f"SELECT {key}, {col}\nFROM {t}\nWHERE " +
            _and(_filter_sql(cfg), _filled(col), f"{col} NOT IN ({lst})"))


def _q_uniqueness(col, cfg, t, key, meta, lk):
    fields = cfg.get("fields") or ([col] if col else [])
    fields = [assert_safe_identifier(f) for f in fields if f]
    cols = ", ".join(fields)
    not_blank = _and(*[_filled(c) for c in fields])
    inner_where = _and(_filter_sql(cfg), not_blank)
    on = " AND ".join(f"C.{c} = D.{c}" for c in fields)
    outer = _filter_sql(cfg, alias="C.")
    sel = ", ".join(f"C.{c}" for c in fields)
    sql = (f"SELECT C.{key}, {sel}\n"
           f"FROM {t} C\n"
           f"JOIN (\n"
           f"    SELECT {cols}\n"
           f"    FROM {t}\n"
           f"    WHERE {inner_where}\n"
           f"    GROUP BY {cols}\n"
           f"    HAVING COUNT(*) > 1\n"
           f") D ON {on}")
    if outer:
        sql += f"\nWHERE {outer}"
    return sql + f"\nORDER BY {sel}"      # duplicates land next to each other


def _q_referential_integrity(col, cfg, t, key, meta, lk):
    lookup_field = assert_safe_identifier(cfg.get("lookupField") or cfg.get("ref_field_name"))
    if not lk:
        raise ValueError("referenced object is not configured")
    # In staging the referenced key lives in record_key; in the SOURCE table it
    # is a real column, so join on the column name directly.
    return (f"SELECT O.{key}, O.{col}\n"
            f"FROM {t} O\n"
            f"LEFT JOIN {lk['source_object_name']} P\n"
            f"    ON O.{col} = P.{lookup_field}\n"
            f"WHERE " + _and(_filter_sql(cfg, alias="O."),
                             _filled(f"O.{col}"),
                             f"P.{lookup_field} IS NULL"))


def _q_aggregation(col, cfg, t, key, meta, lk):
    fn = str(cfg.get("aggregateFunction") or "COUNT").upper()
    agg_field = cfg.get("aggregateField") or "*"
    if agg_field != "*":
        agg_field = assert_safe_identifier(agg_field)
    group_by = [assert_safe_identifier(g) for g in (cfg.get("groupBy") or [])]
    op = cfg.get("operator") or ">"
    threshold = cfg.get("threshold")
    gcols = ", ".join(group_by)
    where = _filter_sql(cfg)
    sql = (f"SELECT {gcols}, {fn}({agg_field}) AS measure\n"
           f"FROM {t}\n")
    if where:
        sql += f"WHERE {where}\n"
    sql += (f"GROUP BY {gcols}\n"
            f"HAVING {fn}({agg_field}) {op} {_lit(threshold)}\n"
            f"ORDER BY measure DESC")
    return sql


def _q_expression(col, cfg, t, key, meta, lk):
    expr = cfg.get("expression") or cfg.get("sql") or ""
    sel = f"{key}, {col}" if col else key
    return f"SELECT {sel}\nFROM {t}\nWHERE " + _and(_filter_sql(cfg), f"({expr})")


_BUILDERS = {
    "COMPLETENESS": _q_completeness,
    "VALIDITY": _q_validity,
    "RANGE": _q_range,
    "ALLOWED_VALUES": _q_allowed_values,
    "UNIQUENESS": _q_uniqueness,
    "REFERENTIAL_INTEGRITY": _q_referential_integrity,
    "AGGREGATION": _q_aggregation,
    "CROSS_FIELD_SIMPLE": _q_expression,
    "CUSTOM_SQL": _q_expression,
}

# What the returned rows mean, shown above the SQL in the UI.
_MEANING = {
    "AGGREGATION": "One row per failing GROUP, not per record — this rule's "
                   "violation is the group itself.",
    "UNIQUENESS": "Every member of each duplicate set is returned, not just "
                  "the extras, so you can see which record to keep.",
    "REFERENTIAL_INTEGRITY": "Rows whose reference does not resolve in the "
                             "lookup object.",
}


def build(rule, entities: dict, limit: Optional[int] = DEFAULT_LIMIT) -> dict:
    """
    SQL that returns the rows this rule failed on, against the SOURCE database.

    `limit=None` returns it uncapped -- used by the backtest to compare counts
    against what the engine recorded.
    """
    meta = entities.get(rule.entity_name)
    if meta is None:
        raise ValueError(f"Unknown object: {rule.entity_name}")

    try:
        cfg = json.loads(rule.rule_definition or "{}")
    except Exception:  # noqa: BLE001
        cfg = {}

    builder = _BUILDERS.get(rule.rule_type)
    if builder is None:
        raise ValueError(f"No query builder for {rule.rule_type}")

    table = meta["source_object_name"]
    key = assert_safe_identifier(meta["primary_key_field"])
    col = assert_safe_identifier(rule.field_name) if rule.field_name else ""

    lk = None
    if rule.rule_type == "REFERENTIAL_INTEGRITY":
        lk = entities.get(cfg.get("lookupTable") or cfg.get("ref_entity_name"))

    sql = builder(col, cfg, table, key, meta, lk)
    if limit:
        sql += f"\nLIMIT {int(limit)}"
    return {
        "sql": sql + ";",
        "database": "source",
        "object": rule.entity_name,
        "table": table,
        "note": _MEANING.get(rule.rule_type,
                             "One row per failing record."),
    }
