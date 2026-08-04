"""
ingestion.py
------------
Gets source data into the per-entity staging table.

Two properties that matter at 10M+ rows:

1. STREAMING -- csv.DictReader iterates the file; a server-side cursor
   iterates the source DB. The full dataset is never materialised in memory.
   Rows are inserted in fixed batches (row-at-a-time inserts would take
   10-20+ minutes where bulk chunks take seconds).

2. COLUMN PRUNING -- only the entity's declared CDE columns are kept. The
   full Account export is 443 columns wide; we stage ~17. At ~13 KB/row that
   is the difference between roughly 63 GB and 6 GB at 5M rows.

SECURITY: the source database connection comes from SERVER CONFIG
(SOURCE_DB_URL env var), never from an API request. A user selects an ENTITY;
the query is generated here. Users never see, type, or transmit a connection
string, and no user-supplied SQL is executed against the source system.
"""

import csv
import os
from typing import Optional
from urllib.parse import quote_plus

# Credentials come from a gitignored .env, not a shell command -- that keeps
# them out of shell history and works identically on Windows and macOS
# (`VAR=value cmd` is bash-only syntax).
#
# Both locations are read, repo root first, so the project's existing root
# .env (DB_HOST / DB_USER / DB_PASSWORD / SOURCE_DB) keeps working as-is.
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_BACKEND_DIR)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_REPO_ROOT, ".env"))
    load_dotenv(os.path.join(_BACKEND_DIR, ".env"), override=True)
except ImportError:
    pass


def _mysql_url_from_parts():
    """
    Build a SQLAlchemy URL from the discrete DB_* variables this project
    already had, so nothing needs duplicating. An explicit MYSQL_URL still
    wins if it is set.
    """
    host = os.getenv("DB_HOST")
    db = os.getenv("SOURCE_DB")
    if not host or not db:
        return None
    user = os.getenv("DB_USER", "root")
    pwd = quote_plus(os.getenv("DB_PASSWORD", ""))
    port = os.getenv("DB_PORT", "3306")
    return f"mysql+pymysql://{user}:{pwd}@{host}:{port}/{db}"

from sqlalchemy import text
from sqlalchemy.orm import Session

from .models import ENTITIES, staging_model, staging_table_name

BATCH_SIZE = 5000

# One connection string PER SOURCE SYSTEM, set at deploy time. Absent ones make
# a db_fetch fail with a clear message rather than silently falling back.
#
# MySQL example (the source_db holding the b2b* tables):
#   MYSQL_URL="mysql+pymysql://user:pass@localhost:3306/source_db"
ENV_VAR_FOR = {"MySQL": "MYSQL_URL", "SFDC": "SFDC_DB_URL", "Hybris": "HYBRIS_DB_URL"}

SOURCE_URLS = {name: os.getenv(var) for name, var in ENV_VAR_FOR.items()}

# Fall back to the discrete DB_* variables for MySQL.
if not SOURCE_URLS.get("MySQL"):
    SOURCE_URLS["MySQL"] = _mysql_url_from_parts()

# Back-compat: a single SOURCE_DB_URL still works as the MySQL connection.
if not SOURCE_URLS["MySQL"]:
    SOURCE_URLS["MySQL"] = os.getenv("SOURCE_DB_URL")


def source_url_for(source_system: str):
    return SOURCE_URLS.get(source_system)


def _bulk_insert(db: Session, entity_name: str, rows: list):
    if not rows:
        return
    db.bulk_insert_mappings(staging_model(entity_name), rows)
    db.commit()


def _entity_meta(entity_name: str) -> dict:
    meta = ENTITIES.get(entity_name)
    if meta is None:
        raise ValueError(f"Unknown entity {entity_name!r}. Known: {list(ENTITIES)}")
    return meta


def stage_from_csv(db: Session, run_id: int, entity_name: str, file_path: str) -> int:
    """
    Streams a CSV into the entity's staging table. Extra columns in the file
    are ignored; missing required columns fail fast with an explicit message
    rather than validating nothing and reporting a false 100% score.
    """
    meta = _entity_meta(entity_name)
    columns = meta["columns"]
    key_col = meta["primary_key_field"]

    total, batch = 0, []
    with open(file_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        missing = [c for c in columns if c not in fieldnames]
        if missing:
            raise ValueError(
                f"Uploaded file for {entity_name} is missing required columns: {missing}"
            )
        if key_col not in fieldnames:
            raise ValueError(
                f"Uploaded file for {entity_name} is missing the key column: {key_col}"
            )

        for row in reader:
            staged = {"run_id": run_id, "record_key": row.get(key_col, "")}
            for col in columns:
                staged[col] = row.get(col)
            batch.append(staged)
            total += 1
            if len(batch) >= BATCH_SIZE:
                _bulk_insert(db, entity_name, batch)
                batch = []

    _bulk_insert(db, entity_name, batch)
    return total


def stage_from_db(db: Session, run_id: int, entity_name: str,
                  source_system: Optional[str] = None) -> int:
    """
    Pulls an entity straight from the configured source database.

    The SELECT is GENERATED from the entity catalog -- the user picks an
    entity, never writes SQL and never supplies credentials. Only the CDE
    columns are selected, so a wide source table transfers a fraction of the
    bytes a full CSV export would.
    """
    from sqlalchemy import create_engine

    meta = _entity_meta(entity_name)
    source_system = source_system or meta["source_system"]
    url = source_url_for(source_system)
    if not url:
        raise ValueError(
            f"No connection configured for '{source_system}'. Set "
            f"{ENV_VAR_FOR.get(source_system, 'the connection')} in backend/.env "
            f"to enable 'Run from Database'."
        )

    columns = meta["columns"]
    key_col = meta["primary_key_field"]
    select_cols = ", ".join([key_col] + columns)
    query = f"SELECT {select_cols} FROM {meta['source_object_name']}"

    src_engine = create_engine(url)
    total, batch = 0, []
    try:
        with src_engine.connect().execution_options(stream_results=True) as conn:
            result = conn.execute(text(query))
            result_cols = list(result.keys())
            for row in result:
                row_map = dict(zip(result_cols, row))
                staged = {"run_id": run_id, "record_key": row_map.get(key_col, "")}
                for col in columns:
                    staged[col] = row_map.get(col)
                batch.append(staged)
                total += 1
                if len(batch) >= BATCH_SIZE:
                    _bulk_insert(db, entity_name, batch)
                    batch = []
        _bulk_insert(db, entity_name, batch)
    finally:
        src_engine.dispose()
    return total


def clear_staging(db: Session, entity_name: str, run_id: int):
    """Staging is runtime-only -- always cleared once a run finishes."""
    table = staging_table_name(entity_name)
    db.execute(text(f"DELETE FROM {table} WHERE run_id = :run_id"), {"run_id": run_id})
    db.commit()
