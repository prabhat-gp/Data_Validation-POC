"""
db.py
-----
Central place for database connections.
Reads credentials from a local .env file (never commit .env itself).

Usage:
    from db import get_engine
    source_engine = get_engine("source")   # source_db
    config_engine = get_engine("config")   # config_db
    target_engine = get_engine("target")   # target_db
"""

import os
from dotenv import load_dotenv
from sqlalchemy import create_engine

load_dotenv()  # reads .env in the same folder (or CWD)

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = os.getenv("DB_PORT", "3306")
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

DB_NAME_MAP = {
    "source": os.getenv("SOURCE_DB", "source_db"),
    "config": os.getenv("CONFIG_DB", "config_db"),
    "target": os.getenv("TARGET_DB", "target_db"),
}

_engines = {}  # simple cache so we don't recreate engines repeatedly


def get_engine(which: str):
    """
    which: "source" | "config" | "target"
    Returns a SQLAlchemy Engine connected to that specific database.
    """
    if which not in DB_NAME_MAP:
        raise ValueError(f"Unknown db key '{which}'. Use one of {list(DB_NAME_MAP)}")

    if which in _engines:
        return _engines[which]

    db_name = DB_NAME_MAP[which]
    url = f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{db_name}"
    engine = create_engine(url, pool_pre_ping=True)
    _engines[which] = engine
    return engine


def test_connections():
    """Quick sanity check - call this first to confirm all 3 DBs are reachable."""
    import pandas as pd

    for key in DB_NAME_MAP:
        try:
            eng = get_engine(key)
            with eng.connect() as conn:
                result = pd.read_sql("SELECT DATABASE() AS db", conn)
            print(f"[OK] {key:8s} -> connected to '{result['db'][0]}'")
        except Exception as e:
            print(f"[FAIL] {key:8s} -> {e}")


if __name__ == "__main__":
    test_connections()