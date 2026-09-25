"""
Shared DB connection pool helper used by both the API process and the
worker process on the middle-tier VM. Pool size is intentionally small
and configurable via environment variables so an instructor can tune how
quickly the lab scenario plays out on a given VM size. See
INSTRUCTOR_GUIDE.md for sizing guidance.
"""
import os

from psycopg2.pool import ThreadedConnectionPool

_pool = None


def get_pool():
    global _pool
    if _pool is None:
        dsn = os.environ.get(
            "SHOPFLOW_DB_DSN",
            "host=vcf-db01 dbname=shopflow user=shopflow password=shopflow",
        )
        # Tag connections by process so pg_stat_activity can tell the API
        # process's connections apart from the worker's, even though both
        # run on the same VM -- no app-level metrics needed to answer
        # "which tier is holding these connections open?".
        app_name = os.environ.get("SHOPFLOW_APP_NAME", "shopflow")
        dsn = f"{dsn} application_name={app_name}"
        minconn = int(os.environ.get("SHOPFLOW_DB_POOL_MIN", "2"))
        maxconn = int(os.environ.get("SHOPFLOW_DB_POOL_MAX", "15"))
        _pool = ThreadedConnectionPool(minconn, maxconn, dsn)
    return _pool
