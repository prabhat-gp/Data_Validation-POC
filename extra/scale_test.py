"""
scale_test.py
-------------
End-to-end timing of the real engine against a generated dataset, in throwaway
databases. Nothing here touches source_db / config_db / results_db.

It builds scale_src / scale_cfg / scale_res, fills two SOURCE tables, seeds a
rule of every type, then runs the actual three-phase batch -- stage_run,
validate_run, clear_run_staging -- and reports where the time went.

The point is to answer "does this hold at N rows" with measurements from the
code that ships, not from a hand-written query that resembles it.

Usage:
    python scale_test.py                      # 1,000,000 customers / 200,000 units
    python scale_test.py --rows 200000
    python scale_test.py --rows 1000000 --keep    # leave the scratch DBs behind
    python scale_test.py --batch-sizes 5000,25000 # compare streaming batch sizes
"""

import argparse
import os
import random
import sys
import time
from urllib.parse import quote_plus

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
sys.path.insert(0, BACKEND)

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv(os.path.join(BACKEND, ".env"))

SRC, CFG, RES = "scale_src", "scale_cfg", "scale_res"

# Point the app at the scratch databases BEFORE app.database is imported --
# it resolves its engines at import time. load_dotenv does not override
# variables that are already set, so these win.
os.environ["SOURCE_DB"], os.environ["CONFIG_DB"], os.environ["RESULTS_DB"] = SRC, CFG, RES
os.environ.pop("SOURCE_DATABASE_URL", None)
os.environ.pop("CONFIG_DATABASE_URL", None)
os.environ.pop("RESULTS_DATABASE_URL", None)


def _root_url():
    user = os.getenv("DB_USER", "root")
    pwd = quote_plus(os.getenv("DB_PASSWORD", ""))
    host, port = os.getenv("DB_HOST", "localhost"), os.getenv("DB_PORT", "3306")
    return f"mysql+pymysql://{user}:{pwd}@{host}:{port}/"


def make_databases():
    eng = create_engine(_root_url(), future=True)
    with eng.connect() as c:
        for db in (SRC, CFG, RES):
            c.execute(text(f"DROP DATABASE IF EXISTS {db}"))
            c.execute(text(f"CREATE DATABASE {db}"))
        c.commit()
    eng.dispose()


# ---------------------------------------------------------------------------
# source data
# ---------------------------------------------------------------------------
# Shaped like the real Hybris dump: 10-digit unit codes, +CC phone numbers,
# DD.MM.YYYY timestamps. Roughly a quarter of the rows carry a planted defect
# so every rule has something to find and the violation writer is exercised.
CURRENCIES = ["USD", "USD", "USD", "EUR", "gbp"]
LANGS = ["en", "en", "en", "de", None]


