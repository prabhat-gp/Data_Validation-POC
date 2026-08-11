"""
entities.py
-----------
Serves the entity/column catalog that the removed dq_object + dq_element
tables used to provide.

It reads the ENTITIES constant, not a database table. That is deliberate: the
Rules page needs a field list BEFORE any rule exists (you cannot derive the
catalog from rules you haven't written yet) and before any file is uploaded.
Serving a known list also means a rule can never be authored against a
misspelt column -- a typo would compile into valid SQL that silently matches
zero rows forever.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_config_db as get_db
from ..models import ENTITIES, ValRule
from ..rule_compiler import RULE_TYPE_DESCRIPTIONS, RULE_TYPE_META, RULE_TYPES
from ..schemas import EntityOut, RuleTypeOut

router = APIRouter(prefix="/api/entities", tags=["entities"])


@router.get("", response_model=list)
def list_entities(db: Session = Depends(get_db)):
    """
    Every entity that can be validated, with its approved-rule count so the
    Runs page can grey out entities that have nothing to run.
    """
    counts = dict(
        db.query(ValRule.entity_name, func.count(ValRule.rule_id))
        .filter(ValRule.status == "approved", ValRule.active == True)  # noqa: E712
        .group_by(ValRule.entity_name)
        .all()
    )
    return [
        EntityOut(
            entity_name=name,
            source_system=meta["source_system"],
            source_object_name=meta["source_object_name"],
            primary_key_field=meta["primary_key_field"],
            columns=meta["columns"],
            approved_rule_count=counts.get(name, 0),
        )
        for name, meta in ENTITIES.items()
    ]


@router.get("/{entity_name}/columns", response_model=list)
def list_columns(entity_name: str):
    """The field dropdown on the Rules page."""
    meta = ENTITIES.get(entity_name)
    if meta is None:
        raise HTTPException(404, f"Unknown object: {entity_name}")
    return meta["columns"]


@router.get("/meta/rule-types", response_model=list)
def list_rule_types():
    """
    The 8 supported rule types. dimension and execution_type are DERIVED here
    and shown read-only in the UI -- a user must never be able to mark a
    'required' rule as RECORD execution and send it down the wrong engine path.
    """
    return [
        RuleTypeOut(
            code=code,
            description=RULE_TYPE_DESCRIPTIONS.get(code, code),
            dimension=RULE_TYPE_META[code][0],
            execution_type=RULE_TYPE_META[code][1],
        )
        for code in RULE_TYPES
    ]
