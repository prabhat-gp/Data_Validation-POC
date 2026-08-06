"""
seed_source_data.py
-------------------
Adds rows to the MySQL source_db b2b* tables so that EVERY rule type has both
passing and failing records.

The original 40-odd rows left five rules scoring a flat 100% because the data
simply had no such problem:

    UNIQUENESS  email                  no duplicate emails existed
    UNIQUENESS  customer_name+sbg_id   no duplicate name/SBG pairs
    UNIQUENESS  product_code           no duplicate codes
    REF_INTEGRITY customer/product     every sbg_id resolved
    AGGREGATION customers per SBG      max was 6, threshold 8
    AGGREGATION prices per product     every product had exactly one

Each block below is labelled with the rule it exercises, so a failing count
can be traced back to the rows that caused it.

    python seed_source_data.py            # append the new rows
    python seed_source_data.py --reset    # remove them first (keeps originals)
"""

import sys

from sqlalchemy import create_engine, text

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from app.ingestion import source_url_for  # noqa: E402

# --------------------------------------------------------------------------
# b2bsbg  (+3)  -- one blank name trips COMPLETENESS on sbg_name
# --------------------------------------------------------------------------
SBG = [
    ("SBG005", "SPS"),          # clean
    ("SBG006", "HON-DIGITAL"),  # clean
    ("SBG007", ""),             # COMPLETENESS: blank sbg_name
]

# --------------------------------------------------------------------------
# b2bcustomer  (+26)  CUST021..CUST046
#   sbg_id SBG001 gets many rows -> AGGREGATION (customers per SBG > 8)
# --------------------------------------------------------------------------
CUSTOMER = [
    # -- clean rows (should pass everything) --------------------------------
    ("CUST021", "Siemens",            "info@siemens.com",      "Active",   "EMEA",  "SBG001"),
    ("CUST022", "Bosch",              "contact@bosch.com",     "Active",   "EMEA",  "SBG001"),
    ("CUST023", "Rolls Royce",        "sales@rollsroyce.com",  "Active",   "EMEA",  "SBG001"),
    ("CUST024", "Safran",             "hello@safran.com",      "Inactive", "EMEA",  "SBG001"),
    ("CUST025", "Collins Aerospace",  "info@collins.com",      "Active",   "US",    "SBG001"),
    ("CUST026", "GE Aviation",        "contact@geaviation.com","Active",   "US",    "SBG001"),
    ("CUST027", "Pratt Whitney",      "sales@prattwhitney.com","Active",   "US",    "SBG001"),
    ("CUST028", "Thales",             "info@thales.com",       "Active",   "EMEA",  "SBG002"),
    ("CUST029", "Leonardo",           "contact@leonardo.com",  "Active",   "EMEA",  "SBG002"),
    ("CUST030", "Embraer",            "sales@embraer.com",     "Active",   "LATAM", "SBG002"),
    ("CUST031", "Bombardier",         "info@bombardier.com",   "Inactive", "US",    "SBG003"),
    ("CUST032", "Airbus",             "contact@airbus.com",    "Active",   "EMEA",  "SBG005"),
    ("CUST033", "Boeing",             "sales@boeing.com",      "Active",   "US",    "SBG005"),
    ("CUST034", "Mitsubishi Heavy",   "info@mhi.com",          "Active",   "APAC",  "SBG006"),

    # -- UNIQUENESS: duplicate email (3 rows share one address) -------------
    ("CUST035", "Kawasaki Aerospace", "shared@group.com",      "Active",   "APAC",  "SBG006"),
    ("CUST036", "Kawasaki Heavy Ind", "shared@group.com",      "Active",   "APAC",  "SBG006"),
    ("CUST037", "Kawasaki Systems",   "shared@group.com",      "Active",   "APAC",  "SBG006"),

    # -- UNIQUENESS multi-field: same customer_name + sbg_id ---------------
    ("CUST038", "Honeywell Partner",  "hp1@partner.com",       "Active",   "US",    "SBG003"),
    ("CUST039", "Honeywell Partner",  "hp2@partner.com",       "Active",   "US",    "SBG003"),

    # -- REFERENTIAL_INTEGRITY: sbg_id not in b2bsbg -----------------------
    ("CUST040", "Orphan Industries",  "info@orphan.com",       "Active",   "US",    "SBG999"),
    ("CUST041", "Ghost Systems",      "contact@ghost.com",     "Active",   "APAC",  "SBG888"),

    # -- VALIDITY: sbg_id does not match SBG### ----------------------------
    ("CUST042", "Malformed Corp",     "info@malformed.com",    "Active",   "EMEA",  "SBG1"),

    # -- ALLOWED_VALUES / CROSS_FIELD / COMPLETENESS / CUSTOM_SQL ----------
    ("CUST043", "Suspended Ltd",      "info@suspended.com",    "Suspended","US",    "SBG002"),  # bad status
    ("CUST044", "No Region Inc",      "contact@noregion.com",  "Active",   None,    "SBG002"),  # Active w/o region
    ("CUST045", "  Padded Name Co",   "info@padded.com",       "Active",   "APAC",  "SBG004"),  # leading spaces
    ("CUST046", "Bad Email Ltd",      "not-an-email",          "Active",   "MARS",  "SBG004"),  # bad email + bad region
]

