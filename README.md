# ShopFlow — a VCF Operations troubleshooting lab

A small, deliberately-flawed 3-tier e-commerce app ("ShopFlow") for a VCF
Operations troubleshooting session. It runs on 3 Linux VMs (4 if you add
a dedicated load generator), generates its own realistic traffic, and
contains a seeded, non-obvious production bug plus one independent
contributing issue. No separate monitoring stack is involved — VCF
Operations reads everything from native OS metrics plus the standard
PostgreSQL/Redis/nginx Telegraf inputs. The goal is a troubleshooting
exercise that requires correlating metrics across tiers and using VCF
Operations 9.1's real-time metrics to see a pattern the default 5-minute
collection interval would hide.

**If you're the instructor, read `INSTRUCTOR_GUIDE.md` before deploying.**
It has the full root-cause explanation, the expected symptom timeline, a
suggested troubleshooting path, and the timing math so you can tune the
exercise to your session length. This README is the operator/deployment
doc; it deliberately does not spell out the bug.

## What it is

ShopFlow is a toy storefront: browse a catalog, check a "trending now"
panel, check out. Checkout is asynchronous — the API writes the order
and hands it to a background worker that computes the final invoice and
adjusts inventory. Three tiers, three VMs:

| VM             | Role                                              | Tech                          |
|----------------|----------------------------------------------------|---------------------------------|
| vcf-frontend01 | Presentation / reverse proxy                       | nginx                           |
| vcf-middle01   | Business logic: API, async worker, cache/queue     | Flask + gunicorn, Python, Redis |
| vcf-db01       | Database                                           | PostgreSQL                      |

Optional fourth VM, `vcf-loadgen01`, generates traffic — keeping it
separate from the three tiers means its own CPU/network usage doesn't
show up in the metrics you're trying to teach from. If you only have
three VMs to spare, run `loadgen/loadgen.py` from your own workstation
or a jump box instead; it just needs HTTP access to vcf-frontend01.

See `ARCHITECTURE.md` for the request flow and a diagram.

## Deploying

Provision three (or four) small Linux VMs (2 vCPU / 4 GB RAM is plenty;
Ubuntu 22.04/24.04 assumed by the `deploy/*.sh` scripts — adjust package
manager commands for other distros) on your VCF-managed vSphere cluster
so they're visible to VCF Operations. Give them the hostnames above (or
update the configs/env files to match your own naming) and make sure
they can resolve each other, then copy this whole directory to each VM
(scp/git clone/shared datastore ISO, whatever's convenient) and run:

```bash
chmod +x deploy/*.sh   # permissions aren't always preserved by transfer/zip
```

then, per VM:

```bash
# vcf-db01
sudo ./deploy/install_db.sh 10.0.0.0/24      # pass your app-tier subnet

# vcf-middle01
sudo ./deploy/install_common.sh
sudo ./deploy/install_middle.sh
# then edit /opt/shopflow/middle/middle.env if hostnames differ from
# defaults, and: sudo systemctl restart shopflow-api shopflow-worker

# vcf-frontend01
sudo ./deploy/install_frontend.sh

# vcf-loadgen01 (optional)
sudo ./deploy/install_common.sh
sudo ./deploy/install_loadgen.sh
```

Order matters loosely: DB first, then middle (it'll retry connections if
the DB isn't up yet, but starting clean is simpler), then frontend, then
loadgen last since it's what generates load.

## Connecting it to VCF Operations

See `monitoring/METRICS.md` for the full walkthrough: enabling real-time
metrics for these VMs, adding each one as an OS/application monitoring
agent, which service type to pick per tier (NGINX, Redis, PostgreSQL —
all native Telegraf inputs, no app instrumentation involved), and what
to actually look at. Do this *before* class and let it run for at least
one full incident cycle (see `INSTRUCTOR_GUIDE.md` for timing) so
there's history to look at, not just a live feed.

## Running / resetting the scenario

- Start: bring services up in the order above; the load generator alone
  drives the whole thing from then on.
- Reset for a re-run: `sudo systemctl restart shopflow-worker` on
  vcf-middle01 clears any currently-leaked connections (they belong to
  that process), and `sudo -u postgres psql -d shopflow -c "SELECT
  pg_terminate_backend(pid) FROM pg_stat_activity WHERE state = 'idle in
  transaction' AND datname='shopflow';"` on vcf-db01 clears them from the
  DB side immediately if you don't want to wait for the timeout.
- Pause the exercise: `sudo systemctl stop shopflow-loadgen` (wherever
  it's running) stops new traffic (existing leaked connections remain
  until the timeout or a worker restart).

## Tuning

Everything that controls pacing lives in env files
(`/opt/shopflow/*/*.env`) or `db/README.md`'s Postgres settings:
`SHOPFLOW_CLEAROUT_WEIGHT` / `SHOPFLOW_CHECKOUT_EVERY_S` /
`SHOPFLOW_VUSERS` (loadgen), `max_connections` /
`idle_in_transaction_session_timeout` (Postgres), and
`SHOPFLOW_FEATURED_TTL` (middle tier, cache-stampede frequency).
`INSTRUCTOR_GUIDE.md` has the math and recommended dry-run process.

## Safety notes

This is lab-only code: Redis runs with no auth, default passwords are
used throughout, and the seeded bug is a real, exploitable-looking
resource-exhaustion condition. Do not expose these VMs to anything but an
isolated training network.
