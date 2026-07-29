"""
models.py
---------
The 7-table V1 schema. See the schema-design writeup shared with the team for
the reasoning behind every column. This file only CREATES structure -- no
DQ_RULE rows are ever seeded from code. Rules are entered by users through
the UI and reviewed/approved there.

CDE_COLUMNS is the fixed, confirmed set of 16 Critical Data Elements for the
Account object (cross-checked against the Onity CCEX list). STG_SOURCE_RECORD
has one physical column per CDE because V1 has exactly one object in scope;
if a second object is added later this table (or an equivalent per-object
staging table) gets generated the same way.
"""

from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime, ForeignKey, Float, UniqueConstraint, Index
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


def utcnow():
    return datetime.now(timezone.utc)


# The 16 confirmed Account CDEs -- single source of truth for staging columns,
# rule-compiler validation (identifier whitelist), and seed data.
CDE_COLUMNS = [
    "BillingCity", "BillingCountry", "BillingPostalCode", "BillingState", "BillingStreet",
    "Industry", "Name", "Phone",
    "ShippingCity", "ShippingCountry", "ShippingPostalCode", "ShippingState", "ShippingStreet",
    "Type", "Website", "Region__c",
]


class DQObject(Base):
    __tablename__ = "dq_object"

    object_id = Column(Integer, primary_key=True)
    object_name = Column(String(120), nullable=False)          # shown in UI, e.g. "Account"
    source_system = Column(String(60), nullable=False)          # e.g. "SFDC" -- dashboard top bar
    source_object_name = Column(String(120), nullable=False)    # actual object name at source
    record_key_column = Column(String(120), nullable=False)     # which source column is the unique id ("Id")
    active_flag = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=utcnow)


class DQElement(Base):
    __tablename__ = "dq_element"

    element_id = Column(Integer, primary_key=True)
    object_id = Column(Integer, ForeignKey("dq_object.object_id"), nullable=False)
    element_name = Column(String(120), nullable=False)          # display name in rule UI / dashboard
    source_column_name = Column(String(120), nullable=False)    # actual column name at source
    data_type = Column(String(30), nullable=False)              # drives which rule types are offered
    active_flag = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=utcnow)


class DQRule(Base):
    __tablename__ = "dq_rule"

    rule_id = Column(Integer, primary_key=True)
    object_id = Column(Integer, ForeignKey("dq_object.object_id"), nullable=False)
    element_id = Column(Integer, ForeignKey("dq_element.element_id"), nullable=False)
    rule_name = Column(String(200), nullable=False)
    rule_type = Column(String(40), nullable=False)               # required|allowed_values|format_pattern|max_length|unique|conditional_required
    dimension = Column(String(40), nullable=False)               # Completeness|Validity|Format|Uniqueness|Consistency
    severity = Column(String(20), nullable=False, default="Warning")  # Critical|Warning
    rule_config_json = Column(Text, nullable=False)              # source of truth -- what the user entered
    condition_expr = Column(Text, nullable=False)                # generated artifact -- regenerated on every save
    status = Column(String(20), nullable=False, default="draft")  # draft|submitted|approved|rejected
    created_by = Column(String(120))
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    approved_by = Column(String(120))
    approved_at = Column(DateTime)


class DQRun(Base):
    __tablename__ = "dq_run"

    run_id = Column(Integer, primary_key=True)
    object_id = Column(Integer, ForeignKey("dq_object.object_id"), nullable=False)
    run_name = Column(String(200))
    run_type = Column(String(20), nullable=False)                # file_upload|db_fetch
    status = Column(String(20), nullable=False, default="running")  # running|completed|failed
    started_at = Column(DateTime, default=utcnow)
    finished_at = Column(DateTime)
    records_scanned = Column(Integer, default=0)
    source_file_name = Column(String(300))
    error_message = Column(Text)


class DQViolation(Base):
    __tablename__ = "dq_violation"

    violation_id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("dq_run.run_id"), nullable=False)
    object_id = Column(Integer, ForeignKey("dq_object.object_id"), nullable=False)
    element_id = Column(Integer, ForeignKey("dq_element.element_id"), nullable=False)
    rule_id = Column(Integer, ForeignKey("dq_rule.rule_id"), nullable=False)
    record_key = Column(String(120), nullable=False)             # the source Id
    current_value = Column(Text)
    violation_reason = Column(String(400))
    severity = Column(String(20), nullable=False)                # denormalized from rule at write-time
    dimension = Column(String(40), nullable=False)               # denormalized from rule at write-time

    __table_args__ = (
        Index("ix_violation_run_object", "run_id", "object_id"),
        Index("ix_violation_run_rule", "run_id", "rule_id"),
        Index("ix_violation_run_severity", "run_id", "severity"),
    )


class DQMetric(Base):
    __tablename__ = "dq_metric"

    metric_id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("dq_run.run_id"), nullable=False)
    object_id = Column(Integer, ForeignKey("dq_object.object_id"), nullable=False)
    element_id = Column(Integer, ForeignKey("dq_element.element_id"), nullable=False)
    rule_id = Column(Integer, ForeignKey("dq_rule.rule_id"), nullable=False)
    dimension = Column(String(40), nullable=False)
    severity = Column(String(20), nullable=False)
    records_checked = Column(Integer, nullable=False)
    records_failed = Column(Integer, nullable=False)
    score_pct = Column(Float, nullable=False)

    __table_args__ = (
        Index("ix_metric_run_object", "run_id", "object_id"),
    )


class DQReferenceValue(Base):
    """
    Supports the Referential Integrity rule type. stg_source_record is
    cleared after every run (runtime-only, by design) -- so a rule on
    Object B can't JOIN directly against Object A's staged rows, because
    they may already be gone. Instead, we persist just the DISTINCT values
    of whichever element is actually targeted by an approved ref_integrity
    rule (refreshed at the end of each run for that object) -- a small,
    bounded, indexed lookup, not a copy of the source data.

    Bootstrapping note: a ref_integrity rule sees whatever was captured by
    the REFERENCED object's most recent completed run. If that object has
    never been validated, the reference set is empty and the rule will
    fail every row until the referenced object runs at least once.
    """
    __tablename__ = "dq_reference_value"

    id = Column(Integer, primary_key=True)
    object_id = Column(Integer, ForeignKey("dq_object.object_id"), nullable=False)
    element_id = Column(Integer, ForeignKey("dq_element.element_id"), nullable=False)
    value = Column(Text, nullable=False)

    __table_args__ = (
        Index("ix_refval_lookup", "object_id", "element_id", "value"),
    )


# STG_SOURCE_RECORD -- runtime only. One physical column per CDE (V1 has a
# single object, so this stays a flat, explicit table rather than a dynamic
# one). Cleared after every run; never a permanent store.
_stg_cde_columns = {name: Column(name, Text) for name in CDE_COLUMNS}

StgSourceRecord = type(
    "StgSourceRecord",
    (Base,),
    {
        "__tablename__": "stg_source_record",
        "id": Column(Integer, primary_key=True),  # internal surrogate key, not the source Id
        "run_id": Column(Integer, ForeignKey("dq_run.run_id"), nullable=False),
        "record_key": Column(String(120), nullable=False),
        "loaded_at": Column(DateTime, default=utcnow),
        "__table_args__": (Index("ix_stg_run", "run_id"),),
        **_stg_cde_columns,
    },
)
