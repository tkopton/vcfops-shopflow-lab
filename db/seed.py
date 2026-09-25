#!/usr/bin/env python3
"""
Seed data for the ShopFlow lab database.

Run once on vcf-db01 (or from any host that can reach it) after schema.sql
has been applied:

    python3 seed.py --dsn "host=vcf-db01 dbname=shopflow user=shopflow password=shopflow"

Creates a realistic catalog plus the promo codes used by the exercise:
  - FLASH2026   : buy-3-get-1-free style discount (1 free unit per 3 bought)
  - PAIR2026    : buy-2-get-1-free style discount (1 free unit per 2 bought)
  - CLEAROUT    : clearance code applied only to is_clearance items.
                  Intended to zero out the line (100% off) as a
                  "final markdown" promotion. This is the code that
                  the worker's discount calculation does not handle
                  safely -- see worker/worker.py.
"""
import argparse
import random

import psycopg2

PRODUCTS = [
    ("SKU-1001", "Wireless Mouse", "Electronics", 19.99, True, False),
    ("SKU-1002", "Mechanical Keyboard", "Electronics", 79.99, True, False),
    ("SKU-1003", "USB-C Hub", "Electronics", 34.50, False, False),
    ("SKU-1004", "27in Monitor", "Electronics", 249.00, True, False),
    ("SKU-1005", "Laptop Stand", "Accessories", 29.99, False, False),
    ("SKU-1006", "Webcam 1080p", "Electronics", 45.00, False, False),
    ("SKU-1007", "Noise Cancelling Headphones", "Audio", 129.00, True, False),
    ("SKU-1008", "Bluetooth Speaker", "Audio", 59.00, False, False),
    ("SKU-1009", "Desk Mat", "Accessories", 15.00, False, False),
    ("SKU-1010", "Ergonomic Chair", "Furniture", 189.00, True, False),
    ("SKU-1011", "Standing Desk", "Furniture", 349.00, False, False),
    ("SKU-1012", "Ring Light", "Accessories", 22.00, False, False),
    # Clearance items -- these are the ones eligible for CLEAROUT
    ("SKU-9001", "Last-Gen Tablet (Clearance)", "Electronics", 99.00, False, True),
    ("SKU-9002", "Refurb Earbuds (Clearance)", "Audio", 24.00, False, True),
    ("SKU-9003", "Discontinued Router (Clearance)", "Electronics", 39.00, False, True),
]

PROMOS = [
    ("FLASH2026", "Flash sale: 1 free unit per 3 purchased", True),
    ("PAIR2026", "Buy 2 get 1 free (per pair)", True),
    ("CLEAROUT", "Final clearance markdown on clearance SKUs", True),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True, help="psycopg2 DSN for the shopflow database")
    args = ap.parse_args()

    conn = psycopg2.connect(args.dsn)
    conn.autocommit = True
    cur = conn.cursor()

    for sku, name, category, price, featured, clearance in PRODUCTS:
        cur.execute(
            """
            INSERT INTO products (sku, name, category, unit_price, is_featured, is_clearance, stock_qty)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (sku) DO NOTHING
            """,
            (sku, name, category, price, featured, clearance, random.randint(200, 2000)),
        )

    for code, desc, active in PROMOS:
        cur.execute(
            """
            INSERT INTO promo_codes (promo_code, description, active)
            VALUES (%s, %s, %s)
            ON CONFLICT (promo_code) DO NOTHING
            """,
            (code, desc, active),
        )

    print("Seed complete.")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
