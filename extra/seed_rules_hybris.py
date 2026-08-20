"""
seed_rules_hybris.py
--------------------
45 rules for the three Hybris objects, as finalised by Prabhat.

    B2B Customer  20
    B2B Unit      13
    Address       12

Every rule here was compiled against the real column lists and executed
against the 49 / 49 / 50 row extracts in data_dump/ before being written down,
so none of them fail to compile and none of them match nothing by accident.

TWO REFERENTIAL-INTEGRITY LINKS
    B2B Customer.defaultB2BUnit -> B2B Unit.uid
    B2B Unit.addresses          -> Address.pk

The rule is declared on the CHILD -- the object holding the foreign key. The
entity key in the definition is "lookupTable" (rule_compiler.referenced_entity
reads that or "ref_entity_name", nothing else). Spelling it any other way
leaves the lookup object un-staged, the rule fails to compile, and the metric
is silently recorded as 0 failed / 0.0%.

Both objects must be in the SAME RUN for these to work -- the join reads the
lookup entity's staging table.

    python seed_rules_hybris.py --direct           # write to val_rules, approved
    python seed_rules_hybris.py --direct --clear   # replace existing Hybris rules
    python seed_rules_hybris.py                    # go through the API instead
"""

import os
import sys

# this script lives in extra/, the app package lives in backend/
BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
sys.path.insert(0, BACKEND)

import json
import urllib.error
import urllib.request

API = f"http://localhost:{os.getenv('API_PORT', '8000')}/api"

