"""
database.py
-----------
Single place for the DB connection. Default is a local SQLite file so this
runs with zero setup on any laptop (dev machine or office laptop against the
real 700MB accounts.csv). Swapping to Oracle/Postgres/MySQL later is just an
env var change -- nothing else in the codebase should ever import a driver
directly.

    DATABASE_URL=sqlite:///./smtc_dq.db                (fallback)
    DATABASE_URL=oracle+oracledb://user:pass@host/service
    DATABASE_URL=postgresql+psycopg://user:pass@host/db

RESOLUTION ORDER
  1. DATABASE_URL, if set explicitly
  2. the project's DB_* + CONFIG_DB variables (MySQL config_db)
  3. local SQLite file

That middle step is what puts val_rules / val_runs / val_metrics in the real
config_db rather than a laptop-local SQLite file.
"""

import os
import re
from urllib.parse import quote_plus

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

# Same .env files ingestion.py reads: repo root first, backend/ overrides.
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_BACKEND_DIR)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_REPO_ROOT, ".env"))
    load_dotenv(os.path.join(_BACKEND_DIR, ".env"), override=True)
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


# TWO databases, matching the .env layout:
#   CONFIG_DB  -> val_rules + its lookup tables      (the governed asset)
#   TARGET_DB  -> val_batches / val_runs / val_metrics / val_violations + staging
CONFIG_URL = os.getenv("CONFIG_DATABASE_URL") or _mysql_url("CONFIG_DB") or "sqlite:///./config.db"
RESULTS_URL = os.getenv("RESULTS_DATABASE_URL") or _mysql_url("TARGET_DB") or "sqlite:///./results.db"


def _make(url):
    args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    eng = create_engine(url, connect_args=args, future=True, pool_pre_ping=True)
    if url.startswith("sqlite"):
        # VALIDITY rules call REGEXP(pattern, value); SQLite has no built-in one.
        @event.listens_for(eng, "connect")
        def _add_regexp(conn, _):
            conn.create_function(
                "REGEXP", 2,
                lambda p, v: 1 if v is not None and re.search(p, v) else 0,
            )
    return eng


config_engine = _make(CONFIG_URL)
results_engine = _make(RESULTS_URL)

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
