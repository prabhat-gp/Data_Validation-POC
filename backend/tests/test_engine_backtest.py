"""
test_engine_backtest.py
------------------------
End-to-end proof against data_dump/temp.csv (101 real Account rows, the
reference sample -- the real ~700MB accounts.csv only exists on the office
laptop). This exercises the EXACT same code path that will run there:
upload -> stage (chunked) -> compile rules -> execute (SQL pushdown) ->
violations -> metrics -> dashboard reads.

Run with:
    cd backend && /usr/bin/python3 -m pytest tests/test_engine_backtest.py -v
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, DQObject, DQElement, DQRule, DQRun, DQMetric, DQViolation
from app import ingestion
from app.rule_compiler import compile_rule
from app.validation_engine import run_validation

TEMP_CSV = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                         "data_dump", "temp.csv")


def _fresh_session():
    """Isolated in-memory DB per test run so this never touches smtc_dq.db."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    import re

    @__import__("sqlalchemy").event.listens_for(engine, "connect")
    def _regexp(dbapi_conn, _):
        def regexp(pattern, value):
            return bool(re.search(pattern, value)) if value is not None else False
        dbapi_conn.create_function("REGEXP", 2, regexp)

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    return Session()


def _seed_account(db):
    obj = DQObject(object_name="Account", source_system="SFDC",
                    source_object_name="Account", record_key_column="Id", active_flag=True)
    db.add(obj); db.flush()
    elements = {}
    for name in ["Name", "Type", "BillingCountry", "Website", "Phone", "Region__c"]:
        el = DQElement(object_id=obj.object_id, element_name=name, source_column_name=name,
                       data_type="string", active_flag=True)
        db.add(el); db.flush()
        elements[name] = el
    db.commit()
    return obj, elements


def test_full_pipeline_against_temp_csv():
    assert os.path.exists(TEMP_CSV), f"reference file not found: {TEMP_CSV}"
    db = _fresh_session()
    obj, elements = _seed_account(db)

    # A handful of rules across different rule_types + the corrected duplicate approach
    rules_to_create = [
        ("required", elements["Name"], {}, "Critical", "Completeness"),
        ("allowed_values", elements["Type"], {"values": ["Owner/Operator", "Product/Service Provider"]}, "Warning", "Validity"),
        ("unique", elements["Name"], {}, "Warning", "Uniqueness"),
        ("format_pattern", elements["Website"], {"pattern": r"^(https?://|www\.)"}, "Warning", "Format"),
    ]
    for rule_type, element, config, severity, dimension in rules_to_create:
        compiled = compile_rule(rule_type, element.source_column_name, json.dumps(config))
        db.add(DQRule(
            object_id=obj.object_id, element_id=element.element_id,
            rule_name=f"{element.element_name} {rule_type}", rule_type=rule_type,
            dimension=dimension, severity=severity, rule_config_json=json.dumps(config),
            condition_expr=compiled.condition_sql, status="approved",  # pre-approved for the test
        ))
    db.commit()

    run = DQRun(object_id=obj.object_id, run_name="backtest", run_type="file_upload", status="running")
    db.add(run); db.commit(); db.refresh(run)

    run_validation(db, run.run_id, "file_upload", file_path=TEMP_CSV)

    db.refresh(run)
    assert run.status == "completed", f"run failed: {run.error_message}"
    assert run.records_scanned == 101, f"expected 101 rows staged, got {run.records_scanned}"

    metrics = db.query(DQMetric).filter(DQMetric.run_id == run.run_id).all()
    assert len(metrics) == 4, f"expected 4 metric rows (one per rule), got {len(metrics)}"

    # staging must be cleared after the run (runtime-only table)
    from sqlalchemy import text
    staged_left = db.execute(text("SELECT COUNT(*) FROM stg_source_record WHERE run_id=:r"),
                              {"r": run.run_id}).scalar()
    assert staged_left == 0, "staging should be cleared after a completed run"

    print("\n--- Backtest results against data_dump/temp.csv (101 rows) ---")
    for m in metrics:
        rule = db.get(DQRule, m.rule_id)
        print(f"  {rule.rule_name:35s} checked={m.records_checked:<4} failed={m.records_failed:<4} score={m.score_pct}%")

    violation_count = db.query(DQViolation).filter(DQViolation.run_id == run.run_id).count()
    print(f"  total violations captured: {violation_count}")

    # sanity: every metric's checked count must equal records_scanned (no rule silently skipped rows)
    for m in metrics:
        assert m.records_checked == run.records_scanned


def test_failed_run_marks_status_and_preserves_error():
    """A bad rule config must fail the run cleanly, not hang or corrupt state."""
    db = _fresh_session()
    obj, elements = _seed_account(db)

    run = DQRun(object_id=obj.object_id, run_type="file_upload", status="running")
    db.add(run); db.commit(); db.refresh(run)

    try:
        run_validation(db, run.run_id, "file_upload", file_path="/nonexistent/file.csv")
    except Exception:
        pass  # expected -- run_validation re-raises after marking the run failed

    db.refresh(run)
    assert run.status == "failed"
    assert run.error_message is not None


if __name__ == "__main__":
    test_full_pipeline_against_temp_csv()
    test_failed_run_marks_status_and_preserves_error()
    print("\nAll backtest checks passed.")
