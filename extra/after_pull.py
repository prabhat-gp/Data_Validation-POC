"""
after_pull.py
-------------
Run this once on a machine that has just pulled changes. It brings the
databases in line with the code and reports whether the result is usable.

    cd extra
    python after_pull.py            # show what it would do, change nothing
    python after_pull.py --apply    # do it

WHAT IT DOES, IN ORDER
    1. create_tables   builds any table the code declares that the database
                       does not have yet -- on a machine that predates the
                       Hybris work that is stg_b2b_customer / stg_b2b_unit /
                       stg_address. Never touches an existing table.
    2. migrate_db      reconciles tables that DO exist: missing columns, the
                       TEXT -> VARCHAR(255) change on staging, and the
                       declared indexes. Drops no column and no row.
    3. seed rules      loads the 39 approved Hybris rules. Account rules are
                       left alone unless --reseed-account is given.
    4. verify          counts rules per object, compiles every one of them,
                       and confirms the staging schema matches the models.

Safe to run twice -- every step is idempotent. Steps 1-3 are skipped without
--apply; step 4 always runs, so a dry run still tells you where you stand.
"""

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(os.path.dirname(HERE), "backend")
sys.path.insert(0, BACKEND)

PY = sys.executable


def run(script, args, apply_it):
    cmd = [PY, os.path.join(HERE, script)] + args
    print(f"\n{'='*72}\n  {' '.join([os.path.basename(PY)] + cmd[1:])}\n{'='*72}")
    if not apply_it:
        print("  (skipped -- dry run)")
        return True
    r = subprocess.run(cmd, cwd=HERE)
    if r.returncode != 0:
        print(f"\n  !! {script} exited {r.returncode}")
    return r.returncode == 0


def verify():
    """Read-only. Everything here would fail loudly if the DB were wrong."""
    print(f"\n{'='*72}\n  VERIFY\n{'='*72}")
    problems = []

    from sqlalchemy import inspect
    from app.database import config_engine, results_engine, source_engine
    from app.models import (
        ConfigBase, ENTITIES, ResultsBase, StagingBase, ValRule,
        staging_table_name,
    )
    from app.database import ConfigSession
    from app.rule_compiler import (
        CompileContext, RuleCompileError, compile_rule, referenced_entity,
    )

    # --- connections ------------------------------------------------------
    for label, eng in (("source", source_engine), ("config", config_engine),
                       ("results", results_engine)):
        try:
            with eng.connect():
                pass
            print(f"  {label:8} {eng.url.database:14} connected")
        except Exception as exc:
            problems.append(f"{label} database unreachable: {exc}")
            print(f"  {label:8} UNREACHABLE -- {exc}")

    # --- tables + staging column types ------------------------------------
    print()
    insp = inspect(source_engine)
    have = set(insp.get_table_names())
    for entity in ENTITIES:
        t = staging_table_name(entity)
        if t not in have:
            problems.append(f"missing staging table {t}")
            print(f"  {t:22} MISSING")
            continue
        cols = {c["name"]: str(c["type"]).upper() for c in insp.get_columns(t)}
        texty = [c for c, ty in cols.items() if "TEXT" in ty or "CLOB" in ty]
        ix = len([i for i in insp.get_indexes(t) if i.get("name")])
        note = f"{ix} indexes"
        if texty:
            # not fatal: an orphan column from an older schema is harmless,
            # a DECLARED column still being TEXT is not
            declared = set(ENTITIES[entity]["columns"])
            bad = [c for c in texty if c in declared]
            if bad:
                problems.append(f"{t}: declared column(s) still TEXT: {bad}")
                note += f", TEXT columns {bad}"
            else:
                note += f", orphan TEXT columns {texty} (harmless)"
        print(f"  {t:22} {note}")

    # --- rules ------------------------------------------------------------
    print()
    db = ConfigSession()
    try:
        rules = db.query(ValRule).filter(ValRule.active == True).all()  # noqa: E712
        by_entity = {}
        for r in rules:
            by_entity.setdefault(r.entity_name, []).append(r)
        for entity in ENTITIES:
            rs = by_entity.get(entity, [])
            approved = len([r for r in rs if r.status == "APPROVED"])
            print(f"  {entity:16} {len(rs):>3} rules, {approved} approved")
            if rs and approved == 0:
                problems.append(f"{entity}: rules exist but none are APPROVED")

        # every rule must still compile against the current catalog
        print()
        failed = 0
        for r in rules:
            meta = ENTITIES.get(r.entity_name)
            if meta is None:
                problems.append(f"rule {r.rule_id} targets unknown object {r.entity_name!r}")
                failed += 1
                continue
            lk = referenced_entity(r.rule_type, r.rule_definition)
            lkm = ENTITIES.get(lk) if lk else None
            ctx = CompileContext(
                table=staging_table_name(r.entity_name), columns=meta["columns"],
                lookup_table=staging_table_name(lk) if lkm else None,
                lookup_run_id=0,
                lookup_key_field=lkm["primary_key_field"] if lkm else None,
                lookup_columns=lkm["columns"] if lkm else None)
            try:
                compile_rule(r.rule_type, r.field_name, r.rule_definition, ctx)
            except RuleCompileError as exc:
                # This is the failure mode that does NOT announce itself at run
                # time -- the engine records 0 failed / 0.0% and moves on.
                problems.append(f"rule {r.rule_id} '{r.rule_name}' will not compile: {exc}")
                print(f"  WILL NOT COMPILE  #{r.rule_id} {r.rule_name}\n      {exc}")
                failed += 1
        print(f"  {len(rules) - failed}/{len(rules)} active rules compile")
    finally:
        db.close()

    print(f"\n{'='*72}")
    if problems:
        print(f"  {len(problems)} PROBLEM(S):")
        for p in problems:
            print(f"    - {p}")
        return 1
    print("  All good. Start the backend and run a batch.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually make changes")
    ap.add_argument("--reseed-account", action="store_true",
                    help="also reload the 20 Account rules")
    args = ap.parse_args()

    if not args.apply:
        print("DRY RUN -- nothing will be changed. Re-run with --apply.\n")

    run("create_tables.py", [], args.apply)
    run("migrate_db.py", ["--apply"], args.apply)
    run("seed_rules_hybris.py", ["--direct", "--clear"], args.apply)
    if args.reseed_account:
        run("seed_rules_account.py", ["--direct", "--clear"], args.apply)

    code = verify()
    if not args.apply:
        print("\n  (dry run -- re-run with --apply to make the changes above)")
    sys.exit(code)


if __name__ == "__main__":
    main()