def build_source(units: int, customers: int, log):
    from app.database import source_engine

    with source_engine.begin() as c:
        c.execute(text("""
            CREATE TABLE b2bunit (
              pk VARCHAR(20) PRIMARY KEY, uid VARCHAR(32), name VARCHAR(120),
              locName_en VARCHAR(120), accountType VARCHAR(40), active VARCHAR(10),
              orderBlock VARCHAR(10), sfdcServiceLayer VARCHAR(40), addresses VARCHAR(32))"""))
        c.execute(text("""
            CREATE TABLE b2bcustomer (
              pk VARCHAR(20) PRIMARY KEY, originalUid VARCHAR(32), name VARCHAR(120),
              email VARCHAR(160), phone VARCHAR(40), active VARCHAR(10),
              loginDisabled VARCHAR(10), creationtime VARCHAR(32),
              defaultB2BUnit VARCHAR(32), hwCustomerType VARCHAR(60),
              toolAccess VARCHAR(40), sessionCurrency VARCHAR(10),
              sessionLanguage VARCHAR(10), sfdcContactId VARCHAR(32))"""))

    rnd = random.Random(20260812)
    t0 = time.time()
    rows, CH = [], 25_000
    with source_engine.begin() as c:
        for i in range(units):
            rows.append({
                "pk": f"88{i:011d}", "uid": f"{i:010d}",
                "name": f"UNIT {i}" if i % 7 else f"DO NOT USE UNIT {i}",
                "loc": f"UNIT {i}" if i % 7 else f"DO NOT USE UNIT {i}",
                "at": rnd.choice(["Dealer", "OEM", "Distributor", "01", None]),
                "ac": "True", "ob": rnd.choice(["True", "False"]),
                "sl": rnd.choice(["Standard", "Superior", None]),
                "ad": f"879{i:010d}",
            })
            if len(rows) >= CH:
                c.execute(text("INSERT INTO b2bunit VALUES (:pk,:uid,:name,:loc,:at,:ac,:ob,:sl,:ad)"), rows)
                rows = []
        if rows:
            c.execute(text("INSERT INTO b2bunit VALUES (:pk,:uid,:name,:loc,:at,:ac,:ob,:sl,:ad)"), rows)
    log(f"  b2bunit      {units:>9,} rows  {time.time()-t0:6.1f}s")

    t0 = time.time()
    rows = []
    with source_engine.begin() as c:
        for i in range(customers):
            bad = i % 4 == 0                       # planted defects
            rows.append({
                "pk": f"87{i:011d}",
                "ou": f"{i:016x}" if not bad or i % 8 else f"{i:04d}linj",
                "nm": None if bad and i % 12 == 0 else f"Customer {i}",
                "em": f"user{i}@example.com" if not bad else f"user{i}.example.com",
                "ph": f"+1 {5550000000+i}" if not bad else f"({i%900+100}) 555-0100",
                "ac": rnd.choice(["True", "False"]), "ld": rnd.choice(["True", "False"]),
                "ct": f"{(i%28)+1:02d}.{(i%12)+1:02d}.2025 10:{i%60:02d}:00",
                # ~15% point at a unit that does not exist
                "du": f"{rnd.randrange(units):010d}" if i % 7 else f"{9_000_000+i:010d}",
                "hw": "EXTERNAL:HoneywellCustomerType", "ta": "Online Ordering",
                "cu": rnd.choice(CURRENCIES), "sl": rnd.choice(LANGS),
                "sf": f"003{i:015d}",
            })
            if len(rows) >= CH:
                c.execute(text("INSERT INTO b2bcustomer VALUES "
                               "(:pk,:ou,:nm,:em,:ph,:ac,:ld,:ct,:du,:hw,:ta,:cu,:sl,:sf)"), rows)
                rows = []
        if rows:
            c.execute(text("INSERT INTO b2bcustomer VALUES "
                           "(:pk,:ou,:nm,:em,:ph,:ac,:ld,:ct,:du,:hw,:ta,:cu,:sl,:sf)"), rows)
    log(f"  b2bcustomer  {customers:>9,} rows  {time.time()-t0:6.1f}s")


# ---------------------------------------------------------------------------
# rules -- one of every type, so every compiler is on the clock
# ---------------------------------------------------------------------------
RULES = [
    ("B2B Customer", "name", "COMPLETENESS", "CRITICAL", {}),
    ("B2B Customer", "email", "VALIDITY", "ERROR", {"pattern": r"^[^@]+@[^@]+\.[A-Za-z]{2,}$"}),
    ("B2B Customer", "sessionCurrency", "ALLOWED_VALUES", "WARNING",
     {"allowedValues": ["USD", "EUR", "GBP"]}),
    ("B2B Customer", "sessionLanguage", "CROSS_FIELD_SIMPLE", "WARNING",
     {"expression": "sessionCurrency = 'USD' AND (sessionLanguage IS NULL "
                    "OR TRIM(sessionLanguage) = '')"}),
    ("B2B Customer", "originalUid", "UNIQUENESS", "CRITICAL", {}),
    ("B2B Customer", "name", "CUSTOM_SQL", "WARNING", {"expression": "name <> TRIM(name)"}),
    # The entity key is "lookupTable" (or "ref_entity_name") -- see
    # rule_compiler.referenced_entity. Any other spelling leaves the lookup
    # entity un-staged, the rule fails to compile, and the metric is silently
    # recorded as 0 failed / 0.0%.
    ("B2B Customer", "defaultB2BUnit", "REFERENTIAL_INTEGRITY", "CRITICAL",
     {"lookupTable": "B2B Unit", "lookupField": "uid"}),
    ("B2B Customer", "defaultB2BUnit", "AGGREGATION", "WARNING",
     {"aggregateFunction": "COUNT", "aggregateField": "*", "groupBy": ["defaultB2BUnit"],
      "operator": ">", "threshold": 20,
      "filter": {"logic": "AND", "conditions": [
          {"field": "defaultB2BUnit", "operator": "is_not_null"}]}}),
    ("B2B Unit", "uid", "UNIQUENESS", "CRITICAL", {}),
    ("B2B Unit", "name", "CUSTOM_SQL", "WARNING",
     {"expression": "UPPER(name) LIKE '%DO NOT USE%'"}),
    ("B2B Unit", "accountType", "COMPLETENESS", "WARNING", {}),
]


