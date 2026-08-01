"""
rules.py
--------
Rule authoring against the single val_rules table.

The user supplies five things: Entity, Field, Rule Type, Rule Config,
Severity. Everything else -- rule_id, rule_name, source_system,
primary_key_field, execution_type, dimension -- is DERIVED here. None of it
is accepted from the client, because several of those values change how the
engine executes and must not be forgeable.

Lifecycle: draft -> submitted -> approved (or rejected). Only 'approved' AND
'active' rules are ever loaded by the validation engine, which is the
structural approval gate.
"""

import json
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import ENTITIES, ValRule
from ..rule_compiler import (
    RULE_TYPES, RuleCompileError, compile_rule, dimension_for, execution_type_for,
)
from ..schemas import RuleCreate, RuleOut, RuleTransition

router = APIRouter(prefix="/api/rules", tags=["rules"])


def utcnow():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# ROLES
# ---------------------------------------------------------------------------
# PLACEHOLDER until Entra ID lands. Today the role arrives in the request body;
# once SSO is in it comes from the token's `roles` claim and the body field is
# ignored. Either way enforcement stays HERE, server-side -- hiding a button in
# the UI is a convenience, never a control.
#
#   viewer -> dashboard only
#   owner  -> + author rules, trigger runs
#   admin  -> + approve / reject rules
ROLE_RANK = {"viewer": 0, "owner": 1, "admin": 2}

# Separation of duties -- OFF by default so a single developer/admin can author
# and approve their own rules while building. Set
#     REQUIRE_SEPARATE_APPROVER=true
# in the deployed environment to enforce "an author may not approve their own
# rule", which is what makes an approval trail mean anything to an auditor.
# The code path and the approved_by/approved_date columns exist either way, so
# turning it on is a config change, not a rebuild.
REQUIRE_SEPARATE_APPROVER = os.getenv("REQUIRE_SEPARATE_APPROVER", "false").lower() == "true"


def require_role(role: str, minimum: str):
    if ROLE_RANK.get((role or "viewer").lower(), 0) < ROLE_RANK[minimum]:
        raise HTTPException(
            403,
            f"This action requires the '{minimum}' role. You are signed in as "
            f"'{role or 'viewer'}'.",
        )


@router.get("", response_model=list)
def list_rules(
    entity_name: Optional[str] = None,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
):
    q = db.query(ValRule)
    if entity_name:
        q = q.filter(ValRule.entity_name == entity_name)
    if status:
        q = q.filter(ValRule.status == status)
    return q.order_by(ValRule.created_date.desc()).all()


