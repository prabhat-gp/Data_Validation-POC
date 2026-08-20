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

# Three metadata sets: config, results and staging live in different
# databases, so they must not share one MetaData.
ConfigBase = declarative_base()     # config_db  -- rules + lookups
ResultsBase = declarative_base()    # results_db -- batches, runs, metrics, violations
StagingBase = declarative_base()    # source_db  -- stg_* tables, beside the source data
Base = ResultsBase          # back-compat for existing imports


def utcnow():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# ENTITY CATALOG -- known up front (no dynamic discovery)
# ---------------------------------------------------------------------------
# Adding a new object means adding an entry here -- not a schema migration
# and not a runtime CREATE TABLE.
#
#   source_system       -> stamped onto every rule for this entity
#   source_object_name  -> the real table/object name at source
#   primary_key_field   -> the record identifier; what makes a violation
#                          actionable ("record 001xx failed", not "row 4782")
#   columns             -> the CDEs. ONLY these are staged; a 443-column source
#                          export is pruned to just this list during ingestion.
#   index_columns       -> staging columns that get an index. OPTIONAL.
#   column_lengths      -> per-column VARCHAR length override. OPTIONAL.
#
# WHICH COLUMNS BELONG IN index_columns
# -------------------------------------
# Only the ones a rule JOINS or GROUPS on -- an index earns its keep on those
# and costs insert time on every other. Three rule types drive the list:
#
#   REFERENTIAL_INTEGRITY  the child's FK column, and the parent's lookup
#                          column when it is NOT the parent's primary key
#                          (when it IS, the compiler rewrites it to
#                          record_key, which every staging table indexes
#                          by default)
#   UNIQUENESS             the field it de-duplicates on
#   AGGREGATION            its groupBy columns
#
# Everything else -- COMPLETENESS, VALIDITY, ALLOWED_VALUES, CROSS_FIELD,
# CUSTOM_SQL -- is a per-row predicate that reads the whole run anyway, so an
# index would be ignored by the planner and paid for on every insert.
#
# Measured on 1M rows: a referential-integrity join went from not finishing in
# 23 minutes to 16.5s once its key was indexed, on the DEFAULT join buffer.
# See extra/make_scale_data.py and extra/predict_failures.py.


# SFDC and Hybris are the upstream systems of record. Both are landed into
# SOURCE_DB by ETL -- what an Oracle staging layer does in production.
SOURCE_SYSTEMS = ["SFDC", "Hybris", "File Dump"]

ENTITIES: dict = {
    # SFDC. The 650MB / 450-column export is sliced to these 17 columns by
    # extra/prepare_dump.py and landed in source_db.
    "Account": {
        "source_system": "SFDC",
        "source_object_name": "account",
        "primary_key_field": "Id",
        "columns": [
            "BillingCity", "BillingCountry", "BillingPostalCode", "BillingState",
            "BillingStreet", "Industry", "Name", "Phone",
            "ShippingCity", "ShippingCountry", "ShippingPostalCode", "ShippingState",
            "ShippingStreet", "Type", "Website", "Region__c",
        ],
        # Name only: UNIQUENESS goes 1.95s -> 0.19s at 150k rows.
        # Website was declared here for its AGGREGATION groupBy and REMOVED --
        # measured at 1.25s vs 1.36s, i.e. no benefit. A GROUP BY that has no
        # HAVING-independent filter reads every row of the run anyway, so the
        # index only adds write cost. Grouping alone does not justify an index;
        # a JOIN or an equality lookup does.
        "index_columns": ["Name"],
    },
    # --- Hybris -----------------------------------------------------------
    # Only the YELLOW-highlighted columns from data_dump/*.xlsx are declared.
    # The exports carry 54 / 65 / 46 columns; these are the CDEs.
    # The key column exports as "# pk" (Hybris impex artifact) -- it is `pk`
    # once loaded into MySQL.
    "B2B Customer": {
        "source_system": "Hybris",
        "source_object_name": "b2bcustomer",
        "primary_key_field": "pk",
        "columns": [
            "originalUid", "name", "email", "phone",
            "active", "loginDisabled", "creationtime",
            "defaultB2BUnit", "hwCustomerType", "toolAccess",
            "sessionCurrency", "sessionLanguage", "sfdcContactId",
        ],
        # defaultB2BUnit: REFERENTIAL_INTEGRITY -> B2B Unit.uid.
        # The other three: UNIQUENESS.
        "index_columns": ["defaultB2BUnit", "originalUid", "email", "sfdcContactId"],
    },
    "B2B Unit": {
        "source_system": "Hybris",
        "source_object_name": "b2bunit",
        "primary_key_field": "pk",
        "columns": [
            "uid", "name", "locName_en", "accountType",
            "active", "orderBlock", "sfdcServiceLayer",
            # not yellow, but declared so Address referential integrity can
            # use it: b2bunit.addresses -> address.pk
            "addresses",
        ],
        # uid: the LOOKUP TARGET of B2B Customer.defaultB2BUnit, and it is not
        #      this entity's pk, so it needs its own index. This is the one
        #      that took referential integrity from "did not finish in 23
        #      minutes" to 16.5s at 1M rows.
        # addresses: REFERENTIAL_INTEGRITY -> Address.pk.
        # name: UNIQUENESS.
        "index_columns": ["uid", "addresses", "name"],
    },
    "Address": {
        "source_system": "Hybris",
        "source_object_name": "address",
        "primary_key_field": "pk",
        "columns": [
            "country", "postalcode", "billingAddress", "shippingAddress",
            "saveAddress",
        ],
        # B2B Unit.addresses points at this entity's pk, which the compiler
        # rewrites to record_key -- already indexed on every staging table.
        # Nothing else here is joined or grouped on.
        "index_columns": [],
    },
}