def seed_rules(log):
    import json
    from datetime import timezone
    from app.database import ConfigSession
    from app.models import ENTITIES, ValRule
    from app.rule_compiler import dimension_for, execution_type_for
    from app.validation_engine import utcnow

    db = ConfigSession()
    now = utcnow().replace(tzinfo=timezone.utc)
    for entity, field, rtype, sev, defn in RULES:
        meta = ENTITIES[entity]
        db.add(ValRule(
            rule_name=f"{entity}.{field} {rtype}", source_system=meta["source_system"],
            rule_type=rtype, dimension=dimension_for(rtype), entity_name=entity,
            field_name=field, primary_key_field=meta["primary_key_field"],
            execution_type=execution_type_for(rtype), rule_definition=json.dumps(defn),
            severity=sev, status="APPROVED", active=True, created_by="scale_test",
            created_date=now, approved_by="scale_test", approved_date=now))
    db.commit()
    db.close()
    log(f"  {len(RULES)} rules seeded")


# ---------------------------------------------------------------------------
def run_batch(entities, log, label=""):
    """The real three-phase batch, phase by phase, on the clock."""
    from app.database import ResultsSession
    from app.models import ValBatch, ValRun
    from app.validation_engine import (
        clear_run_staging, finish_run, stage_run, utcnow, validate_run,
    )

    db = ResultsSession()
    batch = ValBatch(batch_name=f"scale{label}", run_type="db_fetch",
                     triggered_by="scale_test", started_at=utcnow())
    db.add(batch)
    db.commit()
    runs = []
    for ent in entities:
        r = ValRun(batch_id=batch.batch_id, entity_name=ent, run_type="db_fetch",
                   status="pending", started_at=utcnow())
        db.add(r)
        runs.append(r)
    db.commit()

    timings, staged = {}, {}
    t0 = time.time()
    for r in runs:
        s = time.time()
        n = stage_run(db, r.run_id, "db_fetch")
        staged[r.entity_name] = r.run_id
        timings[f"stage {r.entity_name}"] = (time.time() - s, n)
    timings["PHASE 1 stage"] = (time.time() - t0, sum(v[1] for k, v in timings.items()
                                                      if k.startswith("stage ")))
    t0 = time.time()
    for r in runs:
        s = time.time()
        validate_run(db, r.run_id, staged)
        finish_run(db, r.run_id)
        timings[f"validate {r.entity_name}"] = (time.time() - s, 0)
    timings["PHASE 2 validate"] = (time.time() - t0, 0)

    t0 = time.time()
    for r in runs:
        clear_run_staging(db, r.run_id)
    timings["PHASE 3 clear"] = (time.time() - t0, 0)

    ids = [r.run_id for r in runs]
    db.close()
    return timings, ids


