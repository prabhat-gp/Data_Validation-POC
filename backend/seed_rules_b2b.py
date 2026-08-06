"""
seed_rules_b2b.py
-----------------
23 rules across the four B2B entities, covering all 9 rule types.

Every rule targets a REAL problem visible in source_db, so a run produces
meaningful numbers rather than a flat 100%.

The backend must already be running -- these go in through the API.

    python seed_rules_b2b.py            # create + approve all 23
    python seed_rules_b2b.py --clear    # delete every rule first

Rules are created through the API so they go through the same validation,
id generation and approval flow the UI uses -- nothing is inserted directly.
"""

import json
import os
import sys
import urllib.error
import urllib.request

# The backend runs on 8000 by default; override with API_PORT if you started
# uvicorn somewhere else.
API = f"http://localhost:{os.getenv('API_PORT', '8000')}/api"


def call(url, body=None, method="POST"):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body else None,
        headers={"Content-Type": "application/json"}, method=method,
    )
    try:
        return json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        return {"error": json.loads(e.read()).get("detail", str(e))}


# (name, entity, field, rule_type, severity, definition)
RULES = [
    # ---------------- B2B Customer -------------------------------------
    ("Customer name is required", "B2B Customer", "customer_name",
     "COMPLETENESS", "CRITICAL", {}),
    ("Customer email is required", "B2B Customer", "email",
     "COMPLETENESS", "CRITICAL", {}),
    ("Region is required", "B2B Customer", "region",
     "COMPLETENESS", "WARNING", {}),
    ("Email must be well formed", "B2B Customer", "email",
     "VALIDITY", "ERROR", {"pattern": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"}),
    # NOTE: rules cannot target the primary key -- staging keeps it in
    # record_key, not as a data column. Use a data column instead.
    ("SBG id follows SBGnnn", "B2B Customer", "sbg_id",
     "VALIDITY", "WARNING", {"pattern": r"^SBG[0-9]{3}$"}),
    ("Customer status is Active or Inactive", "B2B Customer", "status",
     "ALLOWED_VALUES", "ERROR", {"allowedValues": ["Active", "Inactive"]}),
    ("Region is a known region", "B2B Customer", "region",
     "ALLOWED_VALUES", "WARNING", {"allowedValues": ["APAC", "EMEA", "US", "LATAM"]}),
    ("Customer email is unique", "B2B Customer", "email",
     "UNIQUENESS", "CRITICAL", {}),
    ("Customer name is unique per SBG", "B2B Customer", "",
     "UNIQUENESS", "WARNING", {"fields": ["customer_name", "sbg_id"]}),
    ("Customer SBG must exist", "B2B Customer", "sbg_id",
     "REFERENTIAL_INTEGRITY", "CRITICAL",
     {"lookupTable": "B2B SBG", "lookupField": "sbg_id"}),
    ("Active customers need a region", "B2B Customer", "region",
     "CROSS_FIELD_SIMPLE", "ERROR",
     {"expression": "status = 'Active' AND region IS NULL"}),
    ("Customer name has no stray spaces", "B2B Customer", "customer_name",
     "CUSTOM_SQL", "WARNING",
     {"expression": "customer_name <> TRIM(customer_name)"}),
    ("Customers per SBG is reasonable", "B2B Customer", "sbg_id",
     "AGGREGATION", "INFO",
     {"aggregateFunction": "COUNT", "aggregateField": "*",
      "groupBy": ["sbg_id"], "operator": ">", "threshold": 8}),

    # ---------------- B2B Product --------------------------------------
    ("Product name is required", "B2B Product", "product_name",
     "COMPLETENESS", "CRITICAL", {}),
    ("Product code follows PRD-nnnn", "B2B Product", "product_code",
     "VALIDITY", "ERROR", {"pattern": r"^PRD-[0-9]{4}$"}),
    ("Product category is known", "B2B Product", "category",
     "ALLOWED_VALUES", "WARNING",
     {"allowedValues": ["Hardware", "Software", "Service"]}),
    ("Product code is unique", "B2B Product", "product_code",
     "UNIQUENESS", "CRITICAL", {}),
    ("Product SBG must exist", "B2B Product", "sbg_id",
     "REFERENTIAL_INTEGRITY", "CRITICAL",
     {"lookupTable": "B2B SBG", "lookupField": "sbg_id"}),

    # ---------------- B2B Price ----------------------------------------
    ("Price must be positive and sane", "B2B Price", "price_amount",
     "RANGE", "CRITICAL", {"min": 0, "max": 100000}),
    ("Discount must be 0-100%", "B2B Price", "discount_pct",
     "RANGE", "ERROR", {"min": 0, "max": 100}),
    ("Price product must exist", "B2B Price", "product_id",
     "REFERENTIAL_INTEGRITY", "CRITICAL",
     {"lookupTable": "B2B Product", "lookupField": "product_id"}),
    ("One active price per product", "B2B Price", "product_id",
     "AGGREGATION", "WARNING",
     {"aggregateFunction": "COUNT", "aggregateField": "*",
      "groupBy": ["product_id"], "operator": ">", "threshold": 1}),

    # ---------------- B2B SBG ------------------------------------------
    ("SBG name is required", "B2B SBG", "sbg_name",
     "COMPLETENESS", "CRITICAL", {}),
]


def main():
    if "--clear" in sys.argv:
        for r in call(f"{API}/rules", method="GET"):
            call(f"{API}/rules/{r['rule_id']}", method="DELETE")
        print("cleared existing rules\n")

    ok = err = 0
    by_type = {}
    for name, entity, field, rtype, sev, defn in RULES:
        r = call(f"{API}/rules", {
            "role": "owner", "rule_name": name, "entity_name": entity,
            "field_name": field, "rule_type": rtype, "severity": sev,
            "rule_definition": defn, "created_by": "prabhat",
        })
        if "error" in r:
            print(f"  FAIL  {rtype:<22} {name}\n        {r['error'][:90]}")
            err += 1
            continue
        rid = r["rule_id"]
        call(f"{API}/rules/{rid}/submit", {"actor": "prabhat", "role": "owner"})
        call(f"{API}/rules/{rid}/approve", {"actor": "prabhat", "role": "admin"})
        by_type[rtype] = by_type.get(rtype, 0) + 1
        print(f"  #{rid:<3} {rtype:<22} {entity:<14} {name}")
        ok += 1

    print(f"\n{ok} rules created and APPROVED, {err} failed")
    print("coverage:", ", ".join(f"{k}={v}" for k, v in sorted(by_type.items())))


if __name__ == "__main__":
    main()
