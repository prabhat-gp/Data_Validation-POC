"""
seed_rules_account.py
---------------------
20 rules for the Account object -- one for every declared element, so Rule
Coverage reads 16 of 16.

Kept deliberately simple: each rule should be obvious from its name.

TWO RULE TYPES ARE NOT USED HERE, AND WHY
    REFERENTIAL_INTEGRITY  needs a second object staged in the same run to
                           join against. Account is standalone -- there is no
                           Industry or Country master table.
    RANGE                  needs a numeric column. All 17 Account columns are
                           text; postal codes are international and contain
                           letters, so a numeric range would measure nothing.

    CROSS_FIELD_SIMPLE IS usable -- it compares fields inside ONE record, so a
    single table is all it needs. It is REFERENTIAL_INTEGRITY that needs two.

    python seed_rules_account.py            # create + approve all 20
    python seed_rules_account.py --clear    # delete existing Account rules first
    python seed_rules_account.py --direct   # write to val_rules without the API
"""

import os
import sys

# this script lives in extra/, the app package lives in backend/
BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
sys.path.insert(0, BACKEND)

import json
import os
import sys
import urllib.error
import urllib.request

API = f"http://localhost:{os.getenv('API_PORT', '8000')}/api"
ENTITY = "Account"


def blank(c):
    """Empty OR null, in one readable expression."""
    return f"TRIM(COALESCE({c},'')) = ''"


def filled(c):
    return f"TRIM(COALESCE({c},'')) <> ''"


# (name, element, rule_type, severity, definition)
RULES = [
    # ---- required fields -------------------------------------------------
    ("Account name is required", "Name",
     "COMPLETENESS", "CRITICAL", {}),
    ("Billing country is required", "BillingCountry",
     "COMPLETENESS", "CRITICAL", {}),
    ("Account type is required", "Type",
     "COMPLETENESS", "ERROR", {}),
    ("Industry is required", "Industry",
     "COMPLETENESS", "WARNING", {}),
    ("Region is required", "Region__c",
     "COMPLETENESS", "WARNING", {}),
    ("Billing street is required", "BillingStreet",
     "COMPLETENESS", "WARNING", {}),
    ("Billing city is required", "BillingCity",
     "COMPLETENESS", "WARNING", {}),

    # ---- format ----------------------------------------------------------
    ("Website must start with http", "Website",
     "VALIDITY", "ERROR", {"pattern": r"^https?://"}),
    ("Phone may contain only + and digits", "Phone",
     "VALIDITY", "WARNING", {"pattern": r"^\+?[0-9]+$"}),
    ("Billing postal code has no odd characters", "BillingPostalCode",
     "VALIDITY", "WARNING", {"pattern": r"^[A-Za-z0-9 -]+$"}),
    ("Shipping postal code has no odd characters", "ShippingPostalCode",
     "VALIDITY", "WARNING", {"pattern": r"^[A-Za-z0-9 -]+$"}),

    # ---- known values ----------------------------------------------------
    ("Account type must be a known value", "Type",
     "ALLOWED_VALUES", "ERROR",
     {"allowedValues": ["Owner/Operator", "Product/Service Provider",
                        "Customer", "Partner", "Prospect", "Distributor"]}),
    ("Region must be AMER, EMEA, APAC or LATAM", "Region__c",
     "ALLOWED_VALUES", "WARNING",
     {"allowedValues": ["AMER", "EMEA", "APAC", "LATAM"]}),

    # ---- duplicates ------------------------------------------------------
    # Rules cannot target Id -- staging keeps the primary key in record_key,
    # not as a data column -- so duplicates are detected on the data instead.
    ("Account name must be unique", "Name",
     "UNIQUENESS", "WARNING", {}),
    ("A website belongs to one account", "Website",
     "AGGREGATION", "WARNING",
     {"aggregateFunction": "COUNT", "aggregateField": "*",
      "groupBy": ["Website"], "operator": ">", "threshold": 1,
      "filter": {"logic": "AND", "conditions": [
          {"field": "Website", "operator": "is_not_null"}]}}),

    # ---- fields that must agree with each other --------------------------
    ("US accounts must have a billing state", "BillingState",
     "CROSS_FIELD_SIMPLE", "ERROR",
     {"expression": f"BillingCountry = 'USA' AND {blank('BillingState')}"}),
    ("US accounts must have a shipping state", "ShippingState",
     "CROSS_FIELD_SIMPLE", "WARNING",
     {"expression": f"ShippingCountry = 'USA' AND {blank('ShippingState')}"}),
    ("A shipping street needs a shipping city", "ShippingCity",
     "CROSS_FIELD_SIMPLE", "WARNING",
     {"expression": f"{filled('ShippingStreet')} AND {blank('ShippingCity')}"}),
    ("A shipping street needs a shipping country", "ShippingCountry",
     "CROSS_FIELD_SIMPLE", "WARNING",
     {"expression": f"{filled('ShippingStreet')} AND {blank('ShippingCountry')}"}),

    # ---- text hygiene ----------------------------------------------------
    ("Shipping street has no extra spaces", "ShippingStreet",
     "CUSTOM_SQL", "WARNING",
     {"expression": "ShippingStreet <> TRIM(ShippingStreet)"}),
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
            if r["entity_name"] == ENTITY:
                call(f"{API}/rules/{r['rule_id']}", method="DELETE")
                n += 1
        print(f"cleared {n} existing {ENTITY} rules\n")

    ok = err = 0
    by_type = {}
    for name, field, rtype, sev, defn in RULES:
        r = call(f"{API}/rules", {
            "role": "owner", "rule_name": name, "entity_name": ENTITY,
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
        print(f"  #{rid:<3} {rtype:<22} {name}")
        ok += 1
    return ok, err, by_type


def direct(clear):
    from datetime import datetime, timezone
    from app.database import ConfigSession
    from app.models import ENTITIES, ValRule
    from app.rule_compiler import (
        CompileContext, RuleCompileError, compile_rule, dimension_for,
        execution_type_for,
    )

    meta = ENTITIES[ENTITY]
    now = datetime.now(timezone.utc)
    db = ConfigSession()
    ok = err = 0
    by_type = {}
    try:
        if clear:
            n = db.query(ValRule).filter(ValRule.entity_name == ENTITY).delete(
                synchronize_session=False)
            db.commit()
            print(f"cleared {n} existing {ENTITY} rules\n")
        for name, field, rtype, sev, defn in RULES:
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
                rule_type=rtype, entity_name=ENTITY, field_name=field,
                primary_key_field=meta["primary_key_field"],
                execution_type=execution_type_for(rtype), dimension=dim,
                rule_definition=dj, severity=sev, status="APPROVED", active=True,
                created_by="prabhat", created_date=now,
                approved_by="prabhat", approved_date=now))
            by_type[rtype] = by_type.get(rtype, 0) + 1
            print(f"       {rtype:<22} {dim:<13} {name}")
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
    print("\nNot used: REFERENTIAL_INTEGRITY (needs a second object), "
          "RANGE (no numeric column).")


if __name__ == "__main__":
    main()
