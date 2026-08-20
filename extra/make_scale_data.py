"""
make_scale_data.py
------------------
Builds a large, realistically-shaped Hybris dataset in source_db.

Row counts follow the real relationship rather than being equal: a B2B Unit is
an organisation, customers belong to one, and both carry addresses. So

    B2B Unit       1 x       organisations
    B2B Customer   4 x       people inside them
    Address        5 x       one per unit plus one per customer

Every distribution below is copied from the 49/49/50-row extract, so a scaled
run fails at the same RATES the real data does and the numbers stay comparable:

    Address defects, matching the shape of the real 700k-row dump:
      country blank              3%      postalcode blank          4%
      country lowercase 'us'     4%      postalcode padded         5%
      country 'USA' not ISO      2%      postalcode junk 'ZZnnn'    3%
      billing AND shipping       6%      saveAddress 'Y' not bool   2%
      neither billing nor ship   3%

    defaultB2BUnit blank      31%      accountType blank        53%
    sessionLanguage blank     31%      sfdcServiceLayer blank   53%
    toolAccess blank           4%      unit name duplicated     20%
    name mis-encoded           4%      "DO NOT USE" in name     49%
    phone wrong format        16%      locName_en != name        6%
    sfdcContactId duplicated   2%      accountType invalid       6%
    active AND loginDisabled  47%      orderBlock True          84%

Referential integrity is planted, not accidental: a known share of customers
point at a unit uid that does not exist, and likewise units at addresses.

    python make_scale_data.py --rows 5000000
    python make_scale_data.py --rows 5000000 --keep-existing

Generation is INSERT..SELECT over a numbers table -- no row ever enters Python.
"""

import argparse
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "backend"))

from sqlalchemy import text                                    # noqa: E402
from app.database import source_engine                         # noqa: E402

# unit : customer : address
MIX = (1, 4, 5)

# planted defect rates, as 1-in-N so they can be expressed in SQL with MOD
ORPHAN_UNIT_IN = 7        # customers whose defaultB2BUnit does not exist
ORPHAN_ADDR_IN = 11       # units whose addresses pk does not exist

CHUNK = 500_000           # rows per INSERT..SELECT


def build_numbers(conn, upto):
    """A numbers table beats a recursive CTE: built once, reused per object."""
    conn.execute(text("DROP TABLE IF EXISTS _seq"))
    conn.execute(text("CREATE TABLE _seq (i BIGINT PRIMARY KEY)"))
    conn.execute(text("SET SESSION cte_max_recursion_depth = 100000"))
    conn.execute(text(
        "INSERT INTO _seq (i) WITH RECURSIVE s(i) AS "
        "(SELECT 0 UNION ALL SELECT i+1 FROM s WHERE i < 9999) SELECT i FROM s"))
    # 10k x 10k = 100M addressable, far past anything we generate
    conn.execute(text("DROP TABLE IF EXISTS _seq2"))
    # Bound the OUTER side too. Without `a.i <= upto/10000` MySQL forms all
    # 100M combinations and filters afterwards -- 40x more work than needed.
    conn.execute(text(
        "CREATE TABLE _seq2 (i BIGINT PRIMARY KEY) "
        "SELECT (a.i * 10000 + b.i) AS i FROM _seq a CROSS JOIN _seq b "
        f"WHERE a.i <= {upto // 10000} AND (a.i * 10000 + b.i) < {upto}"))


DDL = {
    "b2bunit": """
        CREATE TABLE b2bunit (
          pk VARCHAR(20) PRIMARY KEY, uid VARCHAR(32), name VARCHAR(160),
          locName_en VARCHAR(160), accountType VARCHAR(40), active VARCHAR(10),
          orderBlock VARCHAR(10), sfdcServiceLayer VARCHAR(40), addresses VARCHAR(32))""",
    "b2bcustomer": """
        CREATE TABLE b2bcustomer (
          pk VARCHAR(20) PRIMARY KEY, originalUid VARCHAR(32), name VARCHAR(160),
          email VARCHAR(160), phone VARCHAR(40), active VARCHAR(10),
          loginDisabled VARCHAR(10), creationtime VARCHAR(32),
          defaultB2BUnit VARCHAR(32), hwCustomerType VARCHAR(60),
          toolAccess VARCHAR(40), sessionCurrency VARCHAR(10),
          sessionLanguage VARCHAR(10), sfdcContactId VARCHAR(32))""",
    "address": """
        CREATE TABLE address (
          pk VARCHAR(20) PRIMARY KEY, country VARCHAR(8), postalcode VARCHAR(20),
          billingAddress VARCHAR(10), shippingAddress VARCHAR(10),
          saveAddress VARCHAR(10))""",
}

