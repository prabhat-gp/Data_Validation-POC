"""
predict_failures.py
-------------------
Works out what every approved rule SHOULD find, by querying the SOURCE tables
directly, and compares it to what the engine recorded.

The engine is not consulted for the expected number. Each rule's definition is
re-expressed as an independent SQL predicate here, so a disagreement means one
of the two is wrong -- which is the only way to catch a rule that silently
measures nothing.

    python predict_failures.py            # predictions only, before a run
    python predict_failures.py --compare  # predictions vs the latest run
"""

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "backend"))

from sqlalchemy import text                                          # noqa: E402
from app.database import ConfigSession, ResultsSession, source_engine  # noqa: E402
from app.models import ENTITIES, ValMetric, ValRule, ValRun           # noqa: E402


def blank(c):
    return f"(`{c}` IS NULL OR TRIM(`{c}`) = '')"


def filled(c):
    return f"(`{c}` IS NOT NULL AND TRIM(`{c}`) <> '')"


def predicate(rule, cfg, meta):
    """
    The WHERE clause that identifies this rule's failures on the SOURCE table.
    Mirrors rule_compiler but is written independently -- that is the point.
    Returns None for rule types that need a join or a group.
    """
    col = rule.field_name
    t = rule.rule_type

    if t == "COMPLETENESS":
        return blank(col)
    if t == "VALIDITY":
        pat = cfg.get("pattern", "")
        return f"{filled(col)} AND NOT (`{col}` REGEXP :pat_{rule.rule_id})", {f"pat_{rule.rule_id}": pat}
    if t == "ALLOWED_VALUES":
        vals = cfg.get("allowedValues") or []
        if not vals:
            return None
        lst = ", ".join(f"'{v}'" for v in vals)
        return f"{filled(col)} AND `{col}` NOT IN ({lst})"
    if t == "CROSS_FIELD_SIMPLE" or t == "CUSTOM_SQL":
        return cfg.get("expression")
    return None


def main():
    compare = "--compare" in sys.argv

    cdb = ConfigSession()
    rules = (cdb.query(ValRule)
             .filter(ValRule.status == "APPROVED", ValRule.active == True)  # noqa: E712
             .order_by(ValRule.entity_name, ValRule.rule_id).all())
    cdb.close()

    actual = {}
    scanned = {}
    if compare:
        rdb = ResultsSession()
        for ent in {r.entity_name for r in rules}:
            run = (rdb.query(ValRun)
                   .filter(ValRun.entity_name == ent, ValRun.status == "completed")
                   .order_by(ValRun.run_id.desc()).first())
            if not run:
                continue
            scanned[ent] = run.records_scanned or 0
            for m in rdb.query(ValMetric).filter(ValMetric.run_id == run.run_id).all():
                actual[m.rule_id] = m.records_failed
        rdb.close()

    hdr = f"  {'object':13} {'rule':44} {'expected':>10}"
    if compare:
        hdr += f" {'actual':>10}  ok"
    print(hdr)
    print("  " + "-" * (len(hdr) + 4))

    mismatches, unchecked = [], []
    with source_engine.connect() as conn:
        totals = {}
        for ent, meta in ENTITIES.items():
            try:
                totals[ent] = conn.execute(
                    text(f"SELECT COUNT(*) FROM `{meta['source_object_name']}`")).scalar()
            except Exception:               # noqa: BLE001
                totals[ent] = None

        current = None
        for r in rules:
            meta = ENTITIES.get(r.entity_name)
            if not meta or totals.get(r.entity_name) is None:
                continue
            if r.entity_name != current:
                current = r.entity_name
                print(f"\n  {current}  ({totals[current]:,} rows)")

            cfg = json.loads(r.rule_definition or "{}")
            tbl = meta["source_object_name"]
            expected = None

            if r.rule_type == "UNIQUENESS":
                expected = conn.execute(text(
                    f"SELECT COALESCE(SUM(c),0) FROM (SELECT COUNT(*) c FROM `{tbl}` "
                    f"WHERE {filled(r.field_name)} GROUP BY `{r.field_name}` "
                    "HAVING COUNT(*) > 1) d")).scalar()
            elif r.rule_type == "REFERENTIAL_INTEGRITY":
                lk = ENTITIES.get(cfg.get("lookupTable") or "")
                lf = cfg.get("lookupField")
                if lk and lf:
                    expected = conn.execute(text(
                        f"SELECT COUNT(*) FROM `{tbl}` o LEFT JOIN "
                        f"`{lk['source_object_name']}` p ON o.`{r.field_name}` = p.`{lf}` "
                        f"WHERE {filled(r.field_name).replace('`', 'o.`', 1)} "
                        f"AND p.`{lf}` IS NULL")).scalar()
            else:
                pred = predicate(r, cfg, meta)
                params = {}
                if isinstance(pred, tuple):
                    pred, params = pred
                if pred:
                    expected = conn.execute(
                        text(f"SELECT COUNT(*) FROM `{tbl}` WHERE {pred}"), params).scalar()

            name = r.rule_name[:44]
            if expected is None:
                unchecked.append(f"{r.entity_name}.{r.field_name} ({r.rule_type})")
                line = f"  {'':13} {name:44} {'—':>10}"
            else:
                line = f"  {'':13} {name:44} {expected:>10,}"
            if compare:
                a = actual.get(r.rule_id)
                if a is None:
                    line += f" {'—':>10}"
                elif expected is None:
                    line += f" {a:>10,}"
                else:
                    ok = "ok" if a == expected else "MISMATCH"
                    line += f" {a:>10,}  {ok}"
                    if a != expected:
                        mismatches.append((r.rule_id, r.entity_name, r.rule_name, expected, a))
            print(line)

    print()
    if unchecked:
        print(f"  {len(unchecked)} rule(s) not independently checkable here "
              f"(need a group or a join this script does not model):")
        for u in unchecked:
            print(f"    {u}")
    if compare:
        print(f"\n  {len(mismatches)} mismatch(es)")
        for rid, ent, nm, exp, act in mismatches:
            print(f"    #{rid} {ent}.{nm}: expected {exp:,}, engine recorded {act:,}")


if __name__ == "__main__":
    main()
