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

import json
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import ingestion
from .database import ConfigSession, StagingSession
from .models import (
    ENTITIES, MULTI_FIELD_SEP, ValMetric, ValRule, ValRun, ValViolation,
    staging_table_name,
)
from .rule_compiler import (
    CompileContext, RuleCompileError, compile_rule, dimension_for, referenced_entity,
)


def _dimension(rule) -> str:
    """
    The dimension this rule's metrics are filed under.

    Read from the rule, because the author may have reclassified it away from
    the rule type's default. Falls back to the default for rules created
    before dimension was stored.
    """
    return rule.dimension or dimension_for(rule.rule_type)


def _display_field(rule) -> str:
    """
    What the drilldown shows in the Element column. A multi-field UNIQUENESS
    rule has no single field_name, so fall back to the combination it checks
    rather than rendering a bare dash.
    """
    if rule.field_name:
        return rule.field_name
    try:
        cfg = json.loads(rule.rule_definition or "{}")
    except Exception:  # noqa: BLE001
        cfg = {}
    for key in ("fields", "groupBy"):
        if cfg.get(key):
            return MULTI_FIELD_SEP.join(cfg[key])
    return rule.rule_name or "-"


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
        raise ValueError(f"Unknown object {entity!r}")

    run.status = "running"
    run.phase = "staging"
    run.started_at = utcnow()          # when execution begins, not when queued
    run.records_scanned = 0
    db.commit()

    # progress callbacks -- committed as staging streams so the UI can poll a
    # real percentage instead of showing an indeterminate spinner. One extra
    # UPDATE per 5,000 rows is negligible next to the inserts themselves.
    def set_total(n):
        run.total_records = n
        db.commit()

    def bump(n):
        run.records_scanned = n
        db.commit()

    # Rows go into stg_* in SOURCE_DB; run progress stays on `db` (results_db).
    sdb = StagingSession()
    try:
        if source_kind == "file_upload":
            n = ingestion.stage_from_csv(sdb, run_id, entity, file_path,
                                         on_progress=bump, on_total=set_total)
        elif source_kind == "db_fetch":
            n = ingestion.stage_from_db(sdb, run_id, entity, run.source_system,
                                        on_progress=bump, on_total=set_total)
        else:
            raise ValueError(f"Unknown source_kind: {source_kind}")
    finally:
        sdb.close()

    run.records_scanned = n
    run.total_records = n
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

    sdb = StagingSession()
    rules = rules_for_entity(db, entity, rule_ids)
    run.rules_executed = len(rules)
    run.rules_total = len(rules)
    run.rules_done = 0
    run.phase = "validating"
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
                field_name=_display_field(rule), dimension=_dimension(rule),
                severity=rule.severity, records_checked=scanned, records_failed=0,
                score_pct=0.0,
            ))
            db.commit()
            continue

        failed = _execute(sdb, db, run_id, rule, compiled)

        # AGGREGATION violations are GROUPS, not records, so the denominator
        # is the number of groups examined -- otherwise "1 breaching group"
        # reads as 1 bad record out of 101 and the score is meaningless.
        checked = scanned
        if compiled.group_level and compiled.denominator_sql:
            checked = sdb.execute(
                text(compiled.denominator_sql), {"run_id": run_id, **compiled.params}
            ).scalar() or 0

        score = 0.0 if checked == 0 else round((checked - failed) / checked * 100, 2)
        db.add(ValMetric(
            run_id=run_id, rule_id=rule.rule_id, entity_name=entity,
            field_name=_display_field(rule), dimension=_dimension(rule),
            severity=rule.severity, records_checked=checked, records_failed=failed,
            score_pct=score,
        ))
        run.rules_done = (run.rules_done or 0) + 1
        db.commit()      # one commit per rule, progress included

    sdb.close()


def _execute(sdb: Session, db: Session, run_id: int, rule: ValRule, compiled) -> int:
    """
    Run the rule's single SQL statement and bulk-write its violations.

    sdb -- staging session (source_db). The compiled SQL selects from stg_*
           and, for referential integrity, joins another stg_* table.
    db  -- results session (results_db). Violations are written here.
    Two sessions because the two sides now live in different databases.
    """
    params = {"run_id": run_id, **compiled.params}
    failed, batch = 0, []
    for row in sdb.execute(text(compiled.sql), params):
        batch.append({
            "run_id": run_id,
            "rule_id": rule.rule_id,
            "entity_name": rule.entity_name,
            "field_name": _display_field(rule),
            "record_key": row.record_key,
            "current_value": row.current_value,
            "violation_reason": rule.error_message or _default_reason(rule.rule_type, rule.field_name),
            "severity": rule.severity,
            "dimension": _dimension(rule),
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
    """Staging lives in source_db, so this needs its own session."""
    run = db.get(ValRun, run_id)
    sdb = StagingSession()
    try:
        ingestion.clear_staging(sdb, run.entity_name, run_id)
    finally:
        sdb.close()


def finish_run(db: Session, run_id: int, error: Optional[str] = None):
    run = db.get(ValRun, run_id)
    run.phase = "done"
    run.status = "failed" if error else "completed"
    # How many RECORDS are affected, as distinct from how many CHECKS failed.
    # One row failing three rules is 3 failed checks but 1 bad record, and the
    # second number is the one a reviewer actually means by "how bad is it".
    if not error:
        run.records_affected = db.execute(
            text("SELECT COUNT(DISTINCT record_key) FROM val_violations WHERE run_id = :r"),
            {"r": run_id},
        ).scalar() or 0
    run.error_message = error[:2000] if error else None
    run.finished_at = utcnow()
    db.commit()
