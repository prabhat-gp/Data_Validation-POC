"""
cleanup_source.py
-----------------
Finds tables in source_db that the code no longer knows about, and drops them
on request. Nothing the catalog declares is ever touched.

Every table in source_db falls into one of three buckets:

    source      a table an ENTITIES entry points at (account, b2bcustomer, ...)
    staging     an stg_* table this app generates for a declared object
    ORPHAN      everything else -- old datasets, stale staging from objects
                that were removed, scratch tables

Only ORPHANs are candidates. They are listed with their row count so you can
see what you would lose before saying yes.

    python cleanup_source.py            # list, drop nothing
    python cleanup_source.py --apply    # drop the orphans
"""

import os
import sys

# this script lives in extra/, the app package lives in backend/
BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
sys.path.insert(0, BACKEND)

from sqlalchemy import inspect, text

from app.database import source_engine
from app.models import ENTITIES, staging_table_name


def main():
    do_it = "--apply" in sys.argv

    declared_source = {m["source_object_name"].lower(): name
                       for name, m in ENTITIES.items()}
    declared_staging = {staging_table_name(name).lower(): name for name in ENTITIES}

    insp = inspect(source_engine)
    tables = sorted(insp.get_table_names())

    keep, orphans = [], []
    for t in tables:
        low = t.lower()
        if low in declared_source:
            keep.append((t, f"source table for {declared_source[low]}"))
        elif low in declared_staging:
            keep.append((t, f"staging for {declared_staging[low]}"))
        else:
            orphans.append(t)

    print(f"\n=== {source_engine.url.database} -- {len(tables)} tables ===\n")
    print("  KEEP")
    for t, why in keep:
        print(f"    {t:24} {why}")
    if not keep:
        print("    (nothing declared is present -- is this the right database?)")

    # A NAME match is not a SCHEMA match. An old dataset's `b2bcustomer` has
    # the right name and the wrong columns, so it survives the orphan check
    # above and then fails at staging time with a column that does not exist.
    # Check the declared CDEs are really there.
    mismatched = []
    for t, why in keep:
        entity = declared_source.get(t.lower())
        if entity is None:
            continue
        have = {c["name"].lower() for c in insp.get_columns(t)}
        meta = ENTITIES[entity]
        want = [meta["primary_key_field"]] + meta["columns"]
        missing = [c for c in want if c.lower() not in have]
        if missing:
            mismatched.append((t, entity, missing))

    if mismatched:
        print("\n  WRONG SCHEMA -- right table name, missing declared columns")
        for t, entity, missing in mismatched:
            print(f"    {t:24} {entity}: missing {missing}")
        print("\n    These are almost certainly tables from the OLD dataset that happen")
        print("    to share a name. Staging them would fail on the first missing column.")
        print("    Drop and re-import them from the current dump before running.")

    if not orphans:
        print("\n  No orphan tables. Nothing to clean up.")
        return

    print("\n  ORPHAN -- not referenced by any ENTITIES entry")
    with source_engine.connect() as c:
        for t in orphans:
            try:
                n = c.execute(text(f"SELECT COUNT(*) FROM `{t}`")).scalar()
            except Exception:                       # noqa: BLE001
                n = "?"
            print(f"    {t:24} {n if isinstance(n, str) else format(n, ',')} rows")

    if not do_it:
        print(f"\n  {len(orphans)} table(s) would be DROPPED. Re-run to do it:")
        print("      python cleanup_source.py --apply")
        return

    print()
    with source_engine.begin() as c:
        # FK checks off: an old dataset may have constraints between its own
        # tables, and drop order should not decide whether cleanup succeeds.
        c.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for t in orphans:
            c.execute(text(f"DROP TABLE IF EXISTS `{t}`"))
            print(f"  dropped {t}")
        c.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
    print(f"\n{len(orphans)} table(s) dropped. {len(keep)} kept.")


if __name__ == "__main__":
    main()
