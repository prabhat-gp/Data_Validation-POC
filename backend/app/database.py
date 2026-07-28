"""
database.py
-----------
Single place for the DB connection. Default is a local SQLite file so this
runs with zero setup on any laptop (dev machine or office laptop against the
real 700MB accounts.csv). Swapping to Oracle/Postgres/MySQL later is just an
env var change -- nothing else in the codebase should ever import a driver
directly.

    DATABASE_URL=sqlite:///./smtc_dq.db                (default)
    DATABASE_URL=oracle+oracledb://user:pass@host/service
    DATABASE_URL=postgresql+psycopg://user:pass@host/db
"""

import os
import re
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./smtc_dq.db")

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args, future=True)

if DATABASE_URL.startswith("sqlite"):
    # format_pattern rules use REGEXP(pattern, value) in SQL. SQLite has no
    # built-in REGEXP, so register one per connection. On Oracle/Postgres the
    # rule compiler should instead emit REGEXP_LIKE(...) / the `~` operator --
    # this is the one place that's genuinely dialect-specific.
    @event.listens_for(engine, "connect")
    def _register_regexp(dbapi_connection, _):
        def regexp(pattern, value):
            if value is None:
                return False
            return re.search(pattern, value) is not None

        dbapi_connection.create_function("REGEXP", 2, regexp)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db():
    """FastAPI dependency: one session per request, always closed."""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
