"""
test_violation_query.py
-----------------------
Proves the "show me the failing rows" SQL agrees with the engine.

For every approved rule that produced a metric in the latest run, this:
    1. generates the source-DB query (uncapped),
    2. runs it against source_db,
    3. compares the row count to val_metrics.records_failed for that rule.

They must be EQUAL. If they diverge, the query is lying to the user about
which rows failed -- which is worse than not offering the feature at all,
because they would go fix the wrong records.

    python test_violation_query.py            # check every rule
    python test_violation_query.py --sql 42   # print one rule's SQL

Exit code is non-zero if any rule disagrees, so it can gate a deploy.
"""

import os
import sys

# this script lives in extra/, the app package lives in backend/
BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
sys.path.insert(0, BACKEND)

import os
import sys
import urllib.parse

from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.database import ConfigSession, ResultsSession   # noqa: E402
from app.models import ENTITIES, ValRule                 # noqa: E402
from app.violation_query import build                    # noqa: E402


def source_engine():
    pwd = urllib.parse.quote_plus(os.getenv("DB_PASSWORD", ""))
    return create_engine(
        f"mysql+pymysql://{os.getenv('DB_USER')}:{pwd}@{os.getenv('DB_HOST')}:"
        f"{os.getenv('DB_PORT', '3306')}/{os.getenv('SOURCE_DB')}", future=True)


def latest_metrics():
    """rule_id -> (records_failed, entity, run_id) for each entity's newest run."""
    rdb = ResultsSession()
    rows = rdb.execute(text("""
        SELECT m.rule_id, m.records_failed, m.entity_name, m.run_id
        FROM val_metrics m
        JOIN val_runs r ON r.run_id = m.run_id
        JOIN (SELECT entity_name, MAX(run_id) AS mx FROM val_runs
              WHERE status = 'completed' GROUP BY entity_name) L
          ON L.entity_name = r.entity_name AND L.mx = r.run_id
    """)).all()
    rdb.close()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def main():
    if "--sql" in sys.argv:
        rid = int(sys.argv[sys.argv.index("--sql") + 1])
        cdb = ConfigSession()
        rule = cdb.get(ValRule, rid)
        print(build(rule, ENTITIES, limit=10)["sql"])
        cdb.close()
        return

    metrics = latest_metrics()
    cdb = ConfigSession()
    rules = cdb.query(ValRule).filter(ValRule.status == "APPROVED").all()
    cdb.close()
    eng = source_engine()

    print(f"{'rule':>5}  {'type':<22} {'object':<14} {'engine':>7} {'query':>7}  result")
    print("-" * 78)
    ok = bad = skipped = 0
    failures = []
    with eng.connect() as c:
        for r in sorted(rules, key=lambda x: x.rule_id):
            if r.rule_id not in metrics:
                skipped += 1
                continue
            expected, entity, _ = metrics[r.rule_id]
            try:
                sql = build(r, ENTITIES, limit=None)["sql"].rstrip(";")
                got = c.execute(text(f"SELECT COUNT(*) FROM (\n{sql}\n) AS v")).scalar()
            except Exception as exc:  # noqa: BLE001
                bad += 1
                failures.append((r.rule_id, r.rule_name, f"SQL ERROR: {str(exc)[:90]}"))
                print(f"{r.rule_id:>5}  {r.rule_type:<22} {entity:<14} {expected:>7} {'--':>7}  ERROR")
                continue
            match = (got == expected)
            ok += match
            if not match:
                bad += 1
                failures.append((r.rule_id, r.rule_name, f"engine {expected} vs query {got}"))
            print(f"{r.rule_id:>5}  {r.rule_type:<22} {entity:<14} {expected:>7} {got:>7}  "
                  f"{'match' if match else '*** MISMATCH ***'}")
    eng.dispose()

    print("-" * 78)
    print(f"  {ok} match, {bad} wrong, {skipped} skipped (no metric in the latest run)")
    if failures:
        print("\nFAILURES")
        for rid, name, why in failures:
            print(f"  #{rid} {name}: {why}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