# (rule_name, element, rule_type, severity, definition)
RULES = {
    # ---------------------------------------------------------------- 20 ---
    "B2B Customer": [
        ("Customer uid is required", "originalUid", "COMPLETENESS", "CRITICAL", {}),
        ("Customer uid must be unique", "originalUid", "UNIQUENESS", "CRITICAL", {}),
        ("Customer name is required", "name", "COMPLETENESS", "CRITICAL", {}),
        # Catches mojibake -- 'Daniela GarcÃ­Ã¡ Bautista' is a UTF-8 name that
        # was read as Latin-1. A LIKE '%Ã%' test does NOT work: the default
        # utf8mb4_0900_ai_ci collation is accent-insensitive, so it matches
        # every plain 'a' and reported 42 of 49 rows instead of 2.
        ("Customer name has no mis-encoded characters", "name", "VALIDITY", "ERROR",
         {"pattern": r"^[ -~]+$"}),
        ("Email is required", "email", "COMPLETENESS", "CRITICAL", {}),
        ("Email must be unique", "email", "UNIQUENESS", "ERROR", {}),
        ("Phone is required", "phone", "COMPLETENESS", "WARNING", {}),
        ("Phone must be + country code then digits", "phone", "VALIDITY", "WARNING",
         {"pattern": r"^\+[0-9]{1,3}[0-9 ]{6,14}$"}),
        ("Active must be True or False", "active", "ALLOWED_VALUES", "ERROR",
         {"allowedValues": ["True", "False"]}),
        ("Login disabled must be True or False", "loginDisabled", "ALLOWED_VALUES", "ERROR",
         {"allowedValues": ["True", "False"]}),
        ("An active customer cannot have login disabled", "loginDisabled",
         "CROSS_FIELD_SIMPLE", "ERROR",
         {"expression": "active = 'True' AND loginDisabled = 'True'"}),
        ("Creation time is DD.MM.YYYY HH:MM:SS", "creationtime", "VALIDITY", "ERROR",
         {"pattern": r"^[0-3][0-9]\.[0-1][0-9]\.[0-9]{4} [0-2][0-9]:[0-5][0-9]:[0-5][0-9]$"}),
        ("Default unit is a 10-digit code", "defaultB2BUnit", "VALIDITY", "WARNING",
         {"pattern": r"^[0-9]{10}$"}),
        ("Default unit must exist in B2B Unit", "defaultB2BUnit",
         "REFERENTIAL_INTEGRITY", "CRITICAL",
         {"lookupTable": "B2B Unit", "lookupField": "uid"}),
        ("Customer type must be a known value", "hwCustomerType", "ALLOWED_VALUES", "WARNING",
         {"allowedValues": ["EXTERNAL:HoneywellCustomerType"]}),
        ("Tool access must be a known value", "toolAccess", "ALLOWED_VALUES", "WARNING",
         {"allowedValues": ["Online Ordering"]}),
        ("Session language is required", "sessionLanguage", "COMPLETENESS", "WARNING", {}),
        ("Session language is a 2-letter ISO code", "sessionLanguage", "VALIDITY", "WARNING",
         {"pattern": r"^[a-z]{2}$"}),
        ("SFDC contact id is required", "sfdcContactId", "COMPLETENESS", "ERROR", {}),
        ("SFDC contact id must be unique", "sfdcContactId", "UNIQUENESS", "ERROR", {}),
    ],
    # ---------------------------------------------------------------- 13 ---
    "B2B Unit": [
        ("Unit uid is required", "uid", "COMPLETENESS", "CRITICAL", {}),
        ("Unit uid must be unique", "uid", "UNIQUENESS", "CRITICAL", {}),
        ("Unit name is required", "name", "COMPLETENESS", "CRITICAL", {}),
        ("Unit name should be unique", "name", "UNIQUENESS", "WARNING", {}),
        ("English localised name is required", "locName_en", "COMPLETENESS", "ERROR", {}),
        ("Account type is required", "accountType", "COMPLETENESS", "WARNING", {}),
        ("Account type must be a known value", "accountType", "ALLOWED_VALUES", "ERROR",
         {"allowedValues": ["Commercial Airline", "Dealer", "Distributor",
                            "Leasing Company", "OEM", "Owner/Operator",
                            "Product/Service Provider"]}),
        ("Active must be True or False", "active", "ALLOWED_VALUES", "ERROR",
         {"allowedValues": ["True", "False"]}),
        ("Order block must be True or False", "orderBlock", "ALLOWED_VALUES", "ERROR",
         {"allowedValues": ["True", "False"]}),
        ("Service layer is required", "sfdcServiceLayer", "COMPLETENESS", "WARNING", {}),
        ("Service layer must be a known value", "sfdcServiceLayer", "ALLOWED_VALUES", "WARNING",
         {"allowedValues": ["Comprehensive", "Dealer", "Refer to Network",
                            "Repair Shop", "Standard", "Superior"]}),
        ("Address reference is required", "addresses", "COMPLETENESS", "ERROR", {}),
        ("Address reference must exist in Address", "addresses",
         "REFERENTIAL_INTEGRITY", "CRITICAL",
         {"lookupTable": "Address", "lookupField": "pk"}),
    ],
    # ----------------------------------------------------------------- 12 ---
    "Address": [
        ("Country is required", "country", "COMPLETENESS", "CRITICAL", {}),
        ("Country is a 2-letter uppercase ISO code", "country", "VALIDITY", "ERROR",
         {"pattern": r"^[A-Z]{2}$"}),
        ("Country must be a serviced country", "country", "ALLOWED_VALUES", "WARNING",
         {"allowedValues": ["US", "CA"]}),
        ("Postal code is required", "postalcode", "COMPLETENESS", "CRITICAL", {}),
        ("Postal code has no odd characters", "postalcode", "VALIDITY", "WARNING",
         {"pattern": r"^[A-Za-z0-9 -]+$"}),
        ("Postal code has no leading or trailing space", "postalcode", "CUSTOM_SQL", "WARNING",
         {"expression": "postalcode <> TRIM(postalcode)"}),
        # LENGTH is byte length and CHAR_LENGTH is not on the expression
        # whitelist, so the US/CA shape is checked with a pattern per country
        # rather than a cross-field length test.
        ("US postal code is 5 digits or ZIP+4", "postalcode", "CROSS_FIELD_SIMPLE", "ERROR",
         {"expression": "country = 'US' AND postalcode NOT LIKE '_____' "
                        "AND postalcode NOT LIKE '_____-____'"}),
        ("Billing flag must be true or false", "billingAddress", "ALLOWED_VALUES", "ERROR",
         {"allowedValues": ["true", "false"]}),
        ("Shipping flag must be true or false", "shippingAddress", "ALLOWED_VALUES", "ERROR",
         {"allowedValues": ["true", "false"]}),
        ("Save address must be true or false", "saveAddress", "ALLOWED_VALUES", "INFO",
         {"allowedValues": ["true", "false"]}),
        ("An address is not both billing and shipping", "shippingAddress",
         "CROSS_FIELD_SIMPLE", "WARNING",
         {"expression": "billingAddress = 'true' AND shippingAddress = 'true'"}),
        ("An address must be billing or shipping", "billingAddress",
         "CROSS_FIELD_SIMPLE", "ERROR",
         {"expression": "billingAddress = 'false' AND shippingAddress = 'false'"}),
    ],
}

