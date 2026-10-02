#!/usr/bin/env python3
"""
ShopFlow order-processing worker -- runs on vcf-middle01 alongside the
API process (app.py) and the local Redis instance.

Pulls order jobs off a Redis list and turns them into invoices: it looks
up the order, applies whatever promo discount was used, writes the final
invoice_total back to Postgres, and updates inventory.

No metrics library here -- see app.py's module docstring. Structured
logs go to stdout (journald under systemd); that, plus what Postgres and
Redis report about themselves natively, is what VCF Operations has to
work with, which is the point of the exercise.
"""
import json
import logging
import logging.handlers
import os
import socket
import sys
import time

import redis

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))
from dbutil import get_pool  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s worker %(levelname)s %(message)s")
log = logging.getLogger("shopflow-worker")

REDIS_HOST = os.environ.get("SHOPFLOW_REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("SHOPFLOW_REDIS_PORT", "6379"))
QUEUE_KEY = os.environ.get("SHOPFLOW_QUEUE_KEY", "shopflow:orders")

# --- Optional syslog forwarding for order-processing failures --------
# Off by default (no application behavior here unless you set the host).
# This does NOT know about HTTP 5xx responses -- the worker never sees
# those, that's an nginx/API-tier concept. What it forwards is the
# order-processing failure that is the actual root cause of the 5xx a
# trainee eventually sees on the frontend, a few minutes upstream of it.
SYSLOG_HOST = os.environ.get("SHOPFLOW_SYSLOG_HOST")
SYSLOG_PORT = int(os.environ.get("SHOPFLOW_SYSLOG_PORT", "514"))
SYSLOG_PROTO = os.environ.get("SHOPFLOW_SYSLOG_PROTO", "udp").lower()  # udp or tcp

if SYSLOG_HOST:
    _socktype = socket.SOCK_STREAM if SYSLOG_PROTO == "tcp" else socket.SOCK_DGRAM
    _syslog_handler = logging.handlers.SysLogHandler(
        address=(SYSLOG_HOST, SYSLOG_PORT),
        facility=logging.handlers.SysLogHandler.LOG_LOCAL0,
        socktype=_socktype,
    )
    _syslog_handler.ident = "shopflow-worker: "
    # Only failures go to syslog -- the routine "order invoiced" log line
    # stays local (stdout/journald) so the syslog server isn't flooded.
    _syslog_handler.setLevel(logging.ERROR)
    _syslog_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    log.addHandler(_syslog_handler)
    log.info("syslog forwarding enabled -> %s:%d/%s", SYSLOG_HOST, SYSLOG_PORT, SYSLOG_PROTO)

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def compute_effective_unit_price(quantity: int, promo_code: str, subtotal: float) -> float:
    """Apply a promo code and return the effective price-per-billable-unit.

    Each promo works by making some units "free" and spreading the
    subtotal across the remaining billable units, which is how the
    invoice line ends up showing a discounted per-unit price instead of
    a separate discount line (finance prefers it this way for the
    clearance/flash-sale codes).
    """
    free_units = 0
    if promo_code == "FLASH2026":
        free_units = quantity // 3          # 1 free per 3 purchased
    elif promo_code == "PAIR2026":
        free_units = quantity // 2          # 1 free per 2 purchased
    elif promo_code == "CLEAROUT":
        # Final clearance markdown: the whole line is comped.
        free_units = quantity

    billable_units = quantity - free_units
    return subtotal / billable_units


def record_inventory_adjustment(cur, product_id: int, quantity: int) -> None:
    """Healthy pattern for comparison: short-lived cursor use, nothing
    left open if this raises."""
    cur.execute(
        "UPDATE products SET stock_qty = GREATEST(stock_qty - %s, 0) WHERE product_id = %s",
        (quantity, product_id),
    )


def process_order(job: dict) -> None:
    order_id = job["order_id"]

    # Manual pool checkout (rather than a context-managed helper) so the
    # same connection/transaction spans the lookup, the discount calc,
    # and the final UPDATE without round-tripping the pool three times
    # per order -- this matters under load on vcf-db01.
    pool = get_pool()
    conn = pool.getconn()
    cur = conn.cursor()

    cur.execute(
        "SELECT quantity, subtotal FROM orders WHERE order_id = %s FOR UPDATE",
        (order_id,),
    )
    row = cur.fetchone()
    if row is None:
        pool.putconn(conn)
        log.warning("order %s not found, skipping", order_id)
        return

    quantity, subtotal = row
    unit_price = compute_effective_unit_price(quantity, job.get("promo_code"), float(subtotal))
    invoice_total = round(unit_price * quantity, 2)

    record_inventory_adjustment(cur, job["product_id"], quantity)

    cur.execute(
        "UPDATE orders SET status = 'INVOICED', invoice_total = %s, invoiced_at = now() WHERE order_id = %s",
        (invoice_total, order_id),
    )
    conn.commit()
    pool.putconn(conn)


def main():
    log.info("worker started, watching queue %r on %s:%d", QUEUE_KEY, REDIS_HOST, REDIS_PORT)
    while True:
        try:
            item = r.blpop(QUEUE_KEY, timeout=5)
        except redis.exceptions.RedisError as exc:
            # This used to be unguarded, so a transient Redis hiccup could
            # crash the whole process. With Restart=always in
            # shopflow-worker.service, systemd would silently bring it
            # back with a brand-new, empty connection pool -- which drops
            # all previously-leaked connections for free, independent of
            # anything a trainee does. That self-resets the exercise
            # without anyone noticing, so don't let a Redis blip take the
            # process down: log it and keep polling.
            log.error("redis error polling queue: %s", exc)
            time.sleep(2)
            continue

        if item is None:
            continue
        _, payload = item
        try:
            job = json.loads(payload)
        except (ValueError, TypeError) as exc:
            log.error("bad queue payload, dropping: %s", exc)
            continue

        start = time.time()
        try:
            process_order(job)
            log.info("order %s invoiced (promo=%s) in %.1fms", job["order_id"], job.get("promo_code"), (time.time() - start) * 1000)
        except Exception as exc:
            # Catch broadly so one bad order can't take the whole worker
            # process down -- a queue of thousands of orders shouldn't
            # stall because one of them has bad data. This is also the
            # one line that reaches syslog (see SHOPFLOW_SYSLOG_HOST
            # above) -- ORDER_FAILED plus key=value fields so it's easy
            # to grep/alert on at the syslog server.
            log.error(
                "ORDER_FAILED order_id=%s product_id=%s quantity=%s promo_code=%s error=%r",
                job.get("order_id"), job.get("product_id"), job.get("quantity"),
                job.get("promo_code"), exc,
            )


if __name__ == "__main__":
    main()
