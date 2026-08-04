"""
models.py
---------
SQLAlchemy ORM models for the SMTC Data Validation Framework.

SCHEMA SHAPE
------------
CONFIG side is a SINGLE table -- `val_rules`. There is no separate object or
element catalog: a rule row carries `entity_name` / `field_name` /
`primary_key_field` directly. Three tiny lookup tables (rule type, severity,
status) exist purely to constrain values and give the UI something to show.

The entity catalog that `val_rules` no longer stores lives in the ENTITIES
constant below -- known up front, by design. Nothing is discovered at upload
time, so a rule can be authored before any file is ever uploaded.

RESULTS side (`val_runs`, `val_metrics`, `val_violations`) identifies things
by NAME, not by foreign key into a catalog. That is deliberate: historical
results stay correct even if a rule is later renamed or deleted, and the
results tables can live in a physically separate database from val_rules
without needing a cross-database join.
"""

import re
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base

# TWO metadata sets -- config tables and results tables live in different
# databases (CONFIG_DB / TARGET_DB), so they must not share one MetaData.
ConfigBase = declarative_base()
ResultsBase = declarative_base()
Base = ResultsBase          # back-compat for existing imports


def utcnow():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# ENTITY CATALOG -- known up front (no dynamic discovery)
# ---------------------------------------------------------------------------
# Everything the removed dq_object / dq_element tables used to hold. Adding a
# new object = adding an entry here, not a schema migration or a runtime
# CREATE TABLE.
#
#   source_system       -> stamped onto every rule for this entity
#   source_object_name  -> the real table/object name at source
#   primary_key_field   -> the record identifier; what makes a violation
#                          actionable ("record 001xx failed", not "row 4782")
#   columns             -> the CDEs. ONLY these are staged; a 443-column source
#                          export is pruned to just this list during ingestion.
# Where data can come from. Drives the dashboard filter and the Runs page.
SOURCE_SYSTEMS = ["SFDC", "Hybris", "MySQL", "File Dump"]

ENTITIES: dict = {
    "Account": {
        "source_system": "SFDC",
        "source_object_name": "Account",
        "primary_key_field": "Id",
        "columns": [
            "BillingCity", "BillingCountry", "BillingPostalCode", "BillingState",
            "BillingStreet", "Industry", "Name", "Phone",
            "ShippingCity", "ShippingCountry", "ShippingPostalCode", "ShippingState",
            "ShippingStreet", "Type", "Website", "Region__c",
        ],
    },
    # --- MySQL source_db (data_dump/b2b*.csv) -------------------------------
    "B2B Customer": {
        "source_system": "MySQL",
        "source_object_name": "b2bcustomer",
        "primary_key_field": "customer_id",
        "columns": ["customer_name", "email", "status", "region", "sbg_id"],
    },
    "B2B Product": {
        "source_system": "MySQL",
        "source_object_name": "b2bproduct",
        "primary_key_field": "product_id",
        "columns": ["product_code", "product_name", "category", "status", "sbg_id"],
    },
    "B2B Price": {
        "source_system": "MySQL",
        "source_object_name": "b2bprice",
        "primary_key_field": "price_id",
        "columns": ["product_id", "price_amount", "discount_pct", "status",
                    "eff_date", "end_date"],
    },
    "B2B SBG": {
        "source_system": "MySQL",
        "source_object_name": "b2bsbg",
        "primary_key_field": "sbg_id",
        "columns": ["sbg_name"],
    },
}

# Convenience view: {entity_name: [column, ...]}
CDE_COLUMNS: dict = {k: v["columns"] for k, v in ENTITIES.items()}


def staging_table_name(entity_name: str) -> str:
    """'Account Team' -> 'stg_account_team'. One staging table per entity."""
    slug = re.sub(r"[^a-z0-9]+", "_", entity_name.lower()).strip("_")
    return f"stg_{slug}"


