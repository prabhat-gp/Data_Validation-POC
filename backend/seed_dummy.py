"""
seed_dummy.py
--------------
Populates dummy demo data through the REAL schema (DQ_OBJECT, DQ_ELEMENT,
DQ_RULE, DQ_RUN, DQ_METRIC, DQ_VIOLATION -- proper foreign keys, no shortcuts)
so Mock A can be viewed with numbers, matching the approved dash1.png /
dash2.png mockup as closely as possible.

5 objects (Account Buying/Non-Buying/Team, Contact, Product), each with 3
completed runs named "Run #1/#2/#3" so the whole-database trend chart has
history. Only the latest run (Run #3) gets full element-level detail;
Run #1/#2 get a couple of summary metrics, enough to drive the trend line.

This is ADDITIVE demo data for viewing the dashboard -- it does not touch or
replace the Account/16-CDE catalog created by create_tables.py, and it is
NOT meant to represent the real Onity/Aerospace CDE set. Re-run with --reset
to wipe and reseed.
"""

import json
import sys
from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models import DQObject, DQElement, DQRule, DQRun, DQMetric, DQViolation

now = lambda: datetime.now(timezone.utc)

# object_name -> [(element_name, dimension, score_pct, checked, failed, severity), ...]
# numbers match the approved dash1.png / dash2.png mock as closely as reasonable
OBJECTS = {
    "Account (Buying)": {
        "rows": 55000,
        "elements": [
            ("Hirarchy Type", "Relationship", 78.0, 55000, 12100, "Critical"),
            ("Hotel Chain", "Ref Integrity", 82.0, 55000, 9900, "Critical"),
            ("Key_Account__c", "Completeness", 89.0, 55000, 6050, "Critical"),
            ("Name", "Uniqueness", 86.0, 55000, 7700, "Warning"),
            ("Phone", "Format", 88.0, 55000, 6600, "Warning"),
            ("BillingCountry", "Validity", 92.0, 55000, 4400, "Warning"),
            ("BillingPostalCode", "Format", 95.0, 55000, 2750, "Warning"),
        ],
    },
    "Account (Non-Buying)": {
        "rows": 63000,
        "elements": [
            ("Hirarchy Type", "Relationship", 80.0, 63000, 12600, "Critical"),
            ("Hotel Brand", "Ref Integrity", 85.0, 63000, 9450, "Critical"),
            ("Account_Status__c", "Completeness", 91.0, 63000, 5670, "Critical"),
            ("Name", "Uniqueness", 88.0, 63000, 7560, "Warning"),
            ("Website", "Format", 90.0, 63000, 6300, "Warning"),
            ("Region__c", "Validity", 90.0, 63000, 6300, "Warning"),
        ],
    },
    "Account Team": {
        "rows": 40000,
        "elements": [
            ("GBE__c", "Relationship", 93.0, 40000, 2800, "Warning"),
            ("SBU__c", "Ref Integrity", 90.0, 40000, 4000, "Warning"),
            ("Brand__c", "Completeness", 96.0, 40000, 1600, "Warning"),
            ("Product_Line__c", "Uniqueness", 99.0, 40000, 400, "Warning"),
            ("Type", "Format", 92.0, 40000, 3200, "Warning"),
            ("Industry", "Validity", 94.0, 40000, 2400, "Warning"),
        ],
    },
    "Contact": {
        "rows": 90000,
        "elements": [
            ("Account Name", "Ref Integrity", 82.0, 90000, 16200, "Critical"),
            ("Account_Status__c", "Completeness", 84.0, 90000, 14400, "Critical"),
            ("Sub_Region__c", "Relationship", 86.0, 90000, 12600, "Warning"),
            ("Name", "Uniqueness", 95.0, 90000, 4500, "Warning"),
            ("Phone", "Format", 90.0, 90000, 9000, "Warning"),
            ("BillingCountry", "Validity", 88.0, 90000, 10800, "Warning"),
        ],
    },
    "Product": {
        "rows": 90000,
        "elements": [
            ("SBX__c", "Relationship", 95.0, 90000, 4500, "Warning"),
            ("Brand2__c", "Ref Integrity", 96.0, 90000, 3600, "Warning"),
            ("Name", "Completeness", 98.0, 90000, 1800, "Warning"),
            ("Product_Line__c", "Uniqueness", 99.0, 90000, 900, "Warning"),
            ("Type", "Format", 94.0, 90000, 5400, "Warning"),
            ("Industry", "Validity", 95.0, 90000, 4500, "Warning"),
        ],
    },
}

