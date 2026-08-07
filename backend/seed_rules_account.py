"""
seed_rules_account.py
---------------------
25 rules for the Account object, covering 7 of the 9 rule types
and judging all 16 declared elements.

WHAT IS NOT POSSIBLE HERE, AND WHY
    REFERENTIAL_INTEGRITY   needs a SECOND entity staged in the same run to
                            LEFT JOIN against. Account is standalone -- there
                            is no Industry master or Country master table to
                            resolve against -- so this type cannot be used.

    RANGE                   needs a numeric column. All 17 Account columns are
                            text; postal codes are international and mixed
                            (letters in Canadian/UK codes), so a numeric range
                            on one would be measuring the wrong thing.

    CROSS_FIELD_SIMPLE IS possible. It compares fields WITHIN one record, so a
    single table is all it needs -- it is REFERENTIAL_INTEGRITY that requires a
    second table. Six of the rules below use it, and on the 10-row sample the
    "US account with no state" check is the single strongest signal in the data.

DIMENSIONS PRODUCED
    Completeness, Validity, Uniqueness, Consistency, Accuracy   (5 of 6)
    Integrity is unreachable for the reason above.

BLANKS
    prepare_account.py stores empty CSV cells as '' rather than NULL, so every
    cross-field expression tests BOTH -- `X IS NULL OR TRIM(X) = ''`. Testing
    only IS NULL would silently match nothing.

    python seed_rules_account.py            # create + approve all 25
    python seed_rules_account.py --clear    # delete existing Account rules first
    python seed_rules_account.py --direct   # write to val_rules without the API
"""

import json
import os
import sys
import urllib.error
import urllib.request

API = f"http://localhost:{os.getenv('API_PORT', '8000')}/api"
ENTITY = "Account"

_BLANK = "IS NULL OR TRIM({c}) = ''"


def blank(c):
    return f"({c} IS NULL OR TRIM({c}) = '')"


def filled(c):
    return f"({c} IS NOT NULL AND TRIM({c}) <> '')"


