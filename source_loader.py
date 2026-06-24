"""
source_loader.py
-----------------
New ask: "source can be a database OR a file dump."

This module is the single entry point for getting source data into a
pandas DataFrame, regardless of where it physically lives. Everything
downstream (rule_checks.py, engine.py, rule_suggester.py) only ever
deals with DataFrames - it doesn't care or know whether the data came
from MySQL or a CSV sitting on disk.

Usage:
    from source_loader import load_source

    df = load_source(kind="database", table_name="b2bcustomer")
    df = load_source(kind="file", file_path="data/customers.csv")
    df = load_source(kind="file", file_path="data/customers.xlsx", sheet_name="Sheet1")
    df = load_source(kind="file", file_path="data/customers.json")
"""

import os
import pandas as pd

from db import get_engine


SUPPORTED_FILE_TYPES = (".csv", ".xlsx", ".xls", ".json")


def load_source(kind: str, table_name: str = None, file_path: str = None,
                 sheet_name: str = 0, db_key: str = "source") -> pd.DataFrame:
    """
    kind        : "database" or "file"
    table_name  : required if kind == "database"
    file_path   : required if kind == "file"
    sheet_name  : optional, only used for Excel files (default = first sheet)
    db_key      : which DB connection to use if kind == "database" (default "source")

    Returns a pandas DataFrame either way.
    """
    if kind == "database":
        if not table_name:
            raise ValueError("table_name is required when kind='database'")
        return _load_from_database(table_name, db_key)

    if kind == "file":
        if not file_path:
            raise ValueError("file_path is required when kind='file'")
        return _load_from_file(file_path, sheet_name)

    raise ValueError(f"Unknown kind '{kind}'. Use 'database' or 'file'.")


def _load_from_database(table_name: str, db_key: str) -> pd.DataFrame:
    engine = get_engine(db_key)
    with engine.connect() as conn:
        return pd.read_sql(f"SELECT * FROM {table_name}", conn)


def _load_from_file(file_path: str, sheet_name) -> pd.DataFrame:
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    ext = os.path.splitext(file_path)[1].lower()

    if ext not in SUPPORTED_FILE_TYPES:
        raise ValueError(
            f"Unsupported file type '{ext}'. Supported: {SUPPORTED_FILE_TYPES}"
        )

    if ext == ".csv":
        return pd.read_csv(file_path)

    if ext in (".xlsx", ".xls"):
        return pd.read_excel(file_path, sheet_name=sheet_name)

    if ext == ".json":
        # Handles both a flat list of records [{...}, {...}]
        # and line-delimited JSON, falling back gracefully.
        try:
            return pd.read_json(file_path)
        except ValueError:
            return pd.read_json(file_path, lines=True)


def get_source_metadata(df: pd.DataFrame) -> dict:
    """
    Lightweight description of a loaded source, useful for logging
    and as input to the rule_suggester module later.
    """
    return {
        "n_rows": len(df),
        "n_columns": len(df.columns),
        "columns": list(df.columns),
        "dtypes": {col: str(dtype) for col, dtype in df.dtypes.items()},
    }


if __name__ == "__main__":
    # Quick manual test against the existing source_db table
    df = load_source(kind="database", table_name="b2bcustomer")
    print(f"Loaded {len(df)} rows from database table 'b2bcustomer'")
    print(df.head())
    print("\nMetadata:", get_source_metadata(df))