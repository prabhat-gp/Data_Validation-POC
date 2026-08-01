"""
validation_engine.py
--------------------
Executes one entity's validation run.

THE CORE PROPERTY: Python loops over RULES (dozens), never over data rows
(millions). Each rule becomes ONE SQL statement that the DATABASE executes as
a set operation. Only failing rows come back, and they are written out in
bulk batches.

Transaction shape: one commit per rule. If the process dies on rule 40 of 60,
rules 1-39 are already durable -- partial progress survives a crash. Any
exception marks the run 'failed' with an error_message so a broken run is
visible rather than silently half-done.
"""

import json
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import ingestion
from .models import (
    ENTITIES, ValMetric, ValReferenceValue, ValRule, ValRun, ValViolation,
    staging_table_name,
)
from .rule_compiler import (
    RuleCompileError, assert_safe_identifier, compile_rule, dimension_for,
)

VIOLATION_BATCH_SIZE = 5000
DUP_VALUE_CHUNK = 500


def utcnow():
    return datetime.now(timezone.utc)


def _combine(condition_sql: str, filter_sql: Optional[str]) -> str:
    """Scope filter AND failing-row condition."""
    return f"({filter_sql} AND {condition_sql})" if filter_sql else f"({condition_sql})"


def _run_predicate_rule(db: Session, run_id: int, rule: ValRule, compiled) -> int:
    """Query-centric rules: one SELECT for the failing rows, bulk-insert them."""
    col = assert_safe_identifier(rule.field_name)
    table = staging_table_name(rule.entity_name)
    where = _combine(compiled.condition_sql, compiled.filter_sql)

    sql = text(
        f"SELECT record_key, {col} AS current_value FROM {table} "
        f"WHERE run_id = :run_id AND {where}"
    )
    params = {"run_id": run_id, **compiled.params, **compiled.filter_params}

    failed, batch = 0, []
    for row in db.execute(sql, params):
        batch.append(_violation_row(run_id, rule, row.record_key, row.current_value))
        failed += 1
        if len(batch) >= VIOLATION_BATCH_SIZE:
            db.bulk_insert_mappings(ValViolation, batch)
            batch = []
    if batch:
        db.bulk_insert_mappings(ValViolation, batch)
    return failed


def _run_duplicate_rule(db: Session, run_id: int, rule: ValRule, compiled) -> int:
    """
    Unique -- plain SQL GROUP BY/HAVING, same execution path as every other
    rule (no separate record-centric machinery). Two passes, because you
    cannot tell whether a value is duplicated by looking at one row:

      pass 1: which VALUES occur more than once  (one scan, in the database)
      pass 2: which ROWS carry those values      (so both sides get reported)

    The scope filter MUST be applied to BOTH passes. Applying it only to pass
    1 would find duplicates within the filtered subset, then report every row
    sharing that value -- including rows outside the filter. Silent false
    positives, very hard to spot in a dashboard.
    """
    col = assert_safe_identifier(rule.field_name)
    table = staging_table_name(rule.entity_name)
    fsql, fparams = compiled.filter_sql, compiled.filter_params
    filter_clause = f" AND {fsql}" if fsql else ""

    dup_values = [
        r.v for r in db.execute(
            text(
                f"SELECT {col} AS v FROM {table} "
                f"WHERE run_id = :run_id{filter_clause} "
                f"AND {col} IS NOT NULL AND TRIM({col}) <> '' "
                f"GROUP BY {col} HAVING COUNT(*) > 1"
            ),
            {"run_id": run_id, **fparams},
        )
    ]
    if not dup_values:
        return 0

    failed, batch = 0, []
    # chunked so a very wide duplicate set can't build one unbounded IN(...)
    for i in range(0, len(dup_values), DUP_VALUE_CHUNK):
        chunk = dup_values[i:i + DUP_VALUE_CHUNK]
        placeholders = ", ".join(f":v{j}" for j in range(len(chunk)))
        params = {f"v{j}": v for j, v in enumerate(chunk)}
        params.update({"run_id": run_id, **fparams})
        rows_sql = text(
            f"SELECT record_key, {col} AS current_value FROM {table} "
            f"WHERE run_id = :run_id{filter_clause} AND {col} IN ({placeholders})"
        )
        for row in db.execute(rows_sql, params):
            batch.append(_violation_row(run_id, rule, row.record_key, row.current_value))
            failed += 1
            if len(batch) >= VIOLATION_BATCH_SIZE:
                db.bulk_insert_mappings(ValViolation, batch)
                batch = []
    if batch:
        db.bulk_insert_mappings(ValViolation, batch)
    return failed


def _violation_row(run_id: int, rule: ValRule, record_key, current_value) -> dict:
    return {
        "run_id": run_id,
        "rule_id": rule.rule_id,
        "entity_name": rule.entity_name,
        "field_name": rule.field_name,
        "record_key": record_key,
        "current_value": current_value,
        "violation_reason": rule.error_message or _default_reason(rule.rule_type, rule.field_name),
        "severity": rule.severity,
        "dimension": dimension_for(rule.rule_type),
    }


def _default_reason(rule_type: str, col: str) -> str:
    return {
        "required": f"{col} is required but missing",
        "allowed_values": f"{col} value is not in the allowed list",
        "format_pattern": f"{col} does not match the expected format",
        "max_length": f"{col} exceeds the maximum allowed length",
        "unique": f"Duplicate value in {col}",
        "conditional_required": f"{col} is required given another field's value",
        "ref_integrity": f"{col} does not exist in the referenced entity",
        "multi_condition": f"{col} failed a multi-condition business rule",
    }.get(rule_type, f"{col} failed validation")