# whole-database trend (matches dash1.png's DQ Score vs Critical Failed chart)
# (run_name, dq_score, critical_failed_checks_target)
TREND = [
    ("Run #1", 84.0, 97000),
    ("Run #2", 87.0, 106000),
    ("Run #3", 90.2, 107000),  # Run #3's real value comes from the element-level metrics below
]


def reset(db):
    db.query(DQViolation).delete()
    db.query(DQMetric).delete()
    db.query(DQRun).delete()
    db.query(DQRule).delete()
    db.query(DQElement).filter(DQElement.object_id.in_(
        [o.object_id for o in db.query(DQObject).filter(DQObject.object_name != "Account")]
    )).delete(synchronize_session=False)
    db.query(DQObject).filter(DQObject.object_name != "Account").delete()
    db.commit()


def seed(db):
    base_time = now() - timedelta(days=2)

    for obj_name, spec in OBJECTS.items():
        obj = DQObject(
            object_name=obj_name, source_system="SFDC", source_object_name=obj_name.split(" (")[0],
            record_key_column="Id", active_flag=True,
        )
        db.add(obj); db.flush()

        elements = {}
        rules = {}
        for el_name, dimension, *_ in spec["elements"]:
            el = DQElement(object_id=obj.object_id, element_name=el_name, source_column_name=el_name,
                            data_type="string", active_flag=True)
            db.add(el); db.flush()
            elements[el_name] = el

            rule = DQRule(
                object_id=obj.object_id, element_id=el.element_id,
                rule_name=f"{el_name} — {dimension}", rule_type="required",
                dimension=dimension, severity="Critical", rule_config_json="{}",
                condition_expr="(1=0)", status="approved", created_by="seed_dummy",
                approved_by="seed_dummy", approved_at=now(),
            )
            db.add(rule); db.flush()
            rules[el_name] = rule

        # 3 runs: Run #1/#2 get a light summary, Run #3 gets full element detail
        for i, (run_label, dq_score, _) in enumerate(TREND):
            run = DQRun(
                object_id=obj.object_id, run_name=run_label, run_type="file_upload",
                status="completed", started_at=base_time + timedelta(hours=i),
                finished_at=base_time + timedelta(hours=i, minutes=5),
                records_scanned=spec["rows"],
            )
            db.add(run); db.flush()

            if i < len(TREND) - 1:
                # summary-only historical run: two synthetic metric rows (one
                # Critical, one Warning) so the trend line has a real
                # (checked, failed) pair behind dq_score, split by severity
                # the same way fix-profile reports the latest run
                checked = spec["rows"]
                # two metric rows, each independently checking the full row
                # count (same pattern as the real per-element metrics) -- so
                # total checked for this run = 2*checked, and total_failed is
                # sized against THAT so the resulting ratio hits dq_score
                total_failed = int(round(2 * checked * (100 - dq_score) / 100))
                critical_failed = int(round(total_failed * 0.4))
                warning_failed = total_failed - critical_failed
                rule_iter = iter(rules.values())
                el_iter = iter(elements.values())
                for sev, failed in (("Critical", critical_failed), ("Warning", warning_failed)):
                    rule = next(rule_iter, next(iter(rules.values())))
                    el = next(el_iter, next(iter(elements.values())))
                    db.add(DQMetric(
                        run_id=run.run_id, object_id=obj.object_id, element_id=el.element_id,
                        rule_id=rule.rule_id, dimension="Completeness", severity=sev,
                        records_checked=checked, records_failed=failed,
                        score_pct=round((checked - failed) / checked * 100, 2),
                    ))
                continue

            # latest run: full element-level detail + sample violations
            for el_name, dimension, score_pct, checked, failed, severity in spec["elements"]:
                el = elements[el_name]; rule = rules[el_name]
                db.add(DQMetric(
                    run_id=run.run_id, object_id=obj.object_id, element_id=el.element_id,
                    rule_id=rule.rule_id, dimension=dimension, severity=severity,
                    records_checked=checked, records_failed=failed, score_pct=score_pct,
                ))
                sample_n = min(failed, 25)
                for k in range(sample_n):
                    db.add(DQViolation(
                        run_id=run.run_id, object_id=obj.object_id, element_id=el.element_id,
                        rule_id=rule.rule_id, record_key=f"{obj_name[:3].upper()}-{10000+k}",
                        current_value=None, violation_reason=f"{el_name} failed {dimension} check",
                        severity=severity, dimension=dimension,
                    ))
        db.commit()
        print(f"  seeded {obj_name}: {len(spec['elements'])} elements, 3 runs")


def main():
    db = SessionLocal()
    try:
        if "--reset" in sys.argv:
            print("Resetting previous dummy data...")
            reset(db)
        print("Seeding dummy demo data (5 objects, 3 runs each)...")
        seed(db)
        print("Done. Start the API and hit /api/dashboard/kpis etc.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
