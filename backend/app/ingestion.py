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

from sqlalchemy import text
from sqlalchemy.orm import Session

from .models import ENTITIES, staging_model, staging_table_name

BATCH_SIZE = 5000

# Set at deploy time (env var / Key Vault / Docker secret). Absent in dev,
# in which case db_fetch runs fail with a clear message instead of silently
# falling back to something unexpected.
SOURCE_DB_URL = os.getenv("SOURCE_DB_URL")


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


def stage_from_db(db: Session, run_id: int, entity_name: str) -> int:
    """
    Pulls an entity straight from the configured source database.

    The SELECT is GENERATED from the entity catalog -- the user picks an
    entity, never writes SQL and never supplies credentials. Only the CDE
    columns are selected, so a wide source table transfers a fraction of the
    bytes a full CSV export would.
    """
    from sqlalchemy import create_engine

    if not SOURCE_DB_URL:
        raise ValueError(
            "No source database configured. Set the SOURCE_DB_URL environment "
            "variable on the server to enable 'Run from Database'."
        )

    meta = _entity_meta(entity_name)
    columns = meta["columns"]
    key_col = meta["primary_key_field"]
    select_cols = ", ".join([key_col] + columns)
    query = f"SELECT {select_cols} FROM {meta['source_object_name']}"

    src_engine = create_engine(SOURCE_DB_URL)
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
