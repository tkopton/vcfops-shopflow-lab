"""
Shared DB connection helpers used by both the API process and the worker
process on the middle-tier VM.

get_pool() -- a pooled connection source, used by worker.py. Pool size is
configurable via environment variables so an instructor can tune how
quickly the lab scenario plays out on a given VM size. See
INSTRUCTOR_GUIDE.md for sizing guidance.

new_connection() -- a single, unpooled connection, used by app.py (the
API). Deliberately NOT pooled: see middle/app.py's db_conn() for why a
pooled API would (once warmed) keep working fine regardless of whether
Postgres can accept new connections, which would hide this exercise's
intended symptom rather than show it.
"""
import os

import psycopg2
from psycopg2.pool import ThreadedConnectionPool

_pool = None


def _build_dsn():
    dsn = os.environ.get(
        "SHOPFLOW_DB_DSN",
        "host=vcf-db01 dbname=shopflow user=shopflow password=shopflow%shopflow%",
    )
    # Tag connections by process so pg_stat_activity can tell the API
    # process's connections apart from the worker's, even though both
    # run on the same VM -- no app-level metrics needed to answer
    # "which tier is holding these connections open?".
    app_name = os.environ.get("SHOPFLOW_APP_NAME", "shopflow")
    return f"{dsn} application_name={app_name}"


def get_pool():
    global _pool
    if _pool is None:
        minconn = int(os.environ.get("SHOPFLOW_DB_POOL_MIN", "2"))
        maxconn = int(os.environ.get("SHOPFLOW_DB_POOL_MAX", "15"))
        _pool = ThreadedConnectionPool(minconn, maxconn, _build_dsn())
    return _pool


def new_connection():
    """A fresh, unpooled connection -- the caller is expected to close it
    when done (see middle/app.py's db_conn()). Opening one is a live test
    of whether Postgres can accept a new connection right now."""
    return psycopg2.connect(_build_dsn())
