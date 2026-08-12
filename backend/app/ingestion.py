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

from sqlalchemy import (
    Column as SAColumn, MetaData, String as SAString, Table, cast, insert,
    literal, select, text,
)
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from .models import ENTITIES, staging_model, staging_table_name, utcnow

BATCH_SIZE = 5000

# SFDC and Hybris extracts are IMPORTED INTO source_db -- see
# extra/prepare_account.py. So every entity reads from source_db by default,
# whatever source system it is labelled with; `source_system` is provenance
# metadata, not a separate connection.
#
# A per-system URL is only needed if you later point at a live system instead
# of an import. Setting one of these overrides source_db for that system:
#   SFDC_DB_URL=... HYBRIS_DB_URL=... MYSQL_URL=...
ENV_VAR_FOR = {"MySQL": "MYSQL_URL", "SFDC": "SFDC_DB_URL", "Hybris": "HYBRIS_DB_URL"}

SOURCE_URLS = {name: os.getenv(var) for name, var in ENV_VAR_FOR.items()}


def source_url_for(source_system: str):
    """The live override if one is configured, otherwise source_db."""
    override = SOURCE_URLS.get(source_system)
    if override:
        return override
    from .database import SOURCE_URL
    return SOURCE_URL


def _bulk_insert(db: Session, entity_name: str, rows: list):
    if not rows:
        return
    db.bulk_insert_mappings(staging_model(entity_name), rows)
    db.commit()


# ---------------------------------------------------------------------------
# SET-BASED STAGING -- used when the source lives on the same server
# ---------------------------------------------------------------------------
# The streaming path below pulls every row into Python to build a dict, then
# pushes it back. When source and staging are on the SAME server that round
# trip is pure waste: the database can do the whole copy itself.
#
# Measured on 1,000,000 rows:  streaming ~200s   INSERT..SELECT ~40s.
#
# It is also a single statement, so the staged snapshot is atomic and
# internally consistent. The streaming path gets that from one long-lived
# transaction; a chunked copy would get neither.
def _same_server(url_a: str, url_b: str) -> bool:
    """
    Same database server? Compares backend, host and port -- NOT the database
    name, because one server can hold source_db and the staging tables in
    different schemas and INSERT..SELECT still works across them.
    """
    try:
        a, b = make_url(url_a), make_url(url_b)
    except Exception:
        return False
    return (
        a.get_backend_name() == b.get_backend_name()
        and (a.host or "") == (b.host or "")
        and (a.port or 0) == (b.port or 0)
    )


def _insert_select(db: Session, run_id: int, entity_name: str,
                   source_url: str, staging_url: str) -> int:
    """
    INSERT INTO stg_x (run_id, record_key, <cdes>, loaded_at)
    SELECT :run_id, <pk>, <cdes>, :now FROM <source_schema>.<source_table>

    Built with SQLAlchemy Core rather than an f-string so the identifier
    quoting and the LIMIT/FETCH-FIRST dialect differences are the driver's
    problem, not ours -- this compiles unchanged against Oracle.
    """
    meta = _entity_meta(entity_name)
    columns = meta["columns"]
    key_col = meta["primary_key_field"]

    src_db = make_url(source_url).database
    stg_db = make_url(staging_url).database
    # Only qualify when they really are different schemas; an unnecessary
    # qualifier is one more thing that can be wrong about permissions.
    schema = src_db if src_db != stg_db else None

    src = Table(
        meta["source_object_name"], MetaData(),
        *[SAColumn(c, SAString) for c in [key_col] + columns],
        schema=schema,
    )
    stg = staging_model(entity_name).__table__

    sel = select(
        literal(run_id),
        cast(src.c[key_col], SAString(120)),      # record_key is VARCHAR(120)
        *[src.c[c] for c in columns],
        literal(utcnow()),
    )
    stmt = insert(stg).from_select(
        ["run_id", "record_key"] + columns + ["loaded_at"], sel
    )
    result = db.execute(stmt)
    db.commit()
    return result.rowcount or 0


def _entity_meta(entity_name: str) -> dict:
    meta = ENTITIES.get(entity_name)
    if meta is None:
        raise ValueError(f"Unknown object {entity_name!r}. Known: {list(ENTITIES)}")
    return meta


