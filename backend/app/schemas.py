"""Pydantic request/response models for the API."""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


# ---------------------------------------------------------------- entities ---
class EntityOut(BaseModel):
    """From the ENTITIES catalog constant, not a database table."""
    entity_name: str
    source_system: str
    source_object_name: str
    primary_key_field: str
    columns: list
    approved_rule_count: int = 0


class RuleTypeOut(BaseModel):
    code: str
    description: str
    dimension: str
    execution_type: str


# ------------------------------------------------------------------- rules ---
class RuleCreate(BaseModel):
    role: Optional[str] = "viewer"
    entity_name: str
    field_name: str
    rule_name: Optional[str] = None
    rule_type: str
    severity: str = "WARNING"
    rule_definition: dict = {}      # what the form collected -- source of truth
    error_message: Optional[str] = None
    created_by: Optional[str] = None


class RuleOut(BaseModel):
    rule_id: int
    rule_name: str
    source_system: str
    rule_type: str
    entity_name: str
    field_name: str
    primary_key_field: str
    execution_type: str
    dimension: Optional[str]
    rule_definition: Optional[str]
    error_message: Optional[str]
    severity: str
    status: str
    active: bool
    created_by: str
    created_date: datetime
    updated_by: Optional[str]
    updated_date: Optional[datetime]
    approved_by: Optional[str]
    approved_date: Optional[datetime]

    class Config:
        orm_mode = True


class RuleTransition(BaseModel):
    """
    Who is performing the transition. `role` is a PLACEHOLDER until Entra ID
    lands -- once SSO is in, the role comes from the token's `roles` claim and
    this field is ignored. Enforcement lives server-side either way.
    """
    actor: Optional[str] = None
    role: Optional[str] = "viewer"


class RulePreviewOut(BaseModel):
    would_fail_count: int
    sample_failures: list


# -------------------------------------------------------------------- runs ---
class BatchTriggerDb(BaseModel):
    """No connection string, no SQL -- the server already knows both."""
    entity_names: list
    source_system: Optional[str] = None
    batch_name: Optional[str] = None
    rule_ids: Optional[list] = None     # None/empty = all approved rules
    role: Optional[str] = "viewer"
    triggered_by: Optional[str] = None


class RunOut(BaseModel):
    run_id: int
    batch_id: int
    entity_name: str
    run_type: str
    status: str
    started_at: datetime
    finished_at: Optional[datetime]
    records_scanned: int
    total_records: Optional[int] = None
    phase: Optional[str] = None
    rules_total: int = 0
    rules_done: int = 0
    rules_executed: int
    source_file_name: Optional[str]
    error_message: Optional[str]

    class Config:
        orm_mode = True


class BatchOut(BaseModel):
    batch_id: int
    batch_name: Optional[str]
    run_type: str
    source_system: Optional[str] = None
    triggered_by: Optional[str]
    started_at: datetime
    status: str                 # DERIVED from child runs, never stored
    entity_count: int
    runs: list = []


class ViolationOut(BaseModel):
    violation_id: int
    run_id: int
    rule_id: int
    entity_name: str
    field_name: str
    record_key: str
    current_value: Optional[str]
    violation_reason: Optional[str]
    severity: str
    dimension: str

    class Config:
        orm_mode = True
