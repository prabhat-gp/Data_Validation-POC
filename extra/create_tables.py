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

import os
import sys

# this script lives in extra/, the app package lives in backend/
BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
sys.path.insert(0, BACKEND)

import sys

from sqlalchemy.orm import Session

from app.database import (
    CONFIG_URL, RESULTS_URL, SOURCE_URL, config_engine, results_engine, source_engine,
)
from app.models import (
    ConfigBase, ENTITIES, ResultsBase, StagingBase, ValRuleType, ValSeverity,
    ValStatus, staging_table_name,
)
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
    import re as _re
    mask = lambda u: _re.sub(r":[^:@]*@", ":****@", u)
    print(f"SOURCE  -> {mask(SOURCE_URL)}   (source tables + stg_*)")
    print(f"CONFIG  -> {mask(CONFIG_URL)}")
    print(f"RESULTS -> {mask(RESULTS_URL)}\n")

    if "--reset" in sys.argv:
        print("Dropping tables in both databases...")
        StagingBase.metadata.drop_all(source_engine)      # only stg_*, never source tables
        ResultsBase.metadata.drop_all(results_engine)
        ConfigBase.metadata.drop_all(config_engine)

    print("Creating tables...")
    ConfigBase.metadata.create_all(config_engine)
    ResultsBase.metadata.create_all(results_engine)
    StagingBase.metadata.create_all(source_engine)     # stg_* beside the source data

    with Session(config_engine) as db:
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

    print(f"\n  SOURCE_DB  : {sorted(StagingBase.metadata.tables)}")
    print(f"  CONFIG_DB  : {sorted(ConfigBase.metadata.tables)}")
    print(f"  RESULTS_DB : {sorted(ResultsBase.metadata.tables)}")
    print(f"\nEntities (from the ENTITIES constant, no catalog table):")
    for name, meta in ENTITIES.items():
        print(f"  {name:<16} -> {staging_table_name(name):<20} {len(meta['columns'])} columns")
    print("\nNo val_rules rows created -- add rules through the UI.")


if __name__ == "__main__":
    main()