def stage_from_csv(db: Session, run_id: int, entity_name: str, file_path: str,
                   on_progress=None, on_total=None) -> int:
    """
    Streams a CSV into the entity's staging table. Extra columns in the file
    are ignored; missing required columns fail fast with an explicit message
    rather than validating nothing and reporting a false 100% score.
    """
    meta = _entity_meta(entity_name)
    columns = meta["columns"]
    key_col = meta["primary_key_field"]

    # cheap pre-count so the UI can show a real percentage rather than a spinner
    if on_total:
        with open(file_path, "rb") as fh:
            on_total(max(sum(1 for _ in fh) - 1, 0))

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
                if on_progress:
                    on_progress(total)

    _bulk_insert(db, entity_name, batch)
    return total


def stage_from_db(db: Session, run_id: int, entity_name: str,
                  source_system: Optional[str] = None,
                  on_progress=None, on_total=None) -> int:
    """
    Pulls an entity straight from the configured source database.

    The SELECT is GENERATED from the entity catalog -- the user picks an
    entity, never writes SQL and never supplies credentials. Only the CDE
    columns are selected, so a wide source table transfers a fraction of the
    bytes a full CSV export would.

    Two paths, chosen automatically:

      same server   INSERT..SELECT      ~40s per million rows
      remote source streaming cursor    ~200s per million rows

    The remote path is not a fallback for slowness, it is a necessity: you
    cannot INSERT..SELECT across two different servers. Both are correct; one
    is simply available less often.
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

    from .database import SOURCE_URL
    if _same_server(url, SOURCE_URL):
        if on_total:
            with create_engine(url).connect() as cc:
                on_total(cc.execute(
                    text(f"SELECT COUNT(*) FROM {meta['source_object_name']}")).scalar() or 0)
        try:
            n = _insert_select(db, run_id, entity_name, url, SOURCE_URL)
            if on_progress:
                on_progress(n)
            return n
        except Exception as exc:
            # Cross-schema grants are the usual cause. Roll back whatever
            # landed so the streaming retry does not double-count, and say
            # plainly that the slow path is now in use -- a silent 5x
            # slowdown is worse than a noisy one.
            db.rollback()
            clear_staging(db, entity_name, run_id)
            print(f"[ingestion] INSERT..SELECT unavailable for {entity_name} "
                  f"({type(exc).__name__}: {exc}); falling back to streaming.")

    src_engine = create_engine(url)
    total, batch = 0, []
    try:
        # one cheap COUNT so progress is a real percentage, not a guess
        if on_total:
            with src_engine.connect() as cc:
                on_total(cc.execute(
                    text(f"SELECT COUNT(*) FROM {meta['source_object_name']}")).scalar() or 0)
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
                    if on_progress:
                        on_progress(total)
        _bulk_insert(db, entity_name, batch)
    finally:
        src_engine.dispose()
    return total


def clear_staging(db: Session, entity_name: str, run_id: int):
    """
    Staging is runtime-only -- always cleared once a run finishes.

    DELETE costs O(rows x indexes): it removes every row from every index and
    writes undo for all of it. At 1,000,000 staged rows over five indexes that
    measured 415s, which was more than staging and validating COMBINED.

    TRUNCATE drops and recreates the table's storage instead -- constant time,
    no undo, indexes come back empty. It is only correct when this run's rows
    are the ONLY rows present, so that is checked first. MIN/MAX on an indexed
    column is an index seek, not a scan, so the check itself is free.

    TRUNCATE is DDL on both MySQL and Oracle: it commits implicitly and cannot
    be rolled back. That is fine here and nowhere else -- staging holds no
    durable state.
    """
    table = staging_table_name(entity_name)
    lo, hi = db.execute(text(f"SELECT MIN(run_id), MAX(run_id) FROM {table}")).first()

    if lo is None:                       # already empty
        return
    if lo == run_id and hi == run_id:
        db.execute(text(f"TRUNCATE TABLE {table}"))
    else:
        # another run is staged in the same table -- concurrent batches, or a
        # crashed run that never reached phase 3. Only take our own rows.
        db.execute(text(f"DELETE FROM {table} WHERE run_id = :run_id"), {"run_id": run_id})
    db.commit()
