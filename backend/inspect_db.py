"""
inspect_db.py
-------------
Look at the database from the command line, identically on Windows and macOS.

The `sqlite3` CLI ships with macOS but NOT with Windows, and installing it
there is a nuisance. This script uses Python's built-in sqlite3 module, so it
works anywhere the backend already runs -- no extra install, no OS-specific
quoting, no PATH surprises.

USAGE (same on every OS)
    python inspect_db.py                    # summary: row count per table
    python inspect_db.py val_rules          # all rows of a table
    python inspect_db.py val_rules 5        # first 5 rows
    python inspect_db.py --sql "SELECT ..." # any query

macOS/Linux : python3 inspect_db.py val_rules
Windows     : python inspect_db.py val_rules
"""

import os
import sqlite3
import sys

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smtc_dq.db")

TABLES = [
    "val_rules", "val_rule_types", "val_severities", "val_statuses",
    "val_batches", "val_runs", "val_metrics", "val_violations",
]


def connect():
    if not os.path.exists(DB_PATH):
        print(f"No database at {DB_PATH}\nRun:  python create_tables.py")
        sys.exit(1)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def print_rows(rows, limit=None):
    """Plain aligned columns -- no external table library needed."""
    rows = list(rows)
    if not rows:
        print("  (no rows)")
        return
    if limit:
        rows = rows[:limit]

    cols = rows[0].keys()
    widths = {
        c: min(max(len(c), *(len(str(r[c])) for r in rows)), 40) for c in cols
    }
    header = "  ".join(c.ljust(widths[c])[: widths[c]] for c in cols)
    print("  " + header)
    print("  " + "  ".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  " + "  ".join(str(r[c]).ljust(widths[c])[: widths[c]] for c in cols))
    print(f"\n  {len(rows)} row(s)")


def summary(con):
    print(f"\nDatabase: {DB_PATH}\n")
    print("  TABLE                     ROWS")
    print("  " + "-" * 32)
    for t in TABLES:
        try:
            n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            n = "-"
        print(f"  {t:<24}  {n}")

    stg = [
        r["name"] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'stg_%'"
        )
    ]
    if stg:
        print("\n  STAGING (cleared after every run)")
        print("  " + "-" * 32)
        for t in stg:
            n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  {t:<24}  {n}")
    print("\nTip:  python inspect_db.py val_rules")


def main():
    con = connect()
    args = sys.argv[1:]

    if not args:
        summary(con)
    elif args[0] == "--sql":
        if len(args) < 2:
            print('Usage: python inspect_db.py --sql "SELECT * FROM val_rules"')
            sys.exit(1)
        print_rows(con.execute(args[1]).fetchall())
    else:
        table = args[0]
        limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
        try:
            print_rows(con.execute(f"SELECT * FROM {table}").fetchall(), limit)
        except sqlite3.OperationalError as exc:
            print(f"  {exc}\n  Known tables: {', '.join(TABLES)}")
    con.close()


if __name__ == "__main__":
    main()
