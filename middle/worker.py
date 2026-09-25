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
import os
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
        item = r.blpop(QUEUE_KEY, timeout=5)
        if item is None:
            continue
        _, payload = item
        job = json.loads(payload)

        start = time.time()
        try:
            process_order(job)
            log.info("order %s invoiced (promo=%s) in %.1fms", job["order_id"], job.get("promo_code"), (time.time() - start) * 1000)
        except Exception as exc:
            # Catch broadly so one bad order can't take the whole worker
            # process down -- a queue of thousands of orders shouldn't
            # stall because one of them has bad data.
            log.error("order %s failed to invoice (promo=%s): %s", job.get("order_id"), job.get("promo_code"), exc)


if __name__ == "__main__":
    main()
