#!/usr/bin/env python3
"""
ShopFlow API (middle tier) -- runs on vcf-middle01, behind nginx on
vcf-frontend01.

Endpoints:
  GET  /healthz                  -- liveness for nginx upstream checks
  GET  /api/catalog/products     -- full product list (cached, healthy TTL)
  GET  /api/catalog/featured     -- "trending now" panel (cached, TTL is
                                     misconfigured far too low -- see below)
  POST /api/checkout             -- create an order, enqueue it for the
                                     worker process to invoice asynchronously

No metrics library here on purpose -- VCF Operations gets its signal from
the standard OS/PostgreSQL/Redis/nginx Telegraf inputs (see
monitoring/METRICS.md), not from anything this app exposes itself. What
this app does still log (request path + duration, cache hit/miss) is
plain text to stdout, captured by journald, for the log-reading part of
an investigation.

Run with gunicorn, see gunicorn_conf.py.
"""
import contextlib
import json
import logging
import os
import sys
import time

import redis
from flask import Flask, jsonify, request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))
from dbutil import get_pool  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s api %(levelname)s %(message)s")
log = logging.getLogger("shopflow-api")

app = Flask(__name__)

REDIS_HOST = os.environ.get("SHOPFLOW_REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("SHOPFLOW_REDIS_PORT", "6379"))
QUEUE_KEY = os.environ.get("SHOPFLOW_QUEUE_KEY", "shopflow:orders")

# --- Cache TTLs ------------------------------------------------------
# PRODUCTS_TTL is a normal, sane cache lifetime for a catalog listing
# that changes rarely.
#
# FEATURED_TTL backs the homepage "trending now" panel, which is hit by
# nearly every page load. It was set low during a since-forgotten A/B
# test ("we needed fresher trending data for the demo") and never
# reverted. On every expiry, all concurrently-inflight requests miss the
# cache at once and recompute the trending_products aggregate directly
# against vcf-db01 -- a classic cache stampede, independent of (and much
# smaller/higher-frequency than) the connection-leak issue in worker.py.
PRODUCTS_TTL = int(os.environ.get("SHOPFLOW_PRODUCTS_TTL", "300"))
FEATURED_TTL = int(os.environ.get("SHOPFLOW_FEATURED_TTL", "8"))

rds = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


@contextlib.contextmanager
def db_conn():
    """Healthy pattern: always returns the connection to the pool, even on
    error. Contrast with worker.py's manual checkout."""
    pool = get_pool()
    conn = pool.getconn()
    try:
        yield conn
    finally:
        pool.putconn(conn)


@app.before_request
def _start_timer():
    request._start_time = time.time()


@app.after_request
def _log_request(response):
    elapsed_ms = (time.time() - getattr(request, "_start_time", time.time())) * 1000
    log.info("%s %s -> %s (%.1fms)", request.method, request.path, response.status_code, elapsed_ms)
    return response


@app.route("/healthz")
def healthz():
    return jsonify(status="ok")


@app.route("/api/catalog/products")
def catalog_products():
    cache_key = "shopflow:cache:products"
    cached = rds.get(cache_key)
    if cached:
        return jsonify(json.loads(cached))

    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT product_id, sku, name, category, unit_price, is_clearance "
            "FROM products ORDER BY product_id"
        )
        rows = cur.fetchall()

    result = [
        {
            "product_id": r[0], "sku": r[1], "name": r[2],
            "category": r[3], "unit_price": float(r[4]), "is_clearance": r[5],
        }
        for r in rows
    ]
    rds.setex(cache_key, PRODUCTS_TTL, json.dumps(result))
    return jsonify(result)


@app.route("/api/catalog/featured")
def catalog_featured():
    cache_key = "shopflow:cache:featured"
    cached = rds.get(cache_key)
    if cached:
        return jsonify(json.loads(cached))

    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT product_id, sku, name, unit_price, order_count, units_sold "
            "FROM trending_products LIMIT 10"
        )
        rows = cur.fetchall()

    result = [
        {
            "product_id": r[0], "sku": r[1], "name": r[2],
            "unit_price": float(r[3]), "order_count": r[4], "units_sold": int(r[5]),
        }
        for r in rows
    ]
    rds.setex(cache_key, FEATURED_TTL, json.dumps(result))
    return jsonify(result)


@app.route("/api/checkout", methods=["POST"])
def checkout():
    payload = request.get_json(force=True)
    customer_id = payload["customer_id"]
    product_id = payload["product_id"]
    quantity = int(payload["quantity"])
    promo_code = payload.get("promo_code")

    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT unit_price FROM products WHERE product_id = %s", (product_id,))
        row = cur.fetchone()
        if row is None:
            return jsonify(error="unknown product_id"), 404
        unit_price = float(row[0])
        subtotal = round(unit_price * quantity, 2)

        cur.execute(
            "INSERT INTO orders (customer_id, product_id, quantity, promo_code, subtotal, status) "
            "VALUES (%s, %s, %s, %s, %s, 'PENDING') RETURNING order_id",
            (customer_id, product_id, quantity, promo_code, subtotal),
        )
        order_id = cur.fetchone()[0]
        conn.commit()

    job = {
        "order_id": order_id,
        "product_id": product_id,
        "quantity": quantity,
        "promo_code": promo_code,
        "subtotal": subtotal,
    }
    rds.rpush(QUEUE_KEY, json.dumps(job))

    return jsonify(order_id=order_id, status="PENDING"), 202


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
