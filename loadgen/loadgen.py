#!/usr/bin/env python3
"""
ShopFlow traffic generator -- simulates concurrent shoppers hitting the
storefront through vcf-frontend01. Run this from its own VM
(vcf-loadgen01) so its own resource usage doesn't muddy the metrics of
the tiers under test -- if you're limited to 3 VMs total, running this
from your own workstation/jump box instead of a fourth VM works fine
too, it just needs network access to vcf-frontend01.

What it does, continuously, with SHOPFLOW_VUSERS concurrent "shoppers":
  - browses the catalog (GET /api/catalog/products)
  - checks the featured/trending panel (GET /api/catalog/featured) --
    this is what drives the cache-stampede pattern given the middle
    tier's short FEATURED_TTL
  - occasionally checks out (POST /api/checkout) with a randomised
    product/quantity/promo combination

Checkout promo selection is weighted so that CLEAROUT orders -- the ones
that trip the worker-side bug -- occur at a roughly steady, configurable
cadence rather than a fixed timer, so the pattern looks like organic
traffic rather than an obvious synthetic heartbeat.

No metrics library here -- this just prints periodic counters to stdout
so you can see it's alive; the actual troubleshooting signal comes from
VCF Operations against the three tiers, not from the load generator.

Tunables (env vars), all with sane lab defaults:
  SHOPFLOW_BASE_URL          default http://vcf-frontend01
  SHOPFLOW_VUSERS            default 15
  SHOPFLOW_CHECKOUT_EVERY_S  default 5     (avg seconds between checkouts, per vuser)
  SHOPFLOW_CLEAROUT_WEIGHT   default 0.02  (fraction of checkouts using CLEAROUT)

Rough leak-rate math (see INSTRUCTOR_GUIDE.md for the full worked example):
  total_checkouts_per_min  = VUSERS / CHECKOUT_EVERY_S * 60
  clearout_leaks_per_min   = total_checkouts_per_min * CLEAROUT_WEIGHT
  minutes_to_exhaust_pool ~= (max_connections - baseline_in_use) / clearout_leaks_per_min
Do a dry run and watch pg_stat_activity to calibrate for your actual lab
hardware before class, then adjust CLEAROUT_WEIGHT (or db/README.md's
max_connections) to fit your session length.
"""
import json
import os
import random
import threading
import time
from collections import Counter

import requests

BASE_URL = os.environ.get("SHOPFLOW_BASE_URL", "http://vcf-frontend01")
VUSERS = int(os.environ.get("SHOPFLOW_VUSERS", "15"))
CHECKOUT_EVERY_S = float(os.environ.get("SHOPFLOW_CHECKOUT_EVERY_S", "5"))
CLEAROUT_WEIGHT = float(os.environ.get("SHOPFLOW_CLEAROUT_WEIGHT", "0.02"))

NORMAL_PRODUCT_IDS = list(range(1, 13))      # SKU-1001 .. SKU-1012
CLEARANCE_PRODUCT_IDS = list(range(13, 16))  # SKU-9001 .. SKU-9003

session = requests.Session()
_stats_lock = threading.Lock()
_stats = Counter()


def timed_get(path, timeout=12):
    try:
        resp = session.get(BASE_URL + path, timeout=timeout)
        outcome = "ok" if resp.ok else f"http_{resp.status_code}"
    except requests.RequestException:
        outcome = "error"
    with _stats_lock:
        _stats[f"{path.split('?')[0]}:{outcome}"] += 1


def do_checkout():
    if random.random() < CLEAROUT_WEIGHT:
        product_id = random.choice(CLEARANCE_PRODUCT_IDS)
        promo_code = "CLEAROUT"
        quantity = random.randint(1, 4)
    else:
        product_id = random.choice(NORMAL_PRODUCT_IDS)
        promo_code = random.choice([None, None, "FLASH2026", "PAIR2026"])
        quantity = random.randint(1, 6)

    payload = {
        "customer_id": random.randint(1, 100000),
        "product_id": product_id,
        "quantity": quantity,
        "promo_code": promo_code,
    }
    try:
        resp = session.post(BASE_URL + "/api/checkout", json=payload, timeout=12)
        outcome = "ok" if resp.ok else f"http_{resp.status_code}"
    except requests.RequestException:
        outcome = "error"
    with _stats_lock:
        _stats[f"checkout:{outcome}"] += 1
        _stats[f"checkout_promo:{promo_code or 'NONE'}"] += 1


def vuser_loop(vuser_id):
    next_checkout = time.time() + random.uniform(0, CHECKOUT_EVERY_S)
    while True:
        timed_get("/api/catalog/products")
        timed_get("/api/catalog/featured")

        if time.time() >= next_checkout:
            do_checkout()
            next_checkout = time.time() + random.expovariate(1.0 / CHECKOUT_EVERY_S)

        time.sleep(random.uniform(0.2, 1.0))


def stats_loop():
    while True:
        time.sleep(60)
        with _stats_lock:
            snapshot = dict(_stats)
        print(f"[loadgen] last-minute-ish cumulative counters: {json.dumps(snapshot)}")


def main():
    print(f"loadgen: {VUSERS} virtual users -> {BASE_URL}")
    print(f"loadgen: avg checkout interval {CHECKOUT_EVERY_S}s/vuser, CLEAROUT weight {CLEAROUT_WEIGHT}")

    threading.Thread(target=stats_loop, daemon=True).start()
    threads = []
    for i in range(VUSERS):
        t = threading.Thread(target=vuser_loop, args=(i,), daemon=True)
        t.start()
        threads.append(t)
        time.sleep(0.05)  # stagger startup

    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