# Convenience view: {entity_name: [column, ...]}
CDE_COLUMNS: dict = {k: v["columns"] for k, v in ENTITIES.items()}


def staging_table_name(entity_name: str) -> str:
    """'Account Team' -> 'stg_account_team'. One staging table per entity."""
    slug = re.sub(r"[^a-z0-9]+", "_", entity_name.lower()).strip("_")
    return f"stg_{slug}"


# A rule with no single field_name (multi-field UNIQUENESS, grouped
# AGGREGATION) is labelled with the combination it checks, joined by this.
# The dashboard splits on it to count the REAL elements a rule covers:
# "Name + BillingCountry" is TWO elements, not a column of that name.
# Both producer (validation_engine._display_field) and consumer (the dashboard
# counters) must use this constant, or the element counts silently drift.
MULTI_FIELD_SEP = " + "


def elements_in(field_label: str) -> list:
    """The real element names behind one metric's field_name label."""
    return [f.strip() for f in (field_label or "").split(MULTI_FIELD_SEP) if f.strip()]


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
    # Which quality dimension this rule reports under. DERIVED from rule_type
    # via RULE_TYPE_META and stamped at create time -- never user input, so a
    # REFERENTIAL_INTEGRITY rule can only ever be Integrity. Stored rather than
    # resolved at read time so the results side needs no lookup. To change the
    # classification, edit RULE_TYPE_META and run `migrate_db.py --apply`.
    dimension = Column(String(40))
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
    total_records = Column(Integer)          # expected row count, known before staging
    phase = Column(String(20))               # staging | validating | done
    rules_total = Column(Integer, default=0)
    rules_done = Column(Integer, default=0)
    rules_executed = Column(Integer, default=0)
    # Distinct records with at least one violation. Computed ONCE when the run
    # finishes, because the dashboard must never scan val_violations live --
    # that table runs to millions of rows while val_runs is one row per entity.
    records_affected = Column(Integer, default=0)
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

    # Every index ends in violation_id because the worklist pages by KEYSET
    # (`WHERE violation_id > :cursor ORDER BY violation_id`) -- see
    # routers/violations.py. InnoDB appends the primary key to a secondary
    # index implicitly, so on MySQL alone the trailing column is free; Oracle
    # does not, and without it the range scan degrades into a sort of every
    # matching row. Naming it explicitly keeps both backends on the same plan.
    __table_args__ = (
        Index("ix_violation_run", "run_id", "violation_id"),
        Index("ix_violation_run_entity", "run_id", "entity_name", "violation_id"),
        Index("ix_violation_run_rule", "run_id", "rule_id", "violation_id"),
        Index("ix_violation_run_severity", "run_id", "severity", "violation_id"),
    )


# NOTE: val_reference_values is gone. It existed to snapshot a referenced
# entity's distinct values because staging was wiped between entities, which
# forced REFERENTIAL_INTEGRITY to read the PREVIOUS run. The three-phase batch
# (stage all -> validate all -> clear all) lets the rule do a real LEFT JOIN
# against the lookup entity's live staging table, so the snapshot is obsolete.


