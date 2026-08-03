"""
create_tables.py
----------------
Creates the schema and seeds ONLY the lookup tables (rule types, severities,
statuses). The entity/column catalog is the ENTITIES constant in models.py --
there is no catalog table to seed any more.

NO val_rules rows are ever inserted here. Rules are created and approved by
users through the UI only.

Usage:
    python create_tables.py            # create tables + seed lookups
    python create_tables.py --reset    # drop everything and recreate
"""

import sys

from sqlalchemy.orm import Session

from app.database import engine
from app.models import Base, ENTITIES, ValRuleType, ValSeverity, ValStatus, staging_table_name
from app.rule_compiler import RULE_TYPE_DESCRIPTIONS, RULE_TYPE_META, RULE_TYPES

# Matches the reference workbook (Sheet3)
SEVERITIES = [
    ("INFO",     "Informational only"),
    ("WARNING",  "Should be fixed -- does not block"),
    ("ERROR",    "Data is wrong and needs correcting"),
    ("CRITICAL", "Must be fixed -- blocks downstream use"),
]

# DRAFT -> PENDING -> APPROVED. An approved rule that is edited goes to
# UPDATED (it must be re-approved). RETIRED is the end state and is what
# active=False means -- the rule no longer runs, but its history is kept.
STATUSES = [
    ("DRAFT",    "Being authored; not yet submitted"),
    ("PENDING",  "Submitted, awaiting approval"),
    ("APPROVED", "Approved -- WILL be executed by the engine"),
    ("REJECTED", "An approver refused it; edit and resubmit"),
    ("UPDATED",  "Edited after approval; needs re-approval before it runs again"),
    ("RETIRED",  "Switched off (active = false); no longer runs"),
]


def main():
    if "--reset" in sys.argv:
        print("Dropping all tables...")
        Base.metadata.drop_all(engine)

    print("Creating tables...")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        if db.query(ValRuleType).count() == 0:
            for code in RULE_TYPES:
                dimension, execution_type = RULE_TYPE_META[code]
                db.add(ValRuleType(
                    code=code,
                    description=RULE_TYPE_DESCRIPTIONS.get(code, code),
                    dimension=dimension,
                    execution_type=execution_type,
                ))
            print(f"Seeded {len(RULE_TYPES)} rule types.")

        if db.query(ValSeverity).count() == 0:
            for code, desc in SEVERITIES:
                db.add(ValSeverity(code=code, description=desc))
            print(f"Seeded {len(SEVERITIES)} severities.")

        if db.query(ValStatus).count() == 0:
            for code, desc in STATUSES:
                db.add(ValStatus(code=code, description=desc))
            print(f"Seeded {len(STATUSES)} statuses.")

        db.commit()

    print(f"\nEntities (from the ENTITIES constant, no catalog table):")
    for name, meta in ENTITIES.items():
        print(f"  {name:<16} -> {staging_table_name(name):<20} {len(meta['columns'])} columns")
    print("\nNo val_rules rows created -- add rules through the UI.")


if __name__ == "__main__":
    main()
