# Instructor guide — ShopFlow VCF Operations troubleshooting lab

Answer key. Don't hand this to trainees (or at least not before the
exercise) — everything else in the repo is safe for them to see,
including the source code, if you want a "code review" phase after the
metrics-based investigation.

## 1. The headline symptom (what trainees will actually observe)

Every ~12-15 minutes (with default tunables — see §5), the storefront
degrades and then recovers on its own:

- Checkout and catalog requests get progressively slower.
- nginx on `vcf-frontend01` starts returning 504s.
- The Redis-backed order queue on `vcf-middle01` backs up.
- Then, over roughly a minute, it clears and everything returns to
  normal — until it happens again.

This "it fixes itself" behavior is a deliberate design choice: it lets
you run the exercise more than once in a session without a manual reset,
and it's realistic (a lot of real leaks get masked by exactly this kind
of timeout-based band-aid, which is itself a clue worth surfacing).

## 2. Root cause

`middle/worker.py`'s `compute_effective_unit_price()` applies promo
discounts by computing how many units in an order are "free" and
dividing the subtotal across the remaining "billable" units. Two of the
three promo codes (`FLASH2026`, `PAIR2026`) always leave at least one
billable unit. The third, `CLEAROUT` (a clearance/final-markdown code,
meant to comp the *entire* line), sets `free_units = quantity` — so
`billable_units` is always `0`, and the division always raises
`ZeroDivisionError`.

`process_order()` checks a connection out of the worker's DB pool
manually (rather than through the context-managed helper the API
process uses) so a single connection/transaction can span the order
lookup, the discount calculation, and the final write — avoiding extra
pool round-trips. It never wraps that in `try/finally`. The `SELECT ...
FOR UPDATE` before the discount calculation has already opened a
transaction; when the `ZeroDivisionError` fires, the function exits
without `COMMIT`, `ROLLBACK`, or returning the connection to the pool.
The outer loop in `main()` catches the exception broadly (so one bad
order can't crash the whole worker process — itself a defensible,
common pattern) and just logs it.

Net effect: every `CLEAROUT` order permanently consumes one Postgres
backend, sitting in `idle in transaction`, until either the worker
process restarts or `idle_in_transaction_session_timeout` reaps it.
`CLEAROUT` orders occur at a low, roughly steady rate as part of normal
simulated traffic (not a fixed timer — see `loadgen.py`), so the pool
drains slowly and predictably.

**Why it's non-obvious:** nothing about the failure looks like a
resource leak from the outside. The API process's own pool stays
healthy the whole time — the bug is entirely in the worker process, a
separate process on the *same* VM as the API, one hop removed from
where trainees will naturally look first (the tier serving user-facing
errors). The exception is caught and logged, not crashing anything, so
there's no obvious stack trace pointing at "unreleased connection" —
just a rising count of a specific promo code's orders never reaching
`INVOICED` status.

Because the API and worker are separate OS processes on vcf-middle01,
each with its own DB connection pool and its own `application_name` tag
(`shopflow-api` / `shopflow-worker` — see `common/dbutil.py`), trainees
can tell them apart in `pg_stat_activity` even though VCF Operations
only sees one VM. This is deliberate: it's a realistic stand-in for "two
services co-located on one host," which is common enough in smaller
environments that trainees should get comfortable separating them by
something other than which VM they're on.

## 3. The contributing issue: cache stampede

`middle/app.py`'s `FEATURED_TTL = 8` (seconds) backs the "trending now"
panel, hit on nearly every page load. Every 8 seconds under constant
load, the cache entry expires and every concurrently-inflight request
misses at once, recomputing the `trending_products` aggregate directly
against `vcf-db01` (no single-flight/locking). This shows up as a small,
very regular ~8-second-period blip in DB query rate and Redis miss rate
— much higher frequency and much smaller amplitude than the
connection-leak cycle. Trainees who look only at "DB is busy" without
separating frequency bands may initially conflate this with the main
incident. It's real, worth flagging, and independently fixable (raise
the TTL, add a stampede lock) — but it doesn't cause the outage by
itself.

## 4. Suggested troubleshooting path using VCF Operations

A path that uses each of the tools the session is meant to showcase,
roughly in order of "what a competent trainee should try":

1. **Alert / dashboard triage.** Start from whatever alert or dashboard
   first flags the incident (frontend 5xx rate, or a generic VM health
   badge). Confirm user impact and scope (which VMs show anomalies).
2. **Rule out the obvious.** Check `vcf-middle01`'s CPU and memory —
   they stay unremarkable through the whole incident. This is the first
   "wait, it's not actually overloaded" moment: the symptom is
   real, but it isn't a resource-exhaustion problem at the OS level on
   the tier serving requests.
