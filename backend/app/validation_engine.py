"""
validation_engine.py
--------------------
Executes a batch in THREE PHASES:

    PHASE 1  stage every entity        (one at a time)
    PHASE 2  validate every entity     (one at a time)
    PHASE 3  clear all staging

Nothing runs concurrently. The reason for the split is REFERENTIAL_INTEGRITY:
its SQL is a LEFT JOIN against the lookup entity's staging table, so that
table must still be populated when the referring entity is validated. Staging
one entity, validating it, and wiping it before starting the next made that
join impossible -- which is why the previous design had to fall back on a
snapshot table and always read the referenced entity's PREVIOUS run.

THE CORE PROPERTY IS UNCHANGED: Python loops over RULES (dozens), never over
data rows. Every rule -- including UNIQUENESS -- is ONE SQL statement the
database executes as a set operation. Only failing rows come back.

One commit per rule: if the process dies on rule 40 of 60, rules 1-39 are
durable.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import ingestion
from .database import ConfigSession
from .models import ENTITIES, ValMetric, ValRule, ValRun, ValViolation, staging_table_name
from .rule_compiler import (
    CompileContext, RuleCompileError, compile_rule, dimension_for, referenced_entity,
)

VIOLATION_BATCH_SIZE = 5000


def utcnow():
    return datetime.now(timezone.utc)


def rules_for_entity(db: Session, entity_name: str, rule_ids: Optional[list] = None):
    """
    The approval gate: only APPROVED and active rules ever execute.

    Rules live in CONFIG_DB while runs/metrics/violations live in TARGET_DB,
    so this opens its own config session rather than reusing the results one.
    """
    cdb = ConfigSession()
    try:
        return _load_rules(cdb, entity_name, rule_ids)
    finally:
        cdb.close()


def _load_rules(db: Session, entity_name: str, rule_ids: Optional[list] = None):
    q = db.query(ValRule).filter(
        ValRule.entity_name == entity_name,
        ValRule.status == "APPROVED",
        ValRule.active == True,  # noqa: E712
    )
    if rule_ids:
        q = q.filter(ValRule.rule_id.in_(rule_ids))
    return q.all()


# ---------------------------------------------------------------- PHASE 1 ---
def stage_run(db: Session, run_id: int, source_kind: str, file_path: Optional[str] = None) -> int:
    run = db.get(ValRun, run_id)
    entity = run.entity_name
    if entity not in ENTITIES:
        raise ValueError(f"Unknown entity {entity!r}")

    run.status = "running"
    run.started_at = utcnow()          # when execution begins, not when queued
    db.commit()

    if source_kind == "file_upload":
        n = ingestion.stage_from_csv(db, run_id, entity, file_path)
    elif source_kind == "db_fetch":
        n = ingestion.stage_from_db(db, run_id, entity, run.source_system)
    else:
        raise ValueError(f"Unknown source_kind: {source_kind}")

    run.records_scanned = n
    db.commit()
    return n


# ---------------------------------------------------------------- PHASE 2 ---
def validate_run(db: Session, run_id: int, staged_runs: dict, rule_ids: Optional[list] = None):
    """
    staged_runs maps entity_name -> run_id for every entity staged in THIS
    batch. REFERENTIAL_INTEGRITY uses it to locate its lookup table; if the
    referenced entity is not in the batch the rule fails loudly rather than
    silently checking against nothing.
    """
    run = db.get(ValRun, run_id)
    entity = run.entity_name
    meta = ENTITIES[entity]
    scanned = run.records_scanned or 0

    rules = rules_for_entity(db, entity, rule_ids)
    run.rules_executed = len(rules)
    db.commit()

    for rule in rules:
        ref_entity = referenced_entity(rule.rule_type, rule.rule_definition)
        ctx = CompileContext(
            table=staging_table_name(entity),
            columns=meta["columns"],
            lookup_table=staging_table_name(ref_entity) if ref_entity in staged_runs else None,
            lookup_run_id=staged_runs.get(ref_entity),
            lookup_key_field=(ENTITIES.get(ref_entity) or {}).get("primary_key_field"),
            lookup_columns=(ENTITIES.get(ref_entity) or {}).get("columns"),
        )
        try:
            compiled = compile_rule(rule.rule_type, rule.field_name, rule.rule_definition, ctx)
        except RuleCompileError as exc:
            # a broken rule must not take down the run -- record 0/0 and move on
            db.add(ValMetric(
                run_id=run_id, rule_id=rule.rule_id, entity_name=entity,
                field_name=rule.field_name or "-", dimension=dimension_for(rule.rule_type),
                severity=rule.severity, records_checked=scanned, records_failed=0,
                score_pct=0.0,
            ))
            db.commit()
            continue

        failed = _execute(db, run_id, rule, compiled)

        # AGGREGATION violations are GROUPS, not records, so the denominator
        # is the number of groups examined -- otherwise "1 breaching group"
        # reads as 1 bad record out of 101 and the score is meaningless.
        checked = scanned
        if compiled.group_level and compiled.denominator_sql:
            checked = db.execute(
                text(compiled.denominator_sql), {"run_id": run_id, **compiled.params}
            ).scalar() or 0

        score = 0.0 if checked == 0 else round((checked - failed) / checked * 100, 2)
        db.add(ValMetric(
            run_id=run_id, rule_id=rule.rule_id, entity_name=entity,
            field_name=rule.field_name or "-", dimension=dimension_for(rule.rule_type),
            severity=rule.severity, records_checked=checked, records_failed=failed,
            score_pct=score,
        ))
        db.commit()      # one commit per rule


def _execute(db: Session, run_id: int, rule: ValRule, compiled) -> int:
    """Run the rule's single SQL statement and bulk-write its violations."""
    params = {"run_id": run_id, **compiled.params}
    failed, batch = 0, []
    for row in db.execute(text(compiled.sql), params):
        batch.append({
            "run_id": run_id,
            "rule_id": rule.rule_id,
            "entity_name": rule.entity_name,
            "field_name": rule.field_name or "-",
            "record_key": row.record_key,
            "current_value": row.current_value,
            "violation_reason": rule.error_message or _default_reason(rule.rule_type, rule.field_name),
            "severity": rule.severity,
            "dimension": dimension_for(rule.rule_type),
        })
        failed += 1
        if len(batch) >= VIOLATION_BATCH_SIZE:
            db.bulk_insert_mappings(ValViolation, batch)
            batch = []
    if batch:
        db.bulk_insert_mappings(ValViolation, batch)
    return failed


def _default_reason(rule_type: str, col: Optional[str]) -> str:
    c = col or "record"
    return {
        "COMPLETENESS":          f"{c} is required but missing",
        "VALIDITY":              f"{c} does not match the expected format",
        "RANGE":                 f"{c} is outside the allowed range",
        "UNIQUENESS":            f"Duplicate value in {c}",
        "REFERENTIAL_INTEGRITY": f"{c} not found in the lookup entity",
        "AGGREGATION":           "Group breached the configured threshold",
        "ALLOWED_VALUES":        f"{c} is not in the list of allowed values",
        "CROSS_FIELD_SIMPLE":    f"{c} failed a cross-field rule",
        "CUSTOM_SQL":            f"{c} failed a custom rule",
    }.get(rule_type, f"{c} failed validation")


# ---------------------------------------------------------------- PHASE 3 ---
def clear_run_staging(db: Session, run_id: int):
    run = db.get(ValRun, run_id)
    ingestion.clear_staging(db, run.entity_name, run_id)


def finish_run(db: Session, run_id: int, error: Optional[str] = None):
    run = db.get(ValRun, run_id)
    run.status = "failed" if error else "completed"
    run.error_message = error[:2000] if error else None
    run.finished_at = utcnow()
    db.commit()