def per_rule_report(run_ids, log):
    from app.database import ResultsSession
    from app.models import ValMetric, ValRun
    db = ResultsSession()
    log(f"\n  {'rule':46} {'checked':>10} {'failed':>10}  score")
    log("  " + "-" * 78)
    for rid in run_ids:
        run = db.get(ValRun, rid)
        for m in db.query(ValMetric).filter(ValMetric.run_id == rid).order_by(
                ValMetric.metric_id).all():
            log(f"  {m.entity_name + '.' + m.field_name:30} {m.dimension:15} "
                f"{m.records_checked:>10,} {m.records_failed:>10,}  {m.score_pct:5.1f}%")
        log(f"  {'-> ' + run.entity_name:46} scanned {run.records_scanned:,}, "
            f"records affected {run.records_affected:,}")
    total = db.query(ValMetric).count()
    db.close()
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=1_000_000)
    ap.add_argument("--units", type=int, default=None)
    ap.add_argument("--batch-sizes", default="")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    units = args.units if args.units is not None else max(1000, args.rows // 5)

    def log(s=""):
        print(s, flush=True)

    log(f"\n{'='*80}\nSCALE TEST  {args.rows:,} customers / {units:,} units\n{'='*80}")
    make_databases()

    from app.database import config_engine, results_engine, source_engine
    from app.models import ConfigBase, ResultsBase, StagingBase
    StagingBase.metadata.create_all(source_engine)
    ConfigBase.metadata.create_all(config_engine)
    ResultsBase.metadata.create_all(results_engine)
    log("\n-- source data --")
    build_source(units, args.rows, log)
    log("\n-- rules --")
    seed_rules(log)

    sizes = [int(x) for x in args.batch_sizes.split(",") if x.strip()]
    if sizes:
        log("\n-- streaming BATCH_SIZE comparison (INSERT..SELECT disabled) --")
        compare_batch_sizes(sizes, args.rows, log)

    log("\n-- end-to-end batch --")
    timings, run_ids = run_batch(["B2B Unit", "B2B Customer"], log)
    for k in ("stage B2B Unit", "stage B2B Customer", "PHASE 1 stage",
              "validate B2B Unit", "validate B2B Customer", "PHASE 2 validate",
              "PHASE 3 clear"):
        secs, n = timings[k]
        log(f"  {k:28} {secs:8.1f}s" + (f"   {n:,} rows" if n else ""))
    total = sum(timings[k][0] for k in ("PHASE 1 stage", "PHASE 2 validate", "PHASE 3 clear"))
    log(f"  {'TOTAL':28} {total:8.1f}s")

    log("\n-- per rule --")
    per_rule_report(run_ids, log)

    if not args.keep:
        eng = create_engine(_root_url(), future=True)
        with eng.connect() as c:
            for db in (SRC, CFG, RES):
                c.execute(text(f"DROP DATABASE IF EXISTS {db}"))
            c.commit()
        log("\nscratch databases dropped (--keep to retain)")
    else:
        log(f"\nkept: {SRC} / {CFG} / {RES}")


def compare_batch_sizes(sizes, rows, log):
    """
    Times the STREAMING path only. INSERT..SELECT is bypassed here on purpose
    -- BATCH_SIZE has no effect on it, and the comparison is about what the
    remote-source path costs.
    """
    from app.database import ResultsSession, StagingSession
    from app.models import ValBatch, ValRun
    from app.validation_engine import utcnow
    from app import ingestion

    db = ResultsSession()
    original = ingestion.BATCH_SIZE
    real_same_server = ingestion._same_server
    ingestion._same_server = lambda a, b: False       # force the streaming path
    try:
        for size in sizes:
            ingestion.BATCH_SIZE = size
            # one batch per size -- val_runs is unique on (batch_id, entity_name)
            batch = ValBatch(batch_name=f"batchsize {size}", run_type="db_fetch",
                             triggered_by="scale_test", started_at=utcnow())
            db.add(batch); db.commit()
            run = ValRun(batch_id=batch.batch_id, entity_name="B2B Customer",
                         run_type="db_fetch", status="pending", started_at=utcnow())
            db.add(run); db.commit()
            sdb = StagingSession()
            t0 = time.time()
            n = ingestion.stage_from_db(sdb, run.run_id, "B2B Customer", "Hybris")
            secs = time.time() - t0
            ingestion.clear_staging(sdb, "B2B Customer", run.run_id)
            sdb.close()
            log(f"  BATCH_SIZE {size:>6,}  {secs:7.1f}s  {n/secs:>9,.0f} rows/sec")
    finally:
        ingestion.BATCH_SIZE = original
        ingestion._same_server = real_same_server
        db.close()


if __name__ == "__main__":
    main()