# ---------------------------------------------------------------------------
# One INSERT..SELECT per object. `i` is the row ordinal from _seq2.
# ---------------------------------------------------------------------------
UNIT_SQL = """
INSERT INTO b2bunit (pk, uid, name, locName_en, accountType, active, orderBlock,
                     sfdcServiceLayer, addresses)
SELECT
  CONCAT('879', LPAD(i + 7000000000, 10, '0')),
  LPAD(i + 1, 10, '0'),
  CASE WHEN i % 2 = 0 THEN CONCAT('DO NOT USE COMPANY ', i % {name_pool})
       ELSE CONCAT('COMPANY ', i % {name_pool}) END,
  CASE WHEN i % 17 = 0 THEN CONCAT('LOCALISED ', i)
       WHEN i % 2 = 0 THEN CONCAT('DO NOT USE COMPANY ', i % {name_pool})
       ELSE CONCAT('COMPANY ', i % {name_pool}) END,
  CASE WHEN i % 100 < 53 THEN ''
       WHEN i % 100 < 59 THEN '01'
       ELSE ELT(1 + (i % 7), 'Commercial Airline','Dealer','Distributor',
                'Leasing Company','OEM','Owner/Operator','Product/Service Provider') END,
  'True',
  CASE WHEN i % 100 < 84 THEN 'True' ELSE 'False' END,
  CASE WHEN i % 100 < 53 THEN ''
       ELSE ELT(1 + (i % 6), 'Comprehensive','Dealer','Refer to Network',
                'Repair Shop','Standard','Superior') END,
  CASE WHEN i % {orphan_addr} = 0
       THEN CONCAT('999', LPAD(i, 10, '0'))
       ELSE CONCAT('879', LPAD((i % {addr_rows}) + 8000000000, 10, '0')) END
FROM _seq2 WHERE i >= :lo AND i < :hi
"""

CUST_SQL = """
INSERT INTO b2bcustomer (pk, originalUid, name, email, phone, active, loginDisabled,
                         creationtime, defaultB2BUnit, hwCustomerType, toolAccess,
                         sessionCurrency, sessionLanguage, sfdcContactId)
SELECT
  CONCAT('881', LPAD(i + 5000000000, 10, '0')),
  CASE WHEN i % 100 < 6 THEN CONCAT('00', i % 1000, 'linj')
       ELSE LPAD(LOWER(HEX(i + 1000000)), 16, '0') END,
  CASE WHEN i % 25 = 0 THEN CONCAT('Fr', CHAR(195), CHAR(169), 'd', CHAR(195),
                                    CHAR(169), 'ric Arnal ', i)
       ELSE CONCAT('Customer Name ', i) END,
  CONCAT('user', i, '@example', 1 + (i % 40), '.com'),
  CASE WHEN i % 100 < 16 THEN CONCAT('(', 100 + (i % 900), ') 555-', LPAD(i % 10000, 4, '0'))
       ELSE CONCAT('+1 ', 5550000000 + (i % 400000000)) END,
  CASE WHEN i % 100 < 88 THEN 'True' ELSE 'False' END,
  CASE WHEN i % 100 >= 41 AND i % 100 < 98 THEN 'True' ELSE 'False' END,
  CONCAT(LPAD(1 + (i % 28), 2, '0'), '.', LPAD(1 + (i % 12), 2, '0'), '.2025 ',
         LPAD(i % 24, 2, '0'), ':', LPAD(i % 60, 2, '0'), ':', LPAD((i * 7) % 60, 2, '0')),
  CASE WHEN i % 100 < 31 THEN ''
       WHEN i % {orphan_unit} = 0 THEN LPAD(900000000 + i, 10, '0')
       ELSE LPAD((i % {unit_rows}) + 1, 10, '0') END,
  'EXTERNAL:HoneywellCustomerType',
  CASE WHEN i % 25 = 0 THEN '' ELSE 'Online Ordering' END,
  'USD',
  CASE WHEN i % 100 < 31 THEN '' ELSE 'en' END,
  CONCAT('003', LPAD(i - (i % 50 = 0), 15, '0'))
FROM _seq2 WHERE i >= :lo AND i < :hi
"""

