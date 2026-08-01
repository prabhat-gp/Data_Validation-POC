"""
test_engine_backtest.py
-----------------------
End-to-end proof against data_dump/temp.csv (101 real Account rows -- the
reference sample; the real ~700MB accounts.csv only exists on the office
laptop).

Runs the FULL pipeline -- stage -> compile -> execute -> metrics -> refresh
reference values -> clear staging -- exactly as a production run does.
Expected counts are computed from the CSV itself rather than hardcoded, so a
failure means engine behaviour changed, not that the fixture drifted.

Run with:  python -m pytest tests/ -v      (from backend/)
"""

import csv
import json
import os
import sys
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models import (  # noqa: E402
    Base, ValBatch, ValMetric, ValRule, ValRun, ValViolation,
)
from app.rule_compiler import execution_type_for  # noqa: E402
from app.validation_engine import run_validation  # noqa: E402

CSV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data_dump", "temp.csv",
)


def utcnow():
    return datetime.now(timezone.utc)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")

    # REGEXP is not built into SQLite -- register it, same as app/database.py
    import re as _re
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _add_regexp(conn, _):
        conn.create_function(
            "REGEXP", 2,
            lambda pattern, value: 1 if value is not None and _re.search(pattern, value) else 0,
        )

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _csv_rows():
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _make_rule(db, field, rule_type, definition, severity="Warning"):
    rule = ValRule(
        rule_id=f"ACCOUNT_{field.upper()}_{rule_type.upper()}",
        rule_name=f"{field} — {rule_type}",
        source_system="SFDC",
        rule_type=rule_type,
        entity_name="Account",
        field_name=field,
        primary_key_field="Id",
        execution_type=execution_type_for(rule_type),
        rule_definition=json.dumps(definition),
        severity=severity,
        status="approved",              # the engine only ever loads approved rules
        active=True,
        created_by="test",
        created_date=utcnow(),
        approved_by="approver",         # a different person -- separation of duties
        approved_date=utcnow(),
    )
    db.add(rule)
    return rule


def _make_run(db, entity="Account"):
    batch = ValBatch(batch_name="backtest", run_type="file_upload", started_at=utcnow())
    db.add(batch)
    db.flush()
    run = ValRun(batch_id=batch.batch_id, entity_name=entity,
                 run_type="file_upload", status="pending", started_at=utcnow())
    db.add(run)
    db.commit()
    return run


@pytest.mark.skipif(not os.path.exists(CSV_PATH), reason="temp.csv not present")
def test_full_pipeline_against_real_sample(db):
    rows = _csv_rows()

    _make_rule(db, "Name", "required", {}, "Critical")
    _make_rule(db, "BillingCity", "required", {}, "Critical")
    _make_rule(db, "Name", "unique", {})
    db.commit()

    run = _make_run(db)
    run_validation(db, run.run_id, "file_upload", file_path=CSV_PATH)

    db.refresh(run)
    assert run.status == "completed", run.error_message
    assert run.records_scanned == len(rows)
    assert run.rules_executed == 3

    expected_name_blank = sum(1 for r in rows if not (r.get("Name") or "").strip())
    expected_city_blank = sum(1 for r in rows if not (r.get("BillingCity") or "").strip())

    metrics = {m.rule_id: m for m in db.query(ValMetric).filter(ValMetric.run_id == run.run_id)}
    assert len(metrics) == 3
    assert metrics["ACCOUNT_NAME_REQUIRED"].records_failed == expected_name_blank
    assert metrics["ACCOUNT_BILLINGCITY_REQUIRED"].records_failed == expected_city_blank

    # every metric denormalizes entity/field so the dashboard needs no join
    for m in metrics.values():
        assert m.entity_name == "Account"
        assert m.field_name and m.dimension and m.severity

    # staging is runtime-only and must be empty once the run finishes
    left = db.execute(text("SELECT COUNT(*) FROM stg_account WHERE run_id=:r"),
                      {"r": run.run_id}).scalar()
    assert left == 0


@pytest.mark.skipif(not os.path.exists(CSV_PATH), reason="temp.csv not present")
def test_scope_filter_narrows_the_row_set(db):
    """
    The optional filter must genuinely reduce what's checked. Required on
    BillingCity across ALL rows fails far more than the same rule scoped to
    Type='Owner/Operator' -- if the two match, the filter was ignored.
    """
    rows = _csv_rows()
    unfiltered_expected = sum(1 for r in rows if not (r.get("BillingCity") or "").strip())
    filtered_expected = sum(
        1 for r in rows
        if (r.get("Type") or "").strip() == "Owner/Operator"
        and not (r.get("BillingCity") or "").strip()
    )
    assert filtered_expected < unfiltered_expected, "sample can't discriminate; check temp.csv"

    _make_rule(db, "BillingCity", "required", {
        "filter": {"conditions": [{"field": "Type", "operator": "=", "value": "Owner/Operator"}],
                   "logic": "AND"},
    })
    db.commit()

    run = _make_run(db)
    run_validation(db, run.run_id, "file_upload", file_path=CSV_PATH)

    metric = db.query(ValMetric).filter(ValMetric.run_id == run.run_id).one()
    assert metric.records_failed == filtered_expected
    assert db.query(ValViolation).filter(ValViolation.run_id == run.run_id).count() == filtered_expected


@pytest.mark.skipif(not os.path.exists(CSV_PATH), reason="temp.csv not present")
def test_only_approved_rules_execute(db):
    """The approval gate is structural: a draft rule exists but is inert."""
    rule = _make_rule(db, "Name", "required", {})
    rule.status = "draft"
    db.commit()

    run = _make_run(db)
    run_validation(db, run.run_id, "file_upload", file_path=CSV_PATH)

    db.refresh(run)
    assert run.status == "completed"
    assert run.rules_executed == 0
    assert db.query(ValMetric).filter(ValMetric.run_id == run.run_id).count() == 0


@pytest.mark.skipif(not os.path.exists(CSV_PATH), reason="temp.csv not present")
def test_missing_column_fails_the_run_loudly(db):
    """
    A file that doesn't match the entity must FAIL, not silently validate
    nothing and report a false 100%.
    """
    run = _make_run(db, entity="Contact")   # Account CSV against Contact's columns

    with pytest.raises(Exception):
        run_validation(db, run.run_id, "file_upload", file_path=CSV_PATH)

    db.refresh(run)
    assert run.status == "failed"
    assert "missing required columns" in (run.error_message or "")
