# Instructor guide — ShopFlow VCF Operations troubleshooting lab

Answer key. Don't hand this to trainees before the exercise — everything
else in the repo (including the source code) is safe for them to see.

## TL;DR

There's a bug in the middle tier (`vcf-middle01`'s order-processing
worker) that leaks a database connection every time it processes a
specific kind of order. Over roughly 12-15 minutes of normal traffic,
enough connections leak that Postgres runs out of connections entirely,
which starves the API process too — checkout (and sometimes catalog
browsing) starts failing or timing out. It partially recovers on its own
after about 15 minutes (a safety-net Postgres setting kills the stuck
connections), but the worker process itself needs a manual restart to
actually resume processing orders again.

For a troubleshooting exercise, "there's a software bug leaking database
connections somewhere in the middle tier" is a perfectly good stopping
point. The full mechanism (§5) is there if you want to go deeper with an
advanced group, but isn't required to run the exercise.

## 1. How you'll notice something's wrong

- **Synthetic/external monitoring** (see §2) on the checkout endpoint
  starts returning 5xx or timing out.
- A VCF Operations alert on the frontend or middle-tier VM fires (5xx
  rate, or a connection-count threshold on the DB if you've set one up).
- Manually: `curl http://<frontend>/api/checkout -X POST ...` (see §2
  for the body) starts hanging or erroring.

## 2. Synthetic / external monitoring — what to actually probe

If you're polling the frontend externally (e.g. a VCF Operations
synthetic/URL check, or your own cron+curl), there's an important
asymmetry between the endpoints:

- **`GET /api/catalog/products` and `GET /api/catalog/featured` are
  cached in Redis.** During the incident, these can keep returning a
  perfectly healthy `200` if the cache hasn't expired yet — they're
  reading stale-but-fine cached data, not hitting the struggling
  database. Don't rely on these alone to catch the problem; they're not
  dishonest, they're just answering from cache, which is realistic
  (and a good discussion point about why monitoring only the cached path
  can hide a real backend problem).
- **`POST /api/checkout` always touches the database** — it looks up the
  current price and inserts a new order row, with nothing cached in
  front of it. This is the one that will reliably show the problem.

POST body for a synthetic checkout check:

```json
{"customer_id": 1, "product_id": 1, "quantity": 1, "promo_code": null}
```

Use a `product_id` between 1 and 12 (the non-clearance catalog items)
and leave `promo_code` as `null` — this keeps your synthetic probe a
passive health check that never itself triggers the bug (the loadgen's
own organic traffic is what does that). A healthy response is `202` with
a JSON body like `{"order_id": ..., "status": "PENDING"}`; during the
incident expect `500`, `502`/`504` from nginx, or a timeout, depending on
exactly where in the chain things are stuck.

**On polling interval:** the incident window is on the order of a
minute or two once it starts (see §6 for timing), so a 5-minute synthetic
check interval might occasionally straddle right past it. If you want to
reliably catch it for a demo, either shorten the check interval, or
lengthen `idle_in_transaction_session_timeout` in `db/README.md` so the
degraded window lasts longer.

## 3. What to look at in VCF Operations

You don't need the custom per-process Postgres query from
`monitoring/telegraf-db.conf` to run this exercise — if you already have
a built-in/management-pack PostgreSQL integration collecting standard
metrics, that's enough:

- **`numbackends`** (or your management pack's equivalent "active
  connections" metric) on vcf-db01 climbing toward `max_connections`
  (40) over several minutes, then dropping — that's the signal.
- **CPU/memory on vcf-middle01 and vcf-frontend01 stay unremarkable**
  through the whole incident — worth checking and ruling out, since it's
  what trainees will naturally suspect first ("the app server must be
  overloaded"). It isn't; nothing here is CPU-bound.
- **Real-time metrics** (VCF Operations 9.1, 2-60s collection) make the
  climb-then-drop pattern obvious; at the default 5-minute rollup it can
  look like unremarkable noise around a stable average.

The custom `application_name`-tagged idle-in-transaction query in
`telegraf-db.conf` is a nice-to-have that tells you *which process*
(API vs. worker) is holding the connections, via `pg_stat_activity`. If
it's not picking up in your VCF Operations build, don't spend time
debugging it for this exercise — `numbackends` alone is enough to
support the "it's a connection problem on the DB, caused by something in
the middle tier" conclusion. (If you do want it working: the likely
culprit is that product-managed Telegraf agents in VCF Operations may
only support metrics added through the UI's service wizard rather than
arbitrary custom `[[inputs.postgresql_extensible]]` blocks dropped into
a conf file — check your build's docs for whether/how it accepts a raw
Telegraf config snippet versus requiring the service to be added through
the UI.)

## 4. The troubleshooting path (step by step)

1. Notice the alert or synthetic-check failure on checkout.
2. Check vcf-middle01 and vcf-frontend01 CPU/memory — both normal. Rules
   out "just overloaded."
3. Check vcf-frontend01's nginx metrics — elevated 5xx/504, lagging the
   real cause by roughly nginx's proxy timeout window.
4. Check vcf-db01's connection count (`numbackends`) — climbing toward
   `max_connections`.
5. Conclusion: the database is running out of connections, not because
   of query load (CPU/memory on the DB are fine too), but because
   something is opening connections and not closing them. That something
   is upstream, in the middle tier — a software bug, not a capacity
   problem. That's the intended takeaway.
6. Recovery: checkout should start working again on its own once
   `idle_in_transaction_session_timeout` frees the stuck connections
   (watch `numbackends` drop). Separately, restart the worker so it
   actually resumes processing orders:
   ```bash
   sudo systemctl restart shopflow-worker
   ```
   Orders placed during the incident will stay at `status='PENDING'`
   until this runs — a good way to show trainees that "the frontend
   looks fine again" isn't the same as "the problem is actually fixed."

## 5. Full root cause (optional — for advanced trainees or your own background)

`middle/worker.py`'s `compute_effective_unit_price()` applies promo
discounts by computing how many units in an order are "free" and
dividing the subtotal across the remaining "billable" units. The
`CLEAROUT` promo code (a clearance/final-markdown code meant to comp the
*entire* line) sets `free_units = quantity`, so `billable_units` is
always `0` and the division always raises `ZeroDivisionError`.

`process_order()` checks a connection out of the worker's DB pool
manually (rather than through the context-managed helper the API process
uses), to let a single connection/transaction span the order lookup, the
discount calculation, and the final write. It never wraps this in
`try/finally`. When the `ZeroDivisionError` fires — after the row lock is
taken but before `COMMIT`/`ROLLBACK` — the function exits without
releasing the connection. The outer loop catches the exception broadly
(so one bad order can't crash the whole worker) and just logs it.

Net effect: every `CLEAROUT` order permanently leaks one Postgres
connection (`idle in transaction`) for the life of the worker process.
`CLEAROUT` orders occur at a low, steady rate as part of normal simulated
traffic (not a fixed timer — see `loadgen/loadgen.py`), so the problem
builds slowly and predictably rather than failing immediately — which is
what makes it non-obvious: nothing about the failure looks like a
resource leak from the outside, and the API process's own connections
stay completely healthy throughout (different process, different pool).

There's also a smaller, independent contributing issue: `middle/app.py`'s
`FEATURED_TTL = 8` (seconds) causes a small cache-stampede every 8
seconds under load — a much faster, smaller-amplitude pattern than the
connection leak, worth noticing as a separate thing if trainees go
looking at DB query rate.

## 6. Timing / tuning math

```
total_checkouts_per_min ≈ SHOPFLOW_VUSERS / SHOPFLOW_CHECKOUT_EVERY_S * 60
clearout_leaks_per_min  ≈ total_checkouts_per_min * SHOPFLOW_CLEAROUT_WEIGHT
minutes_to_exhaustion   ≈ (max_connections - baseline_connections_in_use) / clearout_leaks_per_min
```

Defaults (`SHOPFLOW_VUSERS=15`, `SHOPFLOW_CHECKOUT_EVERY_S=5`,
`SHOPFLOW_CLEAROUT_WEIGHT=0.02`) give ≈3.6 leaks/min. With
`max_connections=40` and baseline usage of roughly 2-8 connections (the
API's pool, capped at 8 — see `shopflow-api.service`), expect first
exhaustion somewhere around 9-13 minutes after startup. Actual numbers
depend on your lab's hardware/network latency, so **do a dry run** and
watch:

```sql
SELECT count(*) FROM pg_stat_activity WHERE state = 'idle in transaction';
```

on `vcf-db01` to calibrate before class. To lengthen the cycle, lower
`SHOPFLOW_CLEAROUT_WEIGHT`; to shorten it, raise it or lower
`max_connections`. `idle_in_transaction_session_timeout` controls how
long the degraded window lasts before Postgres self-heals — set it
comfortably longer than your synthetic check's polling interval (§2) so
a check is likely to land inside the window.

## 7. Facilitation notes

- Run the stack for at least one full cycle before class so there's real
  history in VCF Operations to look at, not just a live feed trainees
  have to wait on.
- Good discussion prompts: "why did checkout recover but orders are
  still stuck?", "why didn't the catalog page show anything wrong?",
  "how would you alert on this before users notice?", "what's the actual
  code fix?"
- Want more complexity later? Splitting the middle tier back into
  separate app/worker VMs, or adding an unrelated VM on the DB's host
  that periodically burns CPU as a red herring, are both straightforward
  to bolt back on once this simpler version is comfortable for your
  audience.