# --------------------------------------------------------------------------
# b2bproduct  (+25)  PROD011..PROD035
# --------------------------------------------------------------------------
PRODUCT = [
    # -- clean --------------------------------------------------------------
    ("PROD011", "PRD-1011", "Engine Health Monitor",   "Hardware", "Active",       "SBG001"),
    ("PROD012", "PRD-1012", "Navigation Display",      "Hardware", "Active",       "SBG001"),
    ("PROD013", "PRD-1013", "Auxiliary Power Unit",    "Hardware", "Active",       "SBG001"),
    ("PROD014", "PRD-1014", "Weather Radar",           "Hardware", "Active",       "SBG001"),
    ("PROD015", "PRD-1015", "Fuel Management Suite",   "Software", "Active",       "SBG002"),
    ("PROD016", "PRD-1016", "Predictive Maintenance",  "Software", "Active",       "SBG002"),
    ("PROD017", "PRD-1017", "Fleet Analytics",         "Software", "Active",       "SBG002"),
    ("PROD018", "PRD-1018", "Line Maintenance",        "Service",  "Active",       "SBG003"),
    ("PROD019", "PRD-1019", "Overhaul Programme",      "Service",  "Active",       "SBG003"),
    ("PROD020", "PRD-1020", "Technical Training",      "Service",  "Discontinued", "SBG003"),
    ("PROD021", "PRD-1021", "Cockpit Voice Recorder",  "Hardware", "Active",       "SBG005"),
    ("PROD022", "PRD-1022", "Landing Gear Sensor",     "Hardware", "Active",       "SBG005"),
    ("PROD023", "PRD-1023", "Cabin Comfort Module",    "Hardware", "Active",       "SBG006"),
    ("PROD024", "PRD-1024", "Runway Safety System",    "Software", "Active",       "SBG006"),

    # -- UNIQUENESS: duplicate product_code (3 rows share PRD-2000) --------
    ("PROD025", "PRD-2000", "Duplicate Code Item A",   "Hardware", "Active",       "SBG001"),
    ("PROD026", "PRD-2000", "Duplicate Code Item B",   "Hardware", "Active",       "SBG001"),
    ("PROD027", "PRD-2000", "Duplicate Code Item C",   "Software", "Active",       "SBG002"),

    # -- VALIDITY: product_code fails PRD-#### -----------------------------
    ("PROD028", "PRD1028",   "Missing Hyphen Unit",    "Hardware", "Active",       "SBG001"),
    ("PROD029", "prd-1029",  "Lowercase Code Unit",    "Hardware", "Active",       "SBG002"),
    ("PROD030", "XX-1030",   "Wrong Prefix Unit",      "Software", "Active",       "SBG002"),

    # -- ALLOWED_VALUES: category not in the list --------------------------
    ("PROD031", "PRD-1031", "Uncategorised Widget",    "Spares",   "Active",       "SBG003"),
    ("PROD032", "PRD-1032", "Consumable Pack",         "Misc",     "Active",       "SBG003"),

    # -- COMPLETENESS: blank product_name ----------------------------------
    ("PROD033", "PRD-1033", "",                        "Hardware", "Active",       "SBG004"),
    ("PROD034", "PRD-1034", None,                      "Software", "Active",       "SBG004"),

    # -- REFERENTIAL_INTEGRITY: sbg_id not in b2bsbg -----------------------
    ("PROD035", "PRD-1035", "Orphan SBG Product",      "Hardware", "Active",       "SBG777"),
]