ADDR_SQL = """
INSERT INTO address (pk, country, postalcode, billingAddress, shippingAddress, saveAddress)
SELECT
  CONCAT('879', LPAD(i + 8000000000, 10, '0')),
  CASE WHEN i % 100 < 3  THEN ''
       WHEN i % 100 < 7  THEN 'us'
       WHEN i % 100 < 9  THEN 'USA'
       WHEN i % 20 = 0   THEN 'CA'
       ELSE 'US' END,
  CASE WHEN i % 100 < 4  THEN ''
       WHEN i % 100 < 9  THEN CONCAT(' ', LPAD(10000 + (i % 89999), 5, '0'), ' ')
       WHEN i % 100 < 12 THEN CONCAT('ZZ', i % 1000)
       WHEN i % 20 = 0   THEN CONCAT(CHAR(76 + (i % 6)), i % 10, CHAR(80 + (i % 5)),
                                     ' ', i % 10, CHAR(66 + (i % 5)), i % 10)
       WHEN i % 7 = 0    THEN CONCAT(LPAD(10000 + (i % 89999), 5, '0'), '-',
                                     LPAD(i % 10000, 4, '0'))
       ELSE LPAD(10000 + (i % 89999), 5, '0') END,
  CASE WHEN i % 100 < 6  THEN 'true'
       WHEN i % 100 < 9  THEN 'false'
       WHEN i % 50 = 0   THEN 'true' ELSE 'false' END,
  CASE WHEN i % 100 < 6  THEN 'true'
       WHEN i % 100 < 9  THEN 'false'
       WHEN i % 50 = 0   THEN 'false' ELSE 'true' END,
  CASE WHEN i % 100 < 2 THEN 'Y' ELSE 'false' END
FROM _seq2 WHERE i >= :lo AND i < :hi
"""


def generate(conn, table, sql, rows, label):
    t0 = time.time()
    done = 0
    while done < rows:
        hi = min(done + CHUNK, rows)
        conn.execute(text(sql), {"lo": done, "hi": hi})
        conn.commit()
        done = hi
        rate = done / max(time.time() - t0, .001)
        print(f"    {label:14} {done:>10,} / {rows:,}   ({rate:,.0f} rows/sec)", flush=True)
    print(f"  {label:16} {rows:>10,} rows in {time.time() - t0:,.0f}s", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=5_000_000, help="total across all three")
    ap.add_argument("--keep-existing", action="store_true")
    args = ap.parse_args()

    unit_share, cust_share, addr_share = MIX
    total_share = sum(MIX)
    units = args.rows * unit_share // total_share
    custs = args.rows * cust_share // total_share
    addrs = args.rows - units - custs

    print(f"\n  target {args.rows:,} rows total")
    print(f"    B2B Unit      {units:>10,}")
    print(f"    B2B Customer  {custs:>10,}")
    print(f"    Address       {addrs:>10,}\n")

    with source_engine.connect() as conn:
        conn.execute(text("SET SESSION cte_max_recursion_depth = 100000"))
        print("  building numbers table…", flush=True)
        build_numbers(conn, max(units, custs, addrs))
        conn.commit()

        if not args.keep_existing:
            for t, ddl in DDL.items():
                conn.execute(text(f"DROP TABLE IF EXISTS {t}"))
                conn.execute(text(ddl))
            conn.commit()

        print("\n  generating…", flush=True)
        generate(conn, "address", ADDR_SQL, addrs, "Address")
        generate(conn, "b2bunit",
                 UNIT_SQL.format(name_pool=max(units * 80 // 100, 1),
                                 orphan_addr=ORPHAN_ADDR_IN, addr_rows=addrs),
                 units, "B2B Unit")
        generate(conn, "b2bcustomer",
                 CUST_SQL.format(orphan_unit=ORPHAN_UNIT_IN, unit_rows=units),
                 custs, "B2B Customer")

        conn.execute(text("DROP TABLE IF EXISTS _seq"))
        conn.execute(text("DROP TABLE IF EXISTS _seq2"))
        conn.commit()
    print("\n  done")


if __name__ == "__main__":
    main()
