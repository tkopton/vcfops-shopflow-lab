# Monitoring setup notes — VCF Operations

This is the bridge between "what the ShopFlow lab exposes" and "what to
actually configure in VCF Operations" before running the session. There
is deliberately no application-level metrics library anywhere in this
lab (no Prometheus, no custom `/metrics` endpoint) — the whole point is
that VCF Operations is the observability tool, working from the native
OS metrics VMware Tools already reports plus the standard Telegraf input
plugins for PostgreSQL, Redis, and nginx. Nothing here asks you to run a
second monitoring stack alongside VCF Operations.

Screens and exact menu paths shift between VCF Operations releases, so
this describes the *data* to bring in and *why*, and points at current
Broadcom docs for the exact click-path in your build:

- Real-time metrics overview: VCF Operations 9.1 release notes ("What's
  New" > VCF Operations) and the "Deploy Real-Time Metrics" deployment
  guide, both on techdocs.broadcom.com.
- OS & application monitoring via Telegraf: "Monitoring Application
  Services using Product-Managed Telegraf" / "OS and Application
  Monitoring" pages for VCF 9.x on techdocs.broadcom.com.

## 1. Get the VMs into VCF Operations at all

Add the vCenter (or standalone ESXi) hosting the lab as a data source if
it isn't already, so all VMs show up as vSphere VM objects with the
usual CPU/memory/disk/network metrics at the default 5-minute collection
interval. This alone shows resource-level symptoms but not the actual
root cause — that needs the application-service metrics below, which is
the point of the exercise.

## 2. Turn on Real-Time Metrics for the lab VMs

VCF Operations 9.1 adds a real-time data path that collects a subset of
metrics as often as every 2 seconds (the broader metric set can be
configured down to about 60s per adapter instance; the dedicated
real-time subset goes lower). Enable/deploy it per the "Deploy Real-Time
Metrics" guide, then point it at all three (or four, with loadgen) lab
VMs, especially vcf-middle01 and vcf-db01. Without this, the leak's
~12-15 minute climb gets rolled into 5-minute averages and looks like
ordinary noise — the whole point of building the scenario around it.

## 3. Add each VM as an OS/Application monitoring agent

Install the Telegraf-based agent on each Linux VM (Environment > add
VM/Endpoint agent, or the equivalent flow in your build), then attach
the service definitions below. The `telegraf-*.conf` files in this
folder show the equivalent raw Telegraf blocks if your build lets you
drop them in directly instead of using the wizard.

| VM             | Service to add          | Target                              |
|----------------|--------------------------|--------------------------------------|
| vcf-frontend01 | NGINX                    | `localhost:8081/nginx_status`        |
| vcf-middle01   | Redis                    | `localhost:6379`                     |
| vcf-db01       | PostgreSQL               | `localhost:5432`, role `telegraf`    |

Set collection interval to 5s on all of these so they line up with the
real-time metrics window. No service needs to be added for the API/worker
processes themselves — there's nothing custom to scrape there; OS-level
CPU/memory for vcf-middle01 is all VCF Operations needs at that layer.

## 4. What to actually look at, by data source

Everything below comes from services that already report it natively —
nothing was added to the application to make this visible.

**PostgreSQL (vcf-db01)** — the core signal:
- `postgresql_numbackends` climbing toward `max_connections` over
  10-15 minutes, then dropping sharply.
- The custom query in `telegraf-db.conf`
  (`shopflow_db_idle_in_txn.idle_in_txn_count`) — the direct fingerprint
  of the leak: sessions sitting `idle in transaction`, not just "busy."
  This is what separates "the DB is legitimately under load" from "the
  DB is quietly running out of connections."
- `pg_stat_activity` itself (see `db/README.md`) if trainees get
  interactive DB access during the exercise.

**Redis (vcf-middle01)**:
- `redis_keyspace_hits` / `redis_keyspace_misses` (rate) — a sharp,
  regular miss spike roughly every 8 seconds is the cache-stampede
  signature, a different (faster, smaller) rhythm than the DB story.
- The optional `shopflow_queue_depth` exec metric — climbs when the
  worker is blocked waiting on an exhausted DB pool, since orders back
  up in the queue faster than they're consumed.

**nginx (vcf-frontend01)**:
- Connection/request counters from `stub_status`, plus 5xx/504 responses
  in the access log — the tier where users actually feel the incident,
  lagging the DB pool exhaustion by roughly the sum of
  `proxy_connect_timeout` and however long the middle tier blocks on
  `pool.getconn()`.

**OS metrics (all VMs)**:
- vcf-middle01's own CPU/memory usually stays unremarkable through the
  whole incident — a useful negative result that rules out "the app
  server is just overloaded" as an explanation.

## 5. Suggested super metrics / alert definitions

Build these once the metrics above are flowing:

- **Super metric** `shopflow.db.idle_in_txn_ratio` =
  `shopflow_db_idle_in_txn.idle_in_txn_count / postgresql_numbackends`.
  Alert (Warning) above 20%, (Critical) above 50%.
- **Super metric** `shopflow.frontend.error_rate_pct` = 5xx / total
  requests on vcf-frontend01's nginx metrics. Alert (Critical) at >5%
  over 2 minutes.
- **Symptom definition**: `idle_in_txn_count > 5` sustained for 3
  minutes on vcf-db01 — this is the one that should point straight at
  the DB tier rather than the frontend/middle tier trainees will
  initially suspect.
- Optional stretch goal for advanced classes: an alert/root-cause
  correlation definition chaining frontend 5xx → Redis queue-depth
  growth → DB idle-in-transaction count, so the platform itself proposes
  the causal chain instead of trainees assembling it purely by eye.

## 6. Flows

If your VCF Operations deployment has network flow visibility (NSX
integration or the built-in application/flow discovery) enabled for
these VMs, have trainees look for two distinct patterns:

- A **slowly growing number of long-duration established flows** between
  vcf-middle01 and vcf-db01:5432 — each leaked connection is a TCP flow
  that never closes. Flow *duration*, not just count, is the tell:
  healthy flows are sub-second; leaked ones live for the rest of the
  incident.
- A **periodic burst of short flows** between vcf-middle01 and
  vcf-db01:5432 every ~8 seconds beyond the baseline — the cache
  stampede fingerprint, visible in flow data as well as in Redis's own
  hit/miss counters.

Both are independent confirmations of what the metrics already show —
useful for the part of the session on corroborating a hypothesis with
more than one data source.
