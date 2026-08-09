"""
seed_rules_b2b.py
-----------------
20 rules for the four B2B objects, covering all 9 rule types.

Kept deliberately simple -- every rule should be obvious from its name alone.
No layered conditions, no clever regex: this set is meant to be read out loud
in a review, not decoded.

    python seed_rules_b2b.py            # create + approve all 20
    python seed_rules_b2b.py --clear    # delete existing B2B rules first
    python seed_rules_b2b.py --direct   # write to val_rules without the API
"""

import json
import os
import sys
import urllib.error
import urllib.request

API = f"http://localhost:{os.getenv('API_PORT', '8000')}/api"
OBJECTS = ["B2B Customer", "B2B Product", "B2B Price", "B2B SBG"]


def blank(c):
    """Empty OR null, in one readable expression."""
    return f"TRIM(COALESCE({c},'')) = ''"


# (name, object, element, rule_type, severity, definition)
RULES = [
    # ---------------- B2B Customer -----------------------------------------
    ("Customer name is required", "B2B Customer", "customer_name",
     "COMPLETENESS", "CRITICAL", {}),
    ("Email is required", "B2B Customer", "email",
     "COMPLETENESS", "CRITICAL", {}),
    ("Email must look like an email", "B2B Customer", "email",
     "VALIDITY", "ERROR", {"pattern": r"^[^@ ]+@[^@ ]+\.[^@ ]+$"}),
    ("Status must be Active or Inactive", "B2B Customer", "status",
     "ALLOWED_VALUES", "ERROR", {"allowedValues": ["Active", "Inactive"]}),
    ("Email must be unique", "B2B Customer", "email",
     "UNIQUENESS", "CRITICAL", {}),
    ("Customer SBG must exist", "B2B Customer", "sbg_id",
     "REFERENTIAL_INTEGRITY", "CRITICAL",
     {"lookupTable": "B2B SBG", "lookupField": "sbg_id"}),
    ("Active customers must have a region", "B2B Customer", "region",
     "CROSS_FIELD_SIMPLE", "ERROR",
     {"expression": f"status = 'Active' AND {blank('region')}"}),
    ("Customer name has no extra spaces", "B2B Customer", "customer_name",
     "CUSTOM_SQL", "WARNING",
     {"expression": "customer_name <> TRIM(customer_name)"}),

    # ---------------- B2B Product ------------------------------------------
    ("Product name is required", "B2B Product", "product_name",
     "COMPLETENESS", "CRITICAL", {}),
    ("Product code must look like PRD-1234", "B2B Product", "product_code",
     "VALIDITY", "ERROR", {"pattern": r"^PRD-[0-9]{4}$"}),
    ("Category must be Hardware, Software or Service", "B2B Product", "category",
     "ALLOWED_VALUES", "WARNING",
     {"allowedValues": ["Hardware", "Software", "Service"]}),
    ("Product code must be unique", "B2B Product", "product_code",
     "UNIQUENESS", "CRITICAL", {}),
    ("Product SBG must exist", "B2B Product", "sbg_id",
     "REFERENTIAL_INTEGRITY", "CRITICAL",
     {"lookupTable": "B2B SBG", "lookupField": "sbg_id"}),

    # ---------------- B2B Price --------------------------------------------
    ("Price is required", "B2B Price", "price_amount",
     "COMPLETENESS", "CRITICAL", {}),
    ("Price must be between 0 and 100000", "B2B Price", "price_amount",
     "RANGE", "CRITICAL", {"min": 0, "max": 100000}),
    ("Discount must be between 0 and 100", "B2B Price", "discount_pct",
     "RANGE", "ERROR", {"min": 0, "max": 100}),
    ("Priced product must exist", "B2B Price", "product_id",
     "REFERENTIAL_INTEGRITY", "CRITICAL",
     {"lookupTable": "B2B Product", "lookupField": "product_id"}),
    ("A product should have only one price", "B2B Price", "product_id",
     "AGGREGATION", "WARNING",
     {"aggregateFunction": "COUNT", "aggregateField": "*",
      "groupBy": ["product_id"], "operator": ">", "threshold": 1}),

    # ---------------- B2B SBG ----------------------------------------------
    ("SBG name is required", "B2B SBG", "sbg_name",
     "COMPLETENESS", "CRITICAL", {}),
    ("SBG name must be unique", "B2B SBG", "sbg_name",
     "UNIQUENESS", "WARNING", {}),
]