@router.post("", response_model=RuleOut)
def create_rule(payload: RuleCreate, db: Session = Depends(get_db)):
    require_role(payload.role, "owner")
    meta = ENTITIES.get(payload.entity_name)
    if meta is None:
        raise HTTPException(400, f"Unknown entity: {payload.entity_name}")
    if payload.field_name not in meta["columns"]:
        raise HTTPException(
            400,
            f"'{payload.field_name}' is not a known column on {payload.entity_name}. "
            f"Known columns: {meta['columns']}",
        )
    if payload.rule_type not in RULE_TYPES:
        raise HTTPException(400, f"Unknown rule_type. Supported: {RULE_TYPES}")

    definition_json = json.dumps(payload.rule_definition or {})

    # Compile now purely to validate the config. The SQL is deliberately NOT
    # stored -- it is regenerated at run time so it can never drift from the
    # definition. Failing here means a broken rule can't reach 'draft'.
    try:
        compile_rule(payload.rule_type, payload.field_name, definition_json)
    except RuleCompileError as exc:
        raise HTTPException(400, f"Invalid rule configuration: {exc}")

    rule = ValRule(
        rule_name=payload.rule_name or f"{payload.field_name} — {payload.rule_type.replace('_', ' ').title()}",
        source_system=meta["source_system"],
        rule_type=payload.rule_type,
        entity_name=payload.entity_name,
        field_name=payload.field_name,
        primary_key_field=meta["primary_key_field"],
        execution_type=execution_type_for(payload.rule_type),   # derived, never user input
        rule_definition=definition_json,
        error_message=payload.error_message,
        severity=payload.severity,
        status="draft",
        active=True,
        created_by=payload.created_by or "system",
        created_date=utcnow(),
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


def _get_rule(db: Session, rule_id: int) -> ValRule:
    rule = db.get(ValRule, rule_id)
    if rule is None:
        raise HTTPException(404, "rule not found")
    return rule


@router.post("/{rule_id}/submit", response_model=RuleOut)
def submit_rule(rule_id: int, payload: RuleTransition = RuleTransition(), db: Session = Depends(get_db)):
    require_role(payload.role, "owner")
    rule = _get_rule(db, rule_id)
    if rule.status not in ("draft", "rejected"):
        raise HTTPException(400, f"cannot submit a rule in status '{rule.status}'")
    rule.status = "submitted"
    rule.updated_by = payload.actor or rule.created_by
    rule.updated_date = utcnow()
    db.commit()
    db.refresh(rule)
    return rule


@router.post("/{rule_id}/approve", response_model=RuleOut)
def approve_rule(rule_id: int, payload: RuleTransition = RuleTransition(), db: Session = Depends(get_db)):
    """
    Approving records WHO approved and WHEN, separately from updated_by /
    updated_date (which change on any edit and so cannot serve as an approval
    record).

    Separation of duties -- "an author may not approve their own rule" -- is
    gated behind REQUIRE_SEPARATE_APPROVER so one person can author and approve
    while building. Turn it on in the deployed environment.
    """
    require_role(payload.role, "admin")
    rule = _get_rule(db, rule_id)
    if rule.status != "submitted":
        raise HTTPException(400, f"only submitted rules can be approved (this one is '{rule.status}')")

    actor = payload.actor
    if (
        REQUIRE_SEPARATE_APPROVER
        and actor and rule.created_by
        and actor.strip().lower() == rule.created_by.strip().lower()
    ):
        raise HTTPException(
            403,
            "You cannot approve a rule you authored. Approval must come from a "
            "different person (separation of duties is enabled on this environment).",
        )

    rule.status = "approved"
    rule.approved_by = actor or "system"
    rule.approved_date = utcnow()
    rule.updated_by = actor or "system"
    rule.updated_date = utcnow()
    db.commit()
    db.refresh(rule)
    return rule


@router.post("/{rule_id}/reject", response_model=RuleOut)
def reject_rule(rule_id: int, payload: RuleTransition = RuleTransition(), db: Session = Depends(get_db)):
    require_role(payload.role, "admin")
    rule = _get_rule(db, rule_id)
    if rule.status != "submitted":
        raise HTTPException(400, f"only submitted rules can be rejected (this one is '{rule.status}')")
    rule.status = "rejected"
    rule.updated_by = payload.actor or "system"
    rule.updated_date = utcnow()
    db.commit()
    db.refresh(rule)
    return rule


@router.delete("/{rule_id}")
def delete_rule(rule_id: int, db: Session = Depends(get_db)):
    rule = _get_rule(db, rule_id)
    db.delete(rule)
    db.commit()
    return {"deleted": rule_id}


@router.get("/{rule_id}/sql")
def show_sql(rule_id: int, db: Session = Depends(get_db)):
    """
    Debug aid: the SQL this rule compiles to right now. Useful when explaining
    to a reviewer what a rule actually does.
    """
    rule = _get_rule(db, rule_id)
    try:
        compiled = compile_rule(rule.rule_type, rule.field_name, rule.rule_definition)
    except RuleCompileError as exc:
        raise HTTPException(400, str(exc))
    return {
        "rule_id": rule.rule_id,
        "execution_type": rule.execution_type,
        "mode": compiled.mode,
        "condition_sql": compiled.condition_sql,
        "filter_sql": compiled.filter_sql,
        "dimension": dimension_for(rule.rule_type),
    }
