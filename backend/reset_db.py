"""
reset_db.py
-----------
Clears the RULES and the RUN RESULTS so you can reload them fresh.

    python reset_db.py            # show what would be deleted, change nothing
    python reset_db.py --apply    # actually delete
    python reset_db.py --apply --results-only    # keep rules, clear runs only

WHAT IT TOUCHES
    config_db   val_rules                                    (all rules)
    target_db   val_batches, val_runs, val_metrics,
                val_violations                               (all run history)

WHAT IT NEVER TOUCHES
    source_db   your actual data -- b2bcustomer, account, etc.
    the lookup tables (val_rule_types / severities / statuses)

Auto-increment counters are reset, so the next rule is #1 and the next run is
#1 rather than continuing from wherever the old data stopped.

You need this before re-seeding because the seed scripts ADD rules -- without
clearing you would end up with the old set and the new set side by side.
(`seed_rules_b2b.py --clear` handles just its own rules; this also wipes the
run history so the dashboard does not show results from deleted rules.)
"""

import sys

from sqlalchemy import text

from app.database import ConfigSession, ResultsSession

RESULT_TABLES = ["val_violations", "val_metrics", "val_runs", "val_batches"]


def main():
    apply = "--apply" in sys.argv
    results_only = "--results-only" in sys.argv

    c, r = ConfigSession(), ResultsSession()
    try:
        print("Current contents:")
        n_rules = c.execute(text("SELECT COUNT(*) FROM val_rules")).scalar()
        print(f"  config_db  val_rules        {n_rules}")
        counts = {}
        for t in RESULT_TABLES:
            counts[t] = r.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
            print(f"  target_db  {t:<16} {counts[t]}")

        if not apply:
            print("\nDry run -- nothing deleted. To actually clear:")
            print("    python reset_db.py --apply")
            return

        if not results_only:
            c.execute(text("DELETE FROM val_rules"))
            c.execute(text("ALTER TABLE val_rules AUTO_INCREMENT = 1"))
            c.commit()
            print(f"\n  deleted {n_rules} rules, next id will be 1")

        # children first -- violations and metrics reference runs
        for t in RESULT_TABLES:
            r.execute(text(f"DELETE FROM {t}"))
        for t in ["val_batches", "val_runs"]:
            r.execute(text(f"ALTER TABLE {t} AUTO_INCREMENT = 1"))
        r.commit()
        print(f"  cleared {sum(counts.values())} result rows, next run will be #1")
        print("\nsource_db was not touched. Next:")
        print("    python seed_rules_b2b.py --direct")
        print("    python seed_rules_account.py --direct")
    finally:
        c.close()
        r.close()


if __name__ == "__main__":
    main()
