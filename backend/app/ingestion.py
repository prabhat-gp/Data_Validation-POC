"""
ingestion.py
------------
Loads source data into STG_SOURCE_RECORD for one run, from either a CSV
upload or a direct DB fetch. Both paths stream in fixed-size batches -- never
load the whole source into memory. This matters for real files (the actual
office-laptop accounts.csv is ~700MB); it's irrelevant for temp.csv (101
rows) but the code path is identical either way, which is the point: dev
testing on temp.csv exercises the exact same chunked-insert logic that runs
against the real file.

Only the 16 CDE columns + the record key are read out of the source -- this
is the "column pruning" decision: never pull 443 columns when 17 are needed.
"""

import csv
from typing import Iterable, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from .models import CDE_COLUMNS, StgSourceRecord

BATCH_SIZE = 5000


def _bulk_insert(db: Session, run_id: int, rows: list[dict]):
    if not rows:
        return
    db.bulk_insert_mappings(StgSourceRecord, rows)
    db.commit()


def stage_from_csv(db: Session, run_id: int, file_path: str, record_key_column: str) -> int:
    """Streams a CSV file into staging in fixed batches. Returns rows staged."""
    total = 0
    batch: list[dict] = []

    with open(file_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = [c for c in CDE_COLUMNS if c not in reader.fieldnames]
        if missing:
            raise ValueError(f"Uploaded file is missing expected CDE columns: {missing}")
        if record_key_column not in reader.fieldnames:
            raise ValueError(f"Uploaded file is missing the record key column: {record_key_column}")

        for row in reader:
            staged = {"run_id": run_id, "record_key": row.get(record_key_column, "")}
            for col in CDE_COLUMNS:
                staged[col] = row.get(col)
            batch.append(staged)
            total += 1
            if len(batch) >= BATCH_SIZE:
                _bulk_insert(db, run_id, batch)
                batch = []

    _bulk_insert(db, run_id, batch)
    return total


def stage_from_db(
    db: Session,
    run_id: int,
    source_connection_url: str,
    source_query: str,
    record_key_column: str,
) -> int:
    """
    Streams rows from an arbitrary external DB (a plain SELECT the user
    provides, expected to return the record key + the 16 CDE columns) into
    staging in fixed batches, using a server-side cursor so the whole result
    set is never materialized in memory at once.
    """
    from sqlalchemy import create_engine

    src_engine = create_engine(source_connection_url)
    total = 0
    batch: list[dict] = []

    with src_engine.connect().execution_options(stream_results=True) as conn:
        result = conn.execute(text(source_query))
        columns = result.keys()
        missing = [c for c in CDE_COLUMNS if c not in columns]
        if missing:
            raise ValueError(f"Source query result is missing expected CDE columns: {missing}")
        if record_key_column not in columns:
            raise ValueError(f"Source query result is missing the record key column: {record_key_column}")

        for row in result:
            row_map = dict(zip(columns, row))
            staged = {"run_id": run_id, "record_key": row_map.get(record_key_column, "")}
            for col in CDE_COLUMNS:
                staged[col] = row_map.get(col)
            batch.append(staged)
            total += 1
            if len(batch) >= BATCH_SIZE:
                _bulk_insert(db, run_id, batch)
                batch = []

    _bulk_insert(db, run_id, batch)
    src_engine.dispose()
    return total


def clear_staging(db: Session, run_id: int):
    """STG_SOURCE_RECORD is runtime-only -- always cleared after a run finishes."""
    db.execute(text("DELETE FROM stg_source_record WHERE run_id = :run_id"), {"run_id": run_id})
    db.commit()