# (name, field, rule_type, severity, definition)
RULES = [
    # ---------- COMPLETENESS -> Completeness -----------------------------
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

    # ---------- VALIDITY -> Validity -------------------------------------
    # Blank websites are skipped -- a missing website is a Completeness
    # problem, not a malformed one. Catches "www.franke-aerotec.de".
    ("Website must be a full URL", "Website",
     "VALIDITY", "ERROR", {"pattern": r"^https?://[^\s]+$"}),
    # E.164: leading + then 7-15 digits. Matches the German and Italian
    # numbers in the sample; catches extensions, spaces and local formats.
    ("Phone must be international format", "Phone",
     "VALIDITY", "WARNING", {"pattern": r"^\+[0-9]{7,15}$"}),

    # ---------- ALLOWED_VALUES -> Validity -------------------------------
    # NOTE: extend this list from the real data before trusting the number:
    #   SELECT Type, COUNT(*) FROM account GROUP BY Type ORDER BY 2 DESC;
    ("Account type is a known value", "Type",
     "ALLOWED_VALUES", "ERROR",
     {"allowedValues": ["Owner/Operator", "Product/Service Provider",
                        "Customer", "Partner", "Prospect", "Distributor"]}),
    ("Region is a known region", "Region__c",
     "ALLOWED_VALUES", "WARNING",
     {"allowedValues": ["AMER", "EMEA", "APAC", "LATAM"]}),

    # ---------- UNIQUENESS -> Uniqueness ---------------------------------
    # Rules cannot target Id: staging keeps the primary key in record_key,
    # not as a data column. Duplicate detection goes on the data instead.
    ("Account name is unique", "Name",
     "UNIQUENESS", "WARNING", {}),
    ("Account name is unique per country", "",
     "UNIQUENESS", "WARNING", {"fields": ["Name", "BillingCountry"]}),

    # ---------- CROSS_FIELD_SIMPLE -> Consistency ------------------------
    # Single table, same record -- exactly what this type is for.
    ("US accounts must have a billing state", "BillingState",
     "CROSS_FIELD_SIMPLE", "ERROR",
     {"expression": f"BillingCountry = 'USA' AND {blank('BillingState')}"}),
    ("A billing street needs a billing city", "BillingCity",
     "CROSS_FIELD_SIMPLE", "WARNING",
     {"expression": f"{filled('BillingStreet')} AND {blank('BillingCity')}"}),
    ("A shipping address needs a shipping country", "ShippingCountry",
     "CROSS_FIELD_SIMPLE", "WARNING",
     {"expression": f"{filled('ShippingStreet')} AND {blank('ShippingCountry')}"}),

    # ---------- CUSTOM_SQL -> Consistency --------------------------------
    ("Account name has no stray spaces", "Name",
     "CUSTOM_SQL", "WARNING",
     {"expression": "Name <> TRIM(Name)"}),
    ("Account name is not a placeholder", "Name",
     "CUSTOM_SQL", "ERROR",
     {"expression": "UPPER(TRIM(Name)) IN ('TEST','N/A','NA','UNKNOWN','TBD','XXX','DUMMY')"}),

    # ---------- AGGREGATION -> Accuracy ----------------------------------
    # Two accounts sharing a website are almost always the same company
    # entered twice. Filtered to non-blank websites, otherwise every account
    # without one lands in a single enormous group.
    ("A website belongs to one account", "Website",
     "AGGREGATION", "WARNING",
     {"aggregateFunction": "COUNT", "aggregateField": "*",
      "groupBy": ["Website"], "operator": ">", "threshold": 1,
      "filter": {"logic": "AND", "conditions": [
          {"field": "Website", "operator": "is_not_null"}]}}),
    # ---------- address completeness -------------------------------------
    ("Billing street is required", "BillingStreet",
     "COMPLETENESS", "WARNING", {}),

    # ---------- postal code shape ----------------------------------------
    # Deliberately permissive: has to accept US 5-digit and ZIP+4, Canadian
    # "L7E 1J9", UK, German and Indian formats. What it catches is junk --
    # "N/A", "-", "." and anything over 10 characters.
    ("Billing postal code is a plausible code", "BillingPostalCode",
     "VALIDITY", "WARNING", {"pattern": r"^[A-Za-z0-9][A-Za-z0-9 -]{1,9}$"}),
    ("Shipping postal code is a plausible code", "ShippingPostalCode",
     "VALIDITY", "WARNING", {"pattern": r"^[A-Za-z0-9][A-Za-z0-9 -]{1,9}$"}),

    # ---------- partial shipping addresses --------------------------------
    # Mirrors of the billing checks. A half-entered address is worse than no
    # address -- it looks usable until something tries to ship to it.
    ("A shipping street needs a shipping city", "ShippingCity",
     "CROSS_FIELD_SIMPLE", "WARNING",
     {"expression": f"{filled('ShippingStreet')} AND {blank('ShippingCity')}"}),
    ("A shipping city needs a shipping street", "ShippingStreet",
     "CROSS_FIELD_SIMPLE", "WARNING",
     {"expression": f"{filled('ShippingCity')} AND {blank('ShippingStreet')}"}),
    ("US shipping addresses must have a state", "ShippingState",
     "CROSS_FIELD_SIMPLE", "WARNING",
     {"expression": f"ShippingCountry = 'USA' AND {blank('ShippingState')}"}),

    # ---------- name quality ---------------------------------------------
    # "A", "X", "--" are not company names. Two characters is the shortest
    # real one worth allowing.
    ("Account name is long enough", "Name",
     "CUSTOM_SQL", "WARNING",
     {"expression": "LENGTH(TRIM(Name)) < 3"}),

    # ---------- duplicate detection ---------------------------------------
    # Same phone on two accounts is the other half of the shared-website
    # check: both are how the same company gets entered twice.
    ("A phone number belongs to one account", "Phone",
     "AGGREGATION", "INFO",
     {"aggregateFunction": "COUNT", "aggregateField": "*",
      "groupBy": ["Phone"], "operator": ">", "threshold": 1,
      "filter": {"logic": "AND", "conditions": [
          {"field": "Phone", "operator": "is_not_null"}]}}),
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
        for r in call(f"{API}/rules", method="GET"):
            if r["entity_name"] == ENTITY:
                call(f"{API}/rules/{r['rule_id']}", method="DELETE")
        print(f"cleared existing {ENTITY} rules\n")

    ok = err = 0
    by_type = {}
    for name, field, rtype, sev, defn in RULES:
        r = call(f"{API}/rules", {
            "role": "owner", "rule_name": name, "entity_name": ENTITY,
            "field_name": field, "rule_type": rtype, "severity": sev,
            "rule_definition": defn, "created_by": "prabhat",
        })
        if "error" in r:
            print(f"  FAIL  {rtype:<20} {name}\n        {r['error'][:100]}")
            err += 1
            continue
        rid = r["rule_id"]
        call(f"{API}/rules/{rid}/submit", {"actor": "prabhat", "role": "owner"})
        call(f"{API}/rules/{rid}/approve", {"actor": "prabhat", "role": "admin"})
        by_type[rtype] = by_type.get(rtype, 0) + 1
        print(f"  #{rid:<3} {rtype:<20} {r.get('dimension', ''):<13} {name}")
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

    meta = ENTITIES[ENTITY]
    now = datetime.now(timezone.utc)
    db = ConfigSession()
    ok = err = 0
    by_type = {}
    try:
        if clear:
            n = db.query(ValRule).filter(ValRule.entity_name == ENTITY).delete()
            db.commit()
            print(f"cleared {n} existing {ENTITY} rules\n")
        for name, field, rtype, sev, defn in RULES:
            dj = json.dumps(defn)
            ctx = CompileContext(table="stg_x", columns=meta["columns"],
                                 lookup_table="stg_lookup", lookup_run_id=0)
            try:
                compile_rule(rtype, field, dj, ctx)
            except RuleCompileError as exc:
                print(f"  FAIL  {rtype:<20} {name}\n        {exc}")
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
            print(f"       {rtype:<20} {dim:<13} {name}")
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
        print(f"  {t:<22} {n}")
    print("""
NOT USED: REFERENTIAL_INTEGRITY (needs a second table), RANGE (no numeric
column). Dimensions produced: Completeness, Validity, Uniqueness, Consistency,
Accuracy -- Integrity is unreachable without a lookup object.

Before trusting the ALLOWED_VALUES counts, check the real domains:
    SELECT Type,      COUNT(*) FROM account GROUP BY Type      ORDER BY 2 DESC;
    SELECT Region__c, COUNT(*) FROM account GROUP BY Region__c ORDER BY 2 DESC;
""")


if __name__ == "__main__":
    main()