3. **Follow the request downstream.** Check `vcf-frontend01`'s nginx
   metrics — 5xx/504 timing should lag the real cause by roughly the
   proxy timeout window, which is a useful clue about where in the chain
   the actual blocking is happening (something the middle tier is
   waiting on, not something it's doing).
4. **Check the DB tier's own view.** `postgresql_numbackends` climbing
   toward `max_connections`, and — with the custom query from
   `telegraf-db.conf` wired up — `shopflow_db_idle_in_txn` (tagged by
   `application_name`) rising specifically for `shopflow-worker`, not
   `shopflow-api`. This is the "aha" metric: connections aren't just
   numerous, they're specifically *idle in transaction*, and they belong
   to one particular process — which rules out both "the DB is just
   legitimately busy" and "it's the whole middle tier."
5. **Switch to real-time metrics.** At the default 5-minute collection
   interval the climb-and-recover cycle (12-15 minutes) is visible but
   easy to dismiss as noise around a stable average. Re-examine the same
   window with VCF Operations 9.1 real-time metrics (2-60s collection)
   and the sawtooth becomes unambiguous. This is the step that
   specifically exercises the 9.1 feature the session is built around —
   consider narrating explicitly "notice how different this looks at
   5-minute vs. real-time resolution."
6. **Corroborate with flows (if available).** Long-duration established
   flows `vcf-middle01 -> vcf-db01:5432` that persist for the whole
   incident, growing in count over time, confirm the same story from a
   different data source. Separately, the ~8s burst pattern between the
   same two VMs corroborates the (secondary) cache-stampede finding —
   distinguishable from the leak by its much shorter flow duration.
7. **Pin down the trigger.** With the worker process implicated, either
   check the Redis queue-depth metric (climbs once the pool is
   saturated and the worker can't keep up) or, if you're doing a
   code-review phase, have trainees read `middle/worker.py`'s
   `compute_effective_unit_price()` and `process_order()` to find the
   missing `try/finally` and the `CLEAROUT` edge case.
8. **Fix and verify.** The real fix is code (wrap the manual pool
   checkout in `try/finally`, or better, use the same context-managed
   pattern as the API process; separately, guard against
   `billable_units == 0`). As a live demo you can instead show the
   *mitigation* already in place — `idle_in_transaction_session_timeout`
   — and discuss why a timeout is a safety net, not a fix.

## 5. Timing / tuning math

Connection-leak cycle length depends on three things: how often
`CLEAROUT` checkouts occur, how much headroom exists in
`max_connections` above baseline usage, and
`idle_in_transaction_session_timeout`.

```
total_checkouts_per_min ≈ SHOPFLOW_VUSERS / SHOPFLOW_CHECKOUT_EVERY_S * 60
clearout_leaks_per_min  ≈ total_checkouts_per_min * SHOPFLOW_CLEAROUT_WEIGHT
minutes_to_exhaustion   ≈ (max_connections - baseline_connections_in_use) / clearout_leaks_per_min
```

Defaults (`SHOPFLOW_VUSERS=15`, `SHOPFLOW_CHECKOUT_EVERY_S=5`,
`SHOPFLOW_CLEAROUT_WEIGHT=0.02`) give ≈3.6 leaks/min. With
`max_connections=40` and typical baseline usage of roughly 5-10
connections (one API pool + one worker pool, both on a single VM now —
less baseline overhead than a multi-VM layout) under this load, expect
first exhaustion somewhere around 8-14 minutes after startup — actual
numbers depend on your lab's hardware/network latency, so **do a dry
run** and watch:

```sql
SELECT application_name, count(*)
FROM pg_stat_activity
WHERE state = 'idle in transaction'
GROUP BY application_name;
```

on `vcf-db01` to calibrate before class. To lengthen the cycle for a
longer session, lower `SHOPFLOW_CLEAROUT_WEIGHT` or raise
`max_connections`; to shorten it, do the opposite.
`idle_in_transaction_session_timeout` determines how long each cycle's
"recovery" takes to kick in — set it to somewhat less than your desired
total cycle length so trainees can watch at least one full
climb-and-recover during a live segment, or set it long and do the DB
tier's `pg_terminate_backend` reset from `README.md` on your own cue
instead of waiting.

The cache-stampede rhythm (`SHOPFLOW_FEATURED_TTL`, default 8s) is
independent of the above and doesn't need retuning unless you want a
different contrast in frequency between the two seeded issues.

## 6. Facilitation notes

- Run the whole stack for at least one full cycle before class starts so
  there's real history in VCF Operations to look at, not just a live
  feed trainees have to wait on.
- If a trainee jumps straight to "it's the worker process's connection
  pool," that's fine — the point of the multi-step path above is to
  build the skill of correlating signals, not to gatekeep the answer.
  Consider asking them to also find the cache-stampede issue and
  articulate why it's a separate problem, so the exercise still covers
  the intended breadth.
- Good discussion prompts: "why does this recover on its own?", "how
  would you tell a genuine capacity problem from a leak using only
  `pg_stat_activity`?", "the API and worker run on the same VM — how did
  you tell which one was responsible without a per-process metric?",
  "what would you alert on to catch this before users notice?", "what's
  the actual code fix, and why didn't the broad `except Exception` catch
  it?"
- Want more complexity later? A natural extension is splitting the
  middle tier back into a separate app VM and worker VM (or adding a
  second, unrelated VM on the same host/cluster as the DB that
  periodically burns CPU) to reintroduce a genuine "which tier, which
  VM" ambiguity — straightforward to bolt back on once this simpler
  version is comfortable for your audience.