def _refresh_reference_values(db: Session, entity_name: str, run_id: int):
    """
    Runs after this entity's rules have executed, BEFORE staging is cleared.
    Refreshes val_reference_values ONLY for fields that some approved
    ref_integrity rule actually targets -- bounded work, not "capture every
    CDE on every run just in case".
    """
    ref_rules = (
        db.query(ValRule)
        .filter(ValRule.rule_type == "ref_integrity", ValRule.status == "approved",
                ValRule.active == True)  # noqa: E712
        .all()
    )
    needed = set()
    for r in ref_rules:
        cfg = json.loads(r.rule_definition) if r.rule_definition else {}
        if cfg.get("ref_entity_name") == entity_name and cfg.get("ref_field_name"):
            needed.add(cfg["ref_field_name"])

    if not needed:
        return
    table = staging_table_name(entity_name)
    for field_name in needed:
        col = assert_safe_identifier(field_name)
        db.execute(
            text("DELETE FROM val_reference_values WHERE entity_name = :e AND field_name = :f"),
            {"e": entity_name, "f": field_name},
        )
        db.execute(
            text(
                f"INSERT INTO val_reference_values (entity_name, field_name, value) "
                f"SELECT :e, :f, {col} FROM {table} "
                f"WHERE run_id = :run_id AND {col} IS NOT NULL AND TRIM({col}) <> '' "
                f"GROUP BY {col}"
            ),
            {"e": entity_name, "f": field_name, "run_id": run_id},
        )
    db.commit()


def run_validation(
    db: Session,
    run_id: int,
    source_kind: str,                      # "file_upload" | "db_fetch"
    file_path: Optional[str] = None,
    rule_ids: Optional[list] = None,       # None/empty = all approved rules
):
    """
    Owns the whole lifecycle of ONE entity's run:
        stage -> validate -> metrics -> refresh refs -> clear staging.
    Called from a background task (see routers/runs.py), never inline in a
    request -- a multi-minute job would otherwise blow the HTTP timeout.
    """
    run = db.get(ValRun, run_id)
    entity = run.entity_name
    meta = ENTITIES.get(entity)
    if meta is None:
        raise ValueError(f"Unknown entity {entity!r}")

    try:
        # started_at is stamped HERE, when execution actually begins -- not when
        # the row was queued at trigger time. Otherwise every run in a batch
        # shares a timestamp and the progress display is meaningless.
        run.status = "running"
        run.started_at = utcnow()
        db.commit()

        # 1. Which rules are we running? The approval gate lives here: only
        #    approved + active rules are ever loaded.
        q = db.query(ValRule).filter(
            ValRule.entity_name == entity,
            ValRule.status == "approved",
            ValRule.active == True,  # noqa: E712
        )
        if rule_ids:
            q = q.filter(ValRule.rule_id.in_(rule_ids))
        rules = q.all()

        # 2. Stage. Only the columns the selected rules actually reference are
        #    kept -- a 443-column export is pruned to a handful.
        if source_kind == "file_upload":
            records_scanned = ingestion.stage_from_csv(db, run_id, entity, file_path)
        elif source_kind == "db_fetch":
            records_scanned = ingestion.stage_from_db(db, run_id, entity)
        else:
            raise ValueError(f"Unknown source_kind: {source_kind}")

        run.records_scanned = records_scanned
        run.rules_executed = len(rules)
        db.commit()

        # 3. Execute each rule as its own transaction
        for rule in rules:
            try:
                compiled = compile_rule(rule.rule_type, rule.field_name, rule.rule_definition)
            except RuleCompileError:
                continue  # a malformed rule must not take down the whole run

            if compiled.mode == "duplicate":
                failed = _run_duplicate_rule(db, run_id, rule, compiled)
            else:
                failed = _run_predicate_rule(db, run_id, rule, compiled)

            score = 0.0 if records_scanned == 0 else round(
                (records_scanned - failed) / records_scanned * 100, 2
            )
            db.add(ValMetric(
                run_id=run_id,
                rule_id=rule.rule_id,
                entity_name=entity,
                field_name=rule.field_name,
                dimension=dimension_for(rule.rule_type),
                severity=rule.severity,
                records_checked=records_scanned,
                records_failed=failed,
                score_pct=score,
            ))
            db.commit()   # one commit per rule -- partial progress survives a crash

        # 4. Capture reference values other entities' ref_integrity rules need.
        #    MUST precede step 5 -- staging is the only place these come from.
        _refresh_reference_values(db, entity, run_id)

        # 5. Staging is runtime-only, never a permanent store.
        ingestion.clear_staging(db, entity, run_id)

        run.status = "completed"
        run.finished_at = utcnow()
        db.commit()

    except Exception as exc:  # noqa: BLE001 -- a failed run must be visible, not silent
        db.rollback()
        run = db.get(ValRun, run_id)
        run.status = "failed"
        run.error_message = str(exc)[:2000]
        run.finished_at = utcnow()
        db.commit()
        try:
            ingestion.clear_staging(db, entity, run_id)
        except Exception:  # noqa: BLE001
            pass
        raise