# ---------------------------------------------------------------------------
# STAGING -- one table per entity, in SOURCE_DB alongside the source tables.
# ---------------------------------------------------------------------------
# Physical columns (not JSON/EAV) so a compiled rule can say `WHERE Name IS NULL`
# against a real column and stay fast and indexable. Generated at import time
# from a constant, so there is never a runtime CREATE TABLE.
#
# WHY String() AND NOT Text
# -------------------------
# Text renders as CLOB on Oracle, and a CLOB cannot carry an ordinary B-tree
# index -- a REFERENTIAL_INTEGRITY join on one is not merely slow, it does not
# work without a function-based index on DBMS_LOB.SUBSTR. MySQL hides this
# behind prefix indexes (`col(32)`), which is why the problem only showed up
# under load. String(n) is VARCHAR on MySQL/Postgres and VARCHAR2 on Oracle:
# indexable and joinable everywhere, with no dialect-specific tuning.
#
# 255 chars = 1020 bytes in utf8mb4, comfortably inside InnoDB's 3072-byte
# index key limit and Oracle's per-block limit. A column that genuinely needs
# more can override via "column_lengths" below -- it just cannot be a join key.
STAGING_COLUMN_LENGTH = 255

# Longest identifier Oracle accepted before 12.2. Index names are global there
# (not per-table as in MySQL), so they are prefixed with the table name and
# clipped to fit rather than risking a name collision at migration time.
_MAX_IDENTIFIER = 30


def _index_name(table: str, column: str) -> str:
    """
    ix_<entity>_<column>. The shared "stg_" prefix is dropped -- it carries no
    information (every table here has it) and those four characters are what
    pushes the longest names past Oracle's limit.
    """
    return f"ix_{table[4:] if table.startswith('stg_') else table}_{column.lower()}"


STAGING_MODELS: dict = {}

for _entity, _meta in ENTITIES.items():
    _table = staging_table_name(_entity)
    _lengths = _meta.get("column_lengths", {})
    _attrs = {
        "__tablename__": _table,
        "id": Column(Integer, primary_key=True),      # surrogate; NOT the source Id
        # No index=True here on purpose. Every index below LEADS with run_id,
        # so a standalone one would be redundant -- and each redundant index
        # costs roughly 14s per million rows staged (measured).
        "run_id": Column(Integer, nullable=False),
        "record_key": Column(String(120), nullable=False),
        "loaded_at": Column(DateTime, default=utcnow),
    }
    for _col in _meta["columns"]:
        _attrs[_col] = Column(_col, String(_lengths.get(_col, STAGING_COLUMN_LENGTH)))

    # SINGLE COLUMN, and specifically NOT (run_id, col).
    #
    # Staging is truncated between runs, so run_id has a cardinality of ONE.
    # Leading with it adds no selectivity, but it does make the index look
    # usable for `WHERE run_id = :run_id`, which every per-row rule carries.
    # MySQL then walks the whole index and does one row lookup per entry
    # instead of a single sequential scan. Measured on 2.5M staged rows:
    #
    #     (run_id, record_key)   per-row rule  30.8s   FK join 5.9s
    #     (record_key)           per-row rule   1.4s   FK join 7.0s
    #
    # 22x faster on the shape 37 of our 39 rules use, 19% slower on the two
    # that join. There is no run_id index at all for the same reason -- one
    # would simply reintroduce the bad plan.
    _indexes = [
        # record_key is the lookup target of EVERY referential-integrity rule
        # that points at a primary key -- the compiler rewrites the parent's
        # `lookupField` to `record_key` when it is that entity's pk.
        Index(_index_name(_table, "record_key"), "record_key"),
    ]
    _indexes += [
        Index(_index_name(_table, _col), _col)
        for _col in _meta.get("index_columns", [])
    ]
    _attrs["__table_args__"] = tuple(_indexes)

    STAGING_MODELS[_entity] = type(
        f"Stg{re.sub(r'[^A-Za-z0-9]', '', _entity)}", (StagingBase,), _attrs
    )


def staging_model(entity_name: str):
    model = STAGING_MODELS.get(entity_name)
    if model is None:
        raise KeyError(f"Unknown entity {entity_name!r}. Known: {list(ENTITIES)}")
    return model