def call(url, body=None, method="POST"):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body else None,
        headers={"Content-Type": "application/json"}, method=method)
    try:
        return json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        return {"error": json.loads(e.read()).get("detail", str(e))}


def via_api(clear):
    if clear:
        n = 0
        for r in call(f"{API}/rules", method="GET"):
            if r["entity_name"] in OBJECTS:
                call(f"{API}/rules/{r['rule_id']}", method="DELETE")
                n += 1
        print(f"cleared {n} existing B2B rules\n")

    ok = err = 0
    by_type = {}
    for name, obj, field, rtype, sev, defn in RULES:
        r = call(f"{API}/rules", {
            "role": "owner", "rule_name": name, "entity_name": obj,
            "field_name": field, "rule_type": rtype, "severity": sev,
            "rule_definition": defn, "created_by": "prabhat",
        })
        if "error" in r:
            print(f"  FAIL  {rtype:<22} {name}\n        {r['error'][:100]}")
            err += 1
            continue
        rid = r["rule_id"]
        call(f"{API}/rules/{rid}/submit", {"actor": "prabhat", "role": "owner"})
        call(f"{API}/rules/{rid}/approve", {"actor": "prabhat", "role": "admin"})
        by_type[rtype] = by_type.get(rtype, 0) + 1
        print(f"  #{rid:<3} {rtype:<22} {obj:<14} {name}")
        ok += 1
    return ok, err, by_type


def direct(clear):
    """Insert straight into val_rules -- for when the backend is not running."""
    from datetime import datetime, timezone
    from app.database import ConfigSession
    from app.models import ENTITIES, ValRule
    from app.rule_compiler import (
        CompileContext, RuleCompileError, compile_rule, dimension_for,
        execution_type_for,
    )

    now = datetime.now(timezone.utc)
    db = ConfigSession()
    ok = err = 0
    by_type = {}
    try:
        if clear:
            n = db.query(ValRule).filter(ValRule.entity_name.in_(OBJECTS)).delete(
                synchronize_session=False)
            db.commit()
            print(f"cleared {n} existing B2B rules\n")
        for name, obj, field, rtype, sev, defn in RULES:
            meta = ENTITIES[obj]
            dj = json.dumps(defn)
            ctx = CompileContext(table="stg_x", columns=meta["columns"],
                                 lookup_table="stg_lookup", lookup_run_id=0)
            try:
                compile_rule(rtype, field, dj, ctx)
            except RuleCompileError as exc:
                print(f"  FAIL  {rtype:<22} {name}\n        {exc}")
                err += 1
                continue
            dim = dimension_for(rtype)
            db.add(ValRule(
                rule_name=name, source_system=meta["source_system"],
                rule_type=rtype, entity_name=obj, field_name=field,
                primary_key_field=meta["primary_key_field"],
                execution_type=execution_type_for(rtype), dimension=dim,
                rule_definition=dj, severity=sev, status="APPROVED", active=True,
                created_by="prabhat", created_date=now,
                approved_by="prabhat", approved_date=now))
            by_type[rtype] = by_type.get(rtype, 0) + 1
            print(f"       {rtype:<22} {dim:<13} {obj:<14} {name}")
            ok += 1
        db.commit()
    finally:
        db.close()
    return ok, err, by_type


def main():
    clear = "--clear" in sys.argv
    ok, err, by_type = (direct if "--direct" in sys.argv else via_api)(clear)
    print(f"\n{ok} rules created and APPROVED, {err} failed")
    for t, n in sorted(by_type.items()):
        print(f"  {t:<24} {n}")


if __name__ == "__main__":
    main()
