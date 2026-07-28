"""Pydantic request/response models for the API."""

from datetime import datetime
from typing import Optional, Any
from pydantic import BaseModel


class ObjectOut(BaseModel):
    object_id: int
    object_name: str
    source_system: str
    record_key_column: str
    active_flag: bool

    class Config:
        orm_mode = True


class ElementOut(BaseModel):
    element_id: int
    object_id: int
    element_name: str
    source_column_name: str
    data_type: str
    active_flag: bool

    class Config:
        orm_mode = True


class RuleCreate(BaseModel):
    object_id: int
    element_id: int
    rule_name: str
    rule_type: str                 # required|allowed_values|format_pattern|max_length|unique|conditional_required
    dimension: str
    severity: str = "Warning"
    rule_config: dict[str, Any]     # e.g. {"values": ["US","IN"]} -- what the simple UI form collects
    created_by: Optional[str] = None


class RuleOut(BaseModel):
    rule_id: int
    object_id: int
    element_id: int
    rule_name: str
    rule_type: str
    dimension: str
    severity: str
    status: str
    created_by: Optional[str]
    created_at: datetime
    approved_by: Optional[str]
    approved_at: Optional[datetime]

    class Config:
        orm_mode = True


class RulePreviewOut(BaseModel):
    """Returned by the 'test this rule before approving' endpoint."""
    would_fail_count: int
    sample_failures: list[dict]


class RunTriggerFile(BaseModel):
    object_id: int
    run_name: Optional[str] = None


class RunTriggerDb(BaseModel):
    object_id: int
    run_name: Optional[str] = None
    connection_url: str
    query: str


class RunOut(BaseModel):
    run_id: int
    object_id: int
    run_name: Optional[str]
    run_type: str
    status: str
    started_at: datetime
    finished_at: Optional[datetime]
    records_scanned: int
    error_message: Optional[str]

    class Config:
        orm_mode = True


class ViolationOut(BaseModel):
    violation_id: int
    run_id: int
    element_id: int
    rule_id: int
    record_key: str
    current_value: Optional[str]
    violation_reason: Optional[str]
    severity: str
    dimension: str

    class Config:
        orm_mode = True