# ---------------------------------------------------------------------------
# LOOKUP TABLES
# ---------------------------------------------------------------------------
class ValRuleType(ConfigBase):
    """The 8 supported rule types. `code` is what val_rules.rule_type stores."""
    __tablename__ = "val_rule_types"

    code = Column(String(50), primary_key=True)
    description = Column(String(255), nullable=False)
    dimension = Column(String(40), nullable=False)        # drives dashboard heatmap columns
    execution_type = Column(String(50), nullable=False)   # QUERY | RECORD (derived, never user input)


class ValSeverity(ConfigBase):
    __tablename__ = "val_severities"
    code = Column(String(20), primary_key=True)
    description = Column(String(255))


class ValStatus(ConfigBase):
    __tablename__ = "val_statuses"
    code = Column(String(20), primary_key=True)
    description = Column(String(255))


# ---------------------------------------------------------------------------
# CONFIG -- the single rules table
# ---------------------------------------------------------------------------
class ValRule(ConfigBase):
    """
    One row = one validation rule. Self-describing: it names its own entity,
    field and key column, so nothing needs joining to interpret it.

    rule_definition is the SOURCE OF TRUTH (exactly what the user configured).
    The SQL is NOT stored -- it is compiled from rule_definition at run time,
    so the two can never drift out of sync.
    """
    __tablename__ = "val_rules"

    rule_id = Column(Integer, primary_key=True, autoincrement=True)   # simple 1, 2, 3…
    rule_name = Column(String(255), nullable=False)
    source_system = Column(String(100), nullable=False)
    rule_type = Column(String(50), nullable=False)      # validated in code, not an FK

    entity_name = Column(String(255), nullable=False)        # was dq_object.object_name
    field_name = Column(String(255), nullable=False)         # was dq_element.element_name
    primary_key_field = Column(String(255), nullable=False)  # was dq_object.record_key_column

    execution_type = Column(String(50), nullable=False)      # QUERY | RECORD (derived from rule_type)
    rule_definition = Column(Text)                           # JSON config -- source of truth
    error_message = Column(Text)                             # stamped onto each violation

    severity = Column(String(20), nullable=False)
    status = Column(String(20), nullable=False, default="DRAFT")
    active = Column(Boolean, nullable=False, default=True)

    created_by = Column(String(100), nullable=False)
    created_date = Column(DateTime, nullable=False, default=utcnow)
    updated_by = Column(String(100))
    updated_date = Column(DateTime)

    # Separation of duties: an author must not approve their own rule. Kept
    # distinct from updated_by/updated_date, which change on ANY edit and so
    # cannot serve as an approval record.
    approved_by = Column(String(100))
    approved_date = Column(DateTime)

    __table_args__ = (Index("ix_rule_entity_status", "entity_name", "status", "active"),)


# ---------------------------------------------------------------------------
# EXECUTION
# ---------------------------------------------------------------------------
class ValBatch(ResultsBase):
    """
    What a user calls "a run": one trigger covering 1..N entities.
    Each entity inside it gets its own val_runs row, so one entity failing
    cannot take down the others. Batch status is DERIVED from its runs, never
    stored -- a crash can't leave a stored status permanently wrong.
    """
    __tablename__ = "val_batches"

    batch_id = Column(Integer, primary_key=True, autoincrement=True)
    batch_name = Column(String(200))
    run_type = Column(String(20), nullable=False)     # file_upload | db_fetch
    source_system = Column(String(50))                # SFDC | Hybris | MySQL | File Dump
    triggered_by = Column(String(100))
    started_at = Column(DateTime, default=utcnow)


