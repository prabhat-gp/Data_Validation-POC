"""
validation_engine.py
---------------------
The run orchestrator. This is the piece responsible for scaling to millions
of rows without breaking:

  1. Every rule compiles to ONE SQL statement (see rule_compiler.py) that the
     DATABASE executes as a set operation. Python never loops over data rows
     -- it only loops over RULES (dozens, not millions) and over RESULT rows
     of a failing query, which it writes back in fixed-size batches.
  2. Violations are written with bulk_insert_mappings in chunks, one commit
     per rule (not one commit per row, not one giant transaction for the
     whole run). If the process dies on rule 40 of 60, rules 1-39's results
     are already durably committed.
  3. This function is designed to be called from a background task, not
     inline in an HTTP request -- see routers/runs.py.
  4. A run is only ever marked 'completed' after every approved rule has
     executed successfully and metrics are written; any exception marks it
     'failed' with error_message set, so a broken run is visible, not silent.
"""

import json
import time
import traceback
from typing import Optional
from sqlalchemy import text
from sqlalchemy.orm import Session

from .models import DQRun, DQRule, DQElement, DQObject, DQViolation, DQMetric
from .rule_compiler import compile_rule, RuleCompileError
from . import ingestion

VIOLATION_BATCH_SIZE = 5000


def _run_predicate_rule(db: Session, run_id: int, rule: DQRule, element: DQElement, compiled):
    """Query-centric rules: one SELECT for the failing rows, one bulk insert."""
    col = element.source_column_name
    sql = text(
        f"SELECT record_key, {col} AS current_value FROM stg_source_record "
        f"WHERE run_id = :run_id AND ({compiled.condition_sql})"
    )
    params = {"run_id": run_id, **compiled.params}
    result = db.execute(sql, params)

    failed = 0
    batch = []
    for row in result:
        batch.append({
            "run_id": run_id,
            "object_id": rule.object_id,
            "element_id": rule.element_id,
            "rule_id": rule.rule_id,
            "record_key": row.record_key,
            "current_value": row.current_value,
            "violation_reason": _reason_for(rule.rule_type, col),
            "severity": rule.severity,
            "dimension": rule.dimension,
        })
        failed += 1
        if len(batch) >= VIOLATION_BATCH_SIZE:
            db.bulk_insert_mappings(DQViolation, batch)
            batch = []
    if batch:
        db.bulk_insert_mappings(DQViolation, batch)
    return failed


def _run_duplicate_rule(db: Session, run_id: int, rule: DQRule, element: DQElement, compiled):
    """
    Unique rule -- plain SQL GROUP BY/HAVING, same execution path as every
    other rule (per direction: no separate record-centric machinery).
    """
    col = compiled.condition_sql  # column name, validated in the compiler
    dup_values_sql = text(
        f"SELECT {col} AS v FROM stg_source_record "
        f"WHERE run_id = :run_id AND {col} IS NOT NULL AND TRIM({col}) <> '' "
        f"GROUP BY {col} HAVING COUNT(*) > 1"
    )
    dup_values = [row.v for row in db.execute(dup_values_sql, {"run_id": run_id})]
    if not dup_values:
        return 0

    failed = 0
    batch = []
    # fetch every record sharing a duplicated value, chunked so a very wide
    # duplicate set can't build one unbounded IN(...) clause
    CHUNK = 500
    for i in range(0, len(dup_values), CHUNK):
        chunk = dup_values[i:i + CHUNK]
        placeholders = ", ".join(f":v{i}" for i in range(len(chunk)))
        params = {f"v{i}": v for i, v in enumerate(chunk)}
        params["run_id"] = run_id
        rows_sql = text(
            f"SELECT record_key, {col} AS current_value FROM stg_source_record "
            f"WHERE run_id = :run_id AND {col} IN ({placeholders})"
        )
        for row in db.execute(rows_sql, params):
            batch.append({
                "run_id": run_id,
                "object_id": rule.object_id,
                "element_id": rule.element_id,
                "rule_id": rule.rule_id,
                "record_key": row.record_key,
                "current_value": row.current_value,
                "violation_reason": f"Duplicate value in {element.element_name}",
                "severity": rule.severity,
                "dimension": rule.dimension,
            })
            failed += 1
            if len(batch) >= VIOLATION_BATCH_SIZE:
                db.bulk_insert_mappings(DQViolation, batch)
                batch = []
    if batch:
        db.bulk_insert_mappings(DQViolation, batch)
    return failed


def _reason_for(rule_type: str, col: str) -> str:
    return {
        "required": f"{col} is required but missing",
        "allowed_values": f"{col} value is not in the allowed list",
        "format_pattern": f"{col} does not match the expected format",
        "max_length": f"{col} exceeds the maximum allowed length",
        "conditional_required": f"{col} is required given another field's value",
    }.get(rule_type, f"{col} failed validation")


def run_validation(
    db: Session,
    run_id: int,
    source_kind: str,               # "file_upload" | "db_fetch"
    file_path: Optional[str] = None,
    db_source_url: Optional[str] = None,
    db_source_query: Optional[str] = None,
):
    """
    Entry point called from a background task (see routers/runs.py). Owns
    the whole lifecycle of one run: stage -> validate -> metrics -> cleanup.
    """
    run = db.get(DQRun, run_id)
    obj = db.get(DQObject, run.object_id)
    try:
        # 1. Stage
        if source_kind == "file_upload":
            records_scanned = ingestion.stage_from_csv(db, run_id, file_path, obj.record_key_column)
        elif source_kind == "db_fetch":
            records_scanned = ingestion.stage_from_db(
                db, run_id, db_source_url, db_source_query, obj.record_key_column
            )
        else:
            raise ValueError(f"Unknown source_kind: {source_kind}")

        run.records_scanned = records_scanned
        db.commit()

        # 2. Load only APPROVED rules -- this is the structural approval gate
        rules = (
            db.query(DQRule)
            .filter(DQRule.object_id == run.object_id, DQRule.status == "approved")
            .all()
        )

        # 3. Execute each rule as its own transaction (see module docstring)
        for rule in rules:
            element = db.get(DQElement, rule.element_id)
            try:
                compiled = compile_rule(rule.rule_type, element.source_column_name, rule.rule_config_json)
            except RuleCompileError:
                # a malformed rule shouldn't take down the whole run
                continue

            if compiled.mode == "duplicate":
                failed = _run_duplicate_rule(db, run_id, rule, element, compiled)
            else:
                failed = _run_predicate_rule(db, run_id, rule, element, compiled)

            score_pct = 0.0 if records_scanned == 0 else round((records_scanned - failed) / records_scanned * 100, 2)
            db.add(DQMetric(
                run_id=run_id,
                object_id=rule.object_id,
                element_id=rule.element_id,
                rule_id=rule.rule_id,
                dimension=rule.dimension,
                severity=rule.severity,
                records_checked=records_scanned,
                records_failed=failed,
                score_pct=score_pct,
            ))
            db.commit()   # one commit per rule -- partial progress survives a crash

        # 4. Staging is runtime-only -- clear it now that metrics are written
        ingestion.clear_staging(db, run_id)

        run.status = "completed"
        from datetime import datetime, timezone
        run.finished_at = datetime.now(timezone.utc)
        db.commit()

    except Exception as exc:
        db.rollback()
        run = db.get(DQRun, run_id)
        run.status = "failed"
        run.error_message = f"{exc}\n{traceback.format_exc()[-2000:]}"
        from datetime import datetime, timezone
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        raise
