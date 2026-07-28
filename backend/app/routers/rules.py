"""
rules.py
--------
The 5-field rule authoring flow (Object, Field, Rule Type, Rule Config,
Severity). Everything else -- rule_config_json, condition_expr, dimension
defaults -- is generated here, never shown to or edited by the user.

Lifecycle: draft -> submitted -> approved (or rejected). Only 'approved'
rules are ever picked up by the validation engine.
"""

import json
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import DQRule, DQElement
from ..schemas import RuleCreate, RuleOut, RulePreviewOut
from ..rule_compiler import compile_rule, RuleCompileError, RULE_TYPES

router = APIRouter(prefix="/api/rules", tags=["rules"])

# rule_type -> default quality dimension (hidden from the user, derived automatically)
DIMENSION_BY_RULE_TYPE = {
    "required": "Completeness",
    "allowed_values": "Validity",
    "format_pattern": "Format",
    "max_length": "Format",
    "unique": "Uniqueness",
    "conditional_required": "Consistency",
}


def _build_rule_name(rule_type: str, element_name: str) -> str:
    label = rule_type.replace("_", " ").title()
    return f"{element_name} — {label}"


@router.get("", response_model=list[RuleOut])
def list_rules(object_id: Optional[int] = None, status: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(DQRule)
    if object_id is not None:
        q = q.filter(DQRule.object_id == object_id)
    if status is not None:
        q = q.filter(DQRule.status == status)
    return q.order_by(DQRule.created_at.desc()).all()


@router.post("", response_model=RuleOut)
def create_rule(payload: RuleCreate, db: Session = Depends(get_db)):
    if payload.rule_type not in RULE_TYPES:
        raise HTTPException(400, f"rule_type must be one of {RULE_TYPES}")

    element = db.get(DQElement, payload.element_id)
    if element is None:
        raise HTTPException(404, "element not found")

    config_json = json.dumps(payload.rule_config)
    try:
        compiled = compile_rule(payload.rule_type, element.source_column_name, config_json)
    except RuleCompileError as exc:
        raise HTTPException(400, f"Invalid rule configuration: {exc}")

    rule = DQRule(
        object_id=payload.object_id,
        element_id=payload.element_id,
        rule_name=payload.rule_name or _build_rule_name(payload.rule_type, element.element_name),
        rule_type=payload.rule_type,
        dimension=DIMENSION_BY_RULE_TYPE.get(payload.rule_type, "Validity"),
        severity=payload.severity,
        rule_config_json=config_json,
        condition_expr=compiled.condition_sql,
        status="draft",
        created_by=payload.created_by,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


@router.post("/{rule_id}/submit", response_model=RuleOut)
def submit_rule(rule_id: int, db: Session = Depends(get_db)):
    rule = _get_rule_or_404(db, rule_id)
    rule.status = "submitted"
    db.commit()
    db.refresh(rule)
    return rule


@router.post("/{rule_id}/approve", response_model=RuleOut)
def approve_rule(rule_id: int, approved_by: str = "reviewer", db: Session = Depends(get_db)):
    rule = _get_rule_or_404(db, rule_id)
    rule.status = "approved"
    rule.approved_by = approved_by
    rule.approved_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(rule)
    return rule


@router.post("/{rule_id}/reject", response_model=RuleOut)
def reject_rule(rule_id: int, db: Session = Depends(get_db)):
    rule = _get_rule_or_404(db, rule_id)
    rule.status = "rejected"
    db.commit()
    db.refresh(rule)
    return rule


@router.post("/{rule_id}/preview", response_model=RulePreviewOut)
def preview_rule(rule_id: int, run_id: int, db: Session = Depends(get_db)):
    """
    Runs a draft/submitted rule against an EXISTING run's staged data (or,
    more commonly in practice, the caller stages a small sample first) and
    returns a failure count + sample rows -- so a reviewer can see the real
    impact before approving, without trusting the config blindly.
    """
    rule = _get_rule_or_404(db, rule_id)
    element = db.get(DQElement, rule.element_id)
    try:
        compiled = compile_rule(rule.rule_type, element.source_column_name, rule.rule_config_json)
    except RuleCompileError as exc:
        raise HTTPException(400, str(exc))

    col = element.source_column_name
    if compiled.mode == "duplicate":
        sql = text(
            f"SELECT record_key, {col} AS current_value FROM stg_source_record "
            f"WHERE run_id = :run_id AND {col} IN ("
            f"  SELECT {col} FROM stg_source_record WHERE run_id = :run_id "
            f"  GROUP BY {col} HAVING COUNT(*) > 1"
            f")"
        )
        params = {"run_id": run_id}
    else:
        sql = text(
            f"SELECT record_key, {col} AS current_value FROM stg_source_record "
            f"WHERE run_id = :run_id AND ({compiled.condition_sql})"
        )
        params = {"run_id": run_id, **compiled.params}

    rows = db.execute(sql, params).fetchall()
    sample = [{"record_key": r.record_key, "current_value": r.current_value} for r in rows[:10]]
    return RulePreviewOut(would_fail_count=len(rows), sample_failures=sample)


def _get_rule_or_404(db: Session, rule_id: int) -> DQRule:
    rule = db.get(DQRule, rule_id)
    if rule is None:
        raise HTTPException(404, "rule not found")
    return rule