# --------------------------------------------------------------------------
# b2bprice  (+28)  PRICE011..PRICE038
#   several products deliberately get MORE THAN ONE price -> AGGREGATION
# --------------------------------------------------------------------------
PRICE = [
    # -- clean, one price per product ---------------------------------------
    ("PRICE011", "PROD011", 2400.00, 10.0, "Active",   "2026-01-01", "2026-12-31"),
    ("PRICE012", "PROD012", 1850.00,  5.0, "Active",   "2026-01-01", "2026-12-31"),
    ("PRICE013", "PROD013", 9800.00, 15.0, "Active",   "2026-01-01", "2026-12-31"),
    ("PRICE014", "PROD014", 5600.00,  0.0, "Active",   "2026-01-01", "2026-12-31"),
    ("PRICE015", "PROD015", 3200.00, 20.0, "Active",   "2026-01-01", "2026-12-31"),
    ("PRICE016", "PROD016", 2750.00, 12.5, "Active",   "2026-01-01", "2026-12-31"),
    ("PRICE017", "PROD017", 4100.00,  7.5, "Active",   "2026-01-01", "2026-12-31"),
    ("PRICE018", "PROD018",  650.00,  0.0, "Active",   "2026-01-01", "2026-12-31"),
    ("PRICE019", "PROD019", 12500.00, 8.0, "Active",   "2026-01-01", "2026-12-31"),
    ("PRICE020", "PROD021", 3300.00, 10.0, "Active",   "2026-01-01", "2026-12-31"),
    ("PRICE021", "PROD022", 1450.00,  5.0, "Active",   "2026-01-01", "2026-12-31"),
    ("PRICE022", "PROD023", 2100.00,  0.0, "Inactive", "2026-01-01", "2026-12-31"),

    # -- AGGREGATION: PROD011 / PROD015 / PROD018 get extra prices ----------
    ("PRICE023", "PROD011", 2450.00, 10.0, "Active",   "2026-02-01", "2026-12-31"),
    ("PRICE024", "PROD011", 2500.00, 12.0, "Active",   "2026-03-01", "2026-12-31"),
    ("PRICE025", "PROD015", 3300.00, 18.0, "Active",   "2026-02-01", "2026-12-31"),
    ("PRICE026", "PROD015", 3400.00, 16.0, "Active",   "2026-03-01", "2026-12-31"),
    ("PRICE027", "PROD018",  675.00,  0.0, "Active",   "2026-02-01", "2026-12-31"),

    # -- RANGE: price outside 0..100000 ------------------------------------
    ("PRICE028", "PROD012",  -125.00,  0.0, "Active",  "2026-01-01", "2026-12-31"),
    ("PRICE029", "PROD013", 250000.00, 0.0, "Active",  "2026-01-01", "2026-12-31"),
    ("PRICE030", "PROD014",  -1.00,    5.0, "Active",  "2026-01-01", "2026-12-31"),

    # -- RANGE: discount outside 0..100 ------------------------------------
    ("PRICE031", "PROD016", 1000.00, 120.0, "Active",  "2026-01-01", "2026-12-31"),
    ("PRICE032", "PROD017", 1500.00, -10.0, "Active",  "2026-01-01", "2026-12-31"),
    ("PRICE033", "PROD019", 2000.00, 999.0, "Active",  "2026-01-01", "2026-12-31"),

    # -- REFERENTIAL_INTEGRITY: product_id not in b2bproduct ---------------
    ("PRICE034", "PROD888",  500.00,  0.0, "Active",   "2026-01-01", "2026-12-31"),
    ("PRICE035", "PROD777",  750.00,  0.0, "Active",   "2026-01-01", "2026-12-31"),
    ("PRICE036", "NOPROD",   900.00,  0.0, "Active",   "2026-01-01", "2026-12-31"),

    # -- more clean rows so the scores are not dominated by failures -------
    ("PRICE037", "PROD024", 1900.00,  6.0, "Active",   "2026-01-01", "2026-12-31"),
    ("PRICE038", "PROD020",  480.00,  0.0, "Inactive", "2026-01-01", "2026-12-31"),
]

NEW_IDS = {
    "b2bsbg":      ("sbg_id",      [r[0] for r in SBG]),
    "b2bcustomer": ("customer_id", [r[0] for r in CUSTOMER]),
    "b2bproduct":  ("product_id",  [r[0] for r in PRODUCT]),
    "b2bprice":    ("price_id",    [r[0] for r in PRICE]),
}


def main():
    url = source_url_for("MySQL")
    if not url:
        print("No MySQL connection. Check DB_* / SOURCE_DB in .env")
        sys.exit(1)
    eng = create_engine(url)

    with eng.begin() as c:
        # remove any previously seeded rows so re-running is safe
        for table, (key, ids) in NEW_IDS.items():
            placeholders = ", ".join(f":i{n}" for n in range(len(ids)))
            c.execute(text(f"DELETE FROM {table} WHERE {key} IN ({placeholders})"),
                      {f"i{n}": v for n, v in enumerate(ids)})
        if "--reset" in sys.argv:
            print("Seeded rows removed. Originals untouched.")
            eng.dispose()
            return

        c.execute(text("INSERT INTO b2bsbg (sbg_id, sbg_name) VALUES (:a,:b)"),
                  [dict(zip("ab", r)) for r in SBG])
        c.execute(text("""INSERT INTO b2bcustomer
                          (customer_id, customer_name, email, status, region, sbg_id)
                          VALUES (:a,:b,:c,:d,:e,:f)"""),
                  [dict(zip("abcdef", r)) for r in CUSTOMER])
        c.execute(text("""INSERT INTO b2bproduct
                          (product_id, product_code, product_name, category, status, sbg_id)
                          VALUES (:a,:b,:c,:d,:e,:f)"""),
                  [dict(zip("abcdef", r)) for r in PRODUCT])
        c.execute(text("""INSERT INTO b2bprice
                          (price_id, product_id, price_amount, discount_pct, status, eff_date, end_date)
                          VALUES (:a,:b,:c,:d,:e,:f,:g)"""),
                  [dict(zip("abcdefg", r)) for r in PRICE])

    with eng.connect() as c:
        print("Row counts after seeding:")
        for t in ("b2bsbg", "b2bcustomer", "b2bproduct", "b2bprice"):
            print(f"  {t:<14} {c.execute(text(f'SELECT COUNT(*) FROM {t}')).scalar():>4}")
    eng.dispose()


if __name__ == "__main__":
    main()