class ValRun(ResultsBase):
    """One entity's execution within a batch."""
    __tablename__ = "val_runs"

    run_id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey("val_batches.batch_id"), nullable=False)
    entity_name = Column(String(255), nullable=False)
    run_type = Column(String(20), nullable=False)
    source_system = Column(String(50))
    status = Column(String(20), nullable=False, default="pending")  # pending|running|completed|failed
    started_at = Column(DateTime, default=utcnow)
    finished_at = Column(DateTime)
    records_scanned = Column(Integer, default=0)
    rules_executed = Column(Integer, default=0)
    source_file_name = Column(String(300))
    error_message = Column(Text)

    __table_args__ = (
        UniqueConstraint("batch_id", "entity_name", name="uq_run_batch_entity"),
        Index("ix_run_entity_status", "entity_name", "status"),
    )


class ValMetric(ResultsBase):
    """
    One row per rule per run. The dashboard reads ONLY this table -- a few
    hundred rows per run -- never a live scan of val_violations. That is why
    the dashboard stays instant regardless of source data volume.
    """
    __tablename__ = "val_metrics"

    metric_id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("val_runs.run_id"), nullable=False)
    rule_id = Column(Integer, nullable=False)         # not an FK: results outlive rule edits
    entity_name = Column(String(255), nullable=False)
    field_name = Column(String(255), nullable=False)
    dimension = Column(String(40), nullable=False)    # denormalized at write time
    severity = Column(String(20), nullable=False)     # denormalized at write time
    records_checked = Column(Integer, nullable=False)
    records_failed = Column(Integer, nullable=False)
    score_pct = Column(Float, nullable=False)

    __table_args__ = (Index("ix_metric_run_entity", "run_id", "entity_name"),)


class ValViolation(ResultsBase):
    """One row per failing record per rule per run -- the fix team's worklist."""
    __tablename__ = "val_violations"

    violation_id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("val_runs.run_id"), nullable=False)
    rule_id = Column(Integer, nullable=False)
    entity_name = Column(String(255), nullable=False)
    field_name = Column(String(255), nullable=False)
    record_key = Column(String(120), nullable=False)   # the source Id -- makes it actionable
    current_value = Column(Text)
    violation_reason = Column(String(400))
    severity = Column(String(20), nullable=False)
    dimension = Column(String(40), nullable=False)

    __table_args__ = (
        Index("ix_violation_run_entity", "run_id", "entity_name"),
        Index("ix_violation_run_rule", "run_id", "rule_id"),
        Index("ix_violation_run_severity", "run_id", "severity"),
    )


# NOTE: val_reference_values is gone. It existed to snapshot a referenced
# entity's distinct values because staging was wiped between entities, which
# forced REFERENTIAL_INTEGRITY to read the PREVIOUS run. The three-phase batch
# (stage all -> validate all -> clear all) lets the rule do a real LEFT JOIN
# against the lookup entity's live staging table, so the snapshot is obsolete.


# ---------------------------------------------------------------------------
# STAGING -- one table per entity, generated from ENTITIES. Runtime-only.
# ---------------------------------------------------------------------------
# Physical columns (not JSON/EAV) so a compiled rule can say `WHERE Name IS NULL`
# against a real column and stay fast and indexable. Generated at import time
# from a constant, so there is never a runtime CREATE TABLE.
STAGING_MODELS: dict = {}

for _entity, _meta in ENTITIES.items():
    _attrs = {
        "__tablename__": staging_table_name(_entity),
        "id": Column(Integer, primary_key=True),      # surrogate; NOT the source Id
        "run_id": Column(Integer, nullable=False, index=True),
        "record_key": Column(String(120), nullable=False),
        "loaded_at": Column(DateTime, default=utcnow),
    }
    for _col in _meta["columns"]:
        _attrs[_col] = Column(_col, Text)
    STAGING_MODELS[_entity] = type(
        f"Stg{re.sub(r'[^A-Za-z0-9]', '', _entity)}", (ResultsBase,), _attrs
    )


def staging_model(entity_name: str):
    model = STAGING_MODELS.get(entity_name)
    if model is None:
        raise KeyError(f"Unknown entity {entity_name!r}. Known: {list(ENTITIES)}")
    return model