# Which entity each REFERENTIAL_INTEGRITY rule needs staged alongside it.
LOOKUP_OF = {"B2B Customer": "B2B Unit", "B2B Unit": "Address"}


def call(url, body=None, method="POST"):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body else None,
        headers={"Content-Type": "application/json"}, method=method)
    try:
        return json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        return {"error": json.loads(e.read()).get("detail", str(e))}


def via_api(clear):
    ok = err = 0
    by_type = {}
    if clear:
        n = 0
        for r in call(f"{API}/rules", method="GET"):
            if r["entity_name"] in RULES:
                call(f"{API}/rules/{r['rule_id']}", method="DELETE")
                n += 1
        print(f"cleared {n} existing Hybris rules\n")

    for entity, rules in RULES.items():
        print(f"\n{entity}")
        for name, field, rtype, sev, defn in rules:
            r = call(f"{API}/rules", {
                "role": "owner", "rule_name": name, "entity_name": entity,
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
            print(f"  #{rid:<4} {rtype:<22} {name}")
            ok += 1
    return ok, err, by_type


def direct(clear):
    from datetime import datetime, timezone
    from app.database import ConfigSession
    from app.models import ENTITIES, ValRule, staging_table_name
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
            n = db.query(ValRule).filter(ValRule.entity_name.in_(list(RULES))).delete(
                synchronize_session=False)
            db.commit()
            print(f"cleared {n} existing Hybris rules\n")

        for entity, rules in RULES.items():
            meta = ENTITIES[entity]
            lk = LOOKUP_OF.get(entity)
            lkm = ENTITIES[lk] if lk else None
            # A real context, not a placeholder -- otherwise a referential
            # integrity rule compiles here and fails at run time.
            ctx = CompileContext(
                table=staging_table_name(entity), columns=meta["columns"],
                lookup_table=staging_table_name(lk) if lk else None,
                lookup_run_id=0,
                lookup_key_field=lkm["primary_key_field"] if lkm else None,
                lookup_columns=lkm["columns"] if lkm else None)

            print(f"\n{entity}  ({len(rules)} rules)")
            for name, field, rtype, sev, defn in rules:
                dj = json.dumps(defn)
                try:
                    compile_rule(rtype, field, dj, ctx)
                except RuleCompileError as exc:
                    print(f"  FAIL  {rtype:<22} {name}\n        {exc}")
                    err += 1
                    continue
                dim = dimension_for(rtype)
                db.add(ValRule(
                    rule_name=name, source_system=meta["source_system"],
                    rule_type=rtype, entity_name=entity, field_name=field,
                    primary_key_field=meta["primary_key_field"],
                    execution_type=execution_type_for(rtype), dimension=dim,
                    rule_definition=dj, severity=sev, status="APPROVED", active=True,
                    created_by="prabhat", created_date=now,
                    approved_by="prabhat", approved_date=now))
                by_type[rtype] = by_type.get(rtype, 0) + 1
                print(f"  {rtype:<22} {dim:<13} {name}")
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
    print("\nReferential integrity needs both objects in the SAME run:")
    for child, parent in LOOKUP_OF.items():
        print(f"  {child:<14} -> {parent}")


if __name__ == "__main__":
    main()
