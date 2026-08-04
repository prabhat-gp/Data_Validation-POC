"""
seed_dummy.py
-------------
Seeds DASHBOARD DEMO DATA ONLY -- 3 batches, each covering all 4 entities.

  Run #1  db_fetch     4 entities
  Run #2  file_upload  4 entities
  Run #3  db_fetch     4 entities

Creates ZERO val_rules rows and ZERO violations. Rules are authored by users
through the UI; nothing here should ever put a rule in front of them.

That is possible because val_metrics carries entity_name / field_name /
dimension denormalized and rule_id is not a foreign key -- so demo metrics can
exist without inventing demo rules.

Usage:
    python seed_dummy.py --reset    # wipe demo runs/metrics and reseed
"""

import sys
from datetime import timedelta

from sqlalchemy.orm import Session

from app.database import results_engine as engine
from app.models import ValBatch, ValMetric, ValRun, ValViolation, utcnow

# Every field below is a REAL column from the ENTITIES catalog in models.py.
# (field, dimension, records_failed_at_run3, severity)
DEMO = {
    "Account": {
        "rows": 55000,
        "fields": [
            ("Name",              "Completeness",  6050,  "CRITICAL"),
            ("Type",              "Relationship",  12100, "CRITICAL"),
            ("BillingCountry",    "Ref Integrity", 9900,  "CRITICAL"),
            ("Name",              "Uniqueness",    7700,  "WARNING"),
            ("Phone",             "Format",        6600,  "WARNING"),
            ("Industry",          "Validity",      4400,  "WARNING"),
            ("BillingPostalCode", "Format",        2750,  "WARNING"),
            ("Website",           "Format",        3300,  "WARNING"),
        ],
    },
    "Contact": {
        "rows": 90000,
        "fields": [
            ("LastName",        "Completeness",  14400, "CRITICAL"),
            ("AccountId",       "Ref Integrity", 16200, "CRITICAL"),
            ("MailingCountry",  "Relationship",  12600, "WARNING"),
            ("Email",           "Uniqueness",    4500,  "WARNING"),
            ("Phone",           "Format",        9000,  "WARNING"),
            ("Title",           "Validity",      10800, "WARNING"),
        ],
    },
    "Product": {
        "rows": 90000,
        "fields": [
            ("Name",                   "Completeness",  1800, "CRITICAL"),
            ("ProductCode",            "Ref Integrity", 3600, "WARNING"),
            ("Family",                 "Relationship",  4500, "WARNING"),
            ("ProductCode",            "Uniqueness",    900,  "WARNING"),
            ("Description",            "Format",        5400, "WARNING"),
            ("QuantityUnitOfMeasure",  "Validity",      4500, "WARNING"),
        ],
    },
    "Account Team": {
        "rows": 40000,
        "fields": [
            ("UserId",             "Completeness",  1600, "CRITICAL"),
            ("AccountId",          "Ref Integrity", 4000, "WARNING"),
            ("TeamMemberRole",     "Relationship",  2800, "WARNING"),
            ("AccountAccessLevel", "Validity",      2400, "WARNING"),
        ],
    },
}

# (run_type, failure multiplier) -- quality improves over the three runs
BATCHES = [
    ("db_fetch",    1.85),
    ("db_fetch",    1.35),
    ("db_fetch",    1.00),
]
DEMO_SOURCE = "Hybris"     # the seeded dashboard data represents Hybris


def reset(db: Session):
    db.query(ValViolation).delete()
    db.query(ValMetric).delete()
    db.query(ValRun).delete()
    db.query(ValBatch).delete()
    db.commit()
    print("Cleared demo runs/metrics (val_rules untouched).")


def seed(db: Session):
    base = utcnow() - timedelta(days=2)

    for i, (run_type, multiplier) in enumerate(BATCHES, start=1):
        batch = ValBatch(
            batch_name=f"Run #{i}",
            run_type=run_type, source_system=DEMO_SOURCE,
            triggered_by="demo_seed",
            started_at=base + timedelta(hours=i * 6),
        )
        db.add(batch)
        db.flush()

        for entity, spec in DEMO.items():
            run = ValRun(
                batch_id=batch.batch_id,
                entity_name=entity,
                run_type=run_type, source_system=DEMO_SOURCE,
                status="completed",
                started_at=base + timedelta(hours=i * 6),
                finished_at=base + timedelta(hours=i * 6, minutes=8),
                records_scanned=spec["rows"],
                rules_executed=len(spec["fields"]),
            )
            db.add(run)
            db.flush()

            for n, (field, dimension, failed, severity) in enumerate(spec["fields"], start=1):
                checked = spec["rows"]
                scaled = min(int(failed * multiplier), checked)
                db.add(ValMetric(
                    run_id=run.run_id,
                    rule_id=n,          # synthetic; NOT an FK, so no demo rules needed
                    entity_name=entity,
                    field_name=field,
                    dimension=dimension,
                    severity=severity,
                    records_checked=checked,
                    records_failed=scaled,
                    score_pct=round((checked - scaled) / checked * 100, 2),
                ))
        db.commit()
        print(f"  Run #{i}  {run_type:<12} {len(DEMO)} entities")


def main():
    with Session(engine) as db:
        if "--reset" in sys.argv:
            reset(db)
        elif db.query(ValRun).count() > 0:
            print("Runs already exist -- pass --reset to reseed.")
            return
        seed(db)
        n_db = sum(1 for t, _ in BATCHES if t == "db_fetch")
        n_up = sum(1 for t, _ in BATCHES if t == "file_upload")
        print(f"\n{db.query(ValRun).count()} runs, {db.query(ValMetric).count()} metrics.")
        print(f"{n_db} database batches, {n_up} file-upload batch. val_rules created: 0.")


if __name__ == "__main__":
    main()
