"""
database.py
-----------
Single place for the DB connections. Nothing else in the codebase imports a
driver directly, so moving to Oracle/Postgres later is an env var change here.

THREE databases:
    SOURCE_DB   the imported SFDC / Hybris tables AND the stg_* staging tables
    CONFIG_DB   val_rules + its lookup tables
    RESULTS_DB  val_batches / val_runs / val_metrics / val_violations

Staging lives in SOURCE_DB, beside the data it is staged from, so a compiled
rule never crosses a database boundary while it runs.

    CONFIG_DATABASE_URL / RESULTS_DATABASE_URL / SOURCE_DATABASE_URL  full URLs
    DB_HOST / DB_PORT / DB_USER / DB_PASSWORD  + SOURCE_DB / CONFIG_DB / RESULTS_DB

If neither resolves, startup FAILS. There is deliberately no local-file
fallback: a misread .env would otherwise start cleanly against an empty
throwaway database, and the only symptom would be a dashboard showing
nothing -- which looks like a bug in the engine, not a missing config.
"""

import os
from urllib.parse import quote_plus

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Same .env files ingestion.py reads: repo root first, backend/ overrides.
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_BACKEND_DIR)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_BACKEND_DIR, ".env"))
except ImportError:
    pass


def _mysql_url(db_name_var: str):
    host, db = os.getenv("DB_HOST"), os.getenv(db_name_var)
    if not host or not db:
        return None
    user = os.getenv("DB_USER", "root")
    pwd = quote_plus(os.getenv("DB_PASSWORD", ""))
    port = os.getenv("DB_PORT", "3306")
    return f"mysql+pymysql://{user}:{pwd}@{host}:{port}/{db}"


def _require(url, name, db_var):
    if url:
        return url
    raise RuntimeError(
        f"No connection for {name}. Set {db_var} (plus DB_HOST/DB_PORT/DB_USER/"
        f"DB_PASSWORD) in .env, or {name}_DATABASE_URL for a full URL."
    )


SOURCE_URL = _require(
    os.getenv("SOURCE_DATABASE_URL") or _mysql_url("SOURCE_DB"), "SOURCE", "SOURCE_DB")
CONFIG_URL = _require(
    os.getenv("CONFIG_DATABASE_URL") or _mysql_url("CONFIG_DB"), "CONFIG", "CONFIG_DB")
# TARGET_DB is the old name for RESULTS_DB -- accepted so an existing .env keeps working.
RESULTS_URL = _require(
    os.getenv("RESULTS_DATABASE_URL") or _mysql_url("RESULTS_DB") or _mysql_url("TARGET_DB"),
    "RESULTS", "RESULTS_DB")


def _make(url):
    return create_engine(url, future=True, pool_pre_ping=True)


source_engine = _make(SOURCE_URL)
config_engine = _make(CONFIG_URL)
results_engine = _make(RESULTS_URL)

# StagingSession writes and reads the stg_* tables in source_db. Every compiled
# rule runs on THIS session -- it selects from staging and, for referential
# integrity, joins another staging table, so both sides must be in one database.
StagingSession = sessionmaker(bind=source_engine, autoflush=False, future=True)
ConfigSession = sessionmaker(bind=config_engine, autoflush=False, future=True)
ResultsSession = sessionmaker(bind=results_engine, autoflush=False, future=True)

# Back-compat aliases so existing imports keep working.
engine = results_engine
SessionLocal = ResultsSession
DATABASE_URL = RESULTS_URL


def get_config_db():
    db = ConfigSession()
    try:
        yield db
    finally:
        db.close()


def get_results_db():
    db = ResultsSession()
    try:
        yield db
    finally:
        db.close()


# most routers read results; rules.py/entities.py override with get_config_db
get_db = get_results_db
