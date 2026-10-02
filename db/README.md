# DB tier — vcf-db01

1. Install PostgreSQL 14+ and create the database/role:

   ```bash
   sudo -u postgres createuser shopflow --pwprompt   # password: shopflow%shopflow% (or your own; update DSNs)
   sudo -u postgres createdb shopflow -O shopflow
   psql -U shopflow -h localhost -d shopflow -f schema.sql
   python3 -m pip install psycopg2-binary
   python3 seed.py --dsn "host=localhost dbname=shopflow user=shopflow password=shopflow%shopflow%"
   ```

2. Apply the lab-sized connection settings (edit `postgresql.conf`, usually
   under `/etc/postgresql/<ver>/main/` or `/var/lib/pgsql/<ver>/data/`):

   ```
   max_connections = 40
   idle_in_transaction_session_timeout = 900000   # 15 min, in ms
   ```

   Then `sudo systemctl restart postgresql`.

   **Why these values matter to the exercise:** the seeded bug in
   `middle/worker.py` leaves a connection open mid-transaction
   (`idle in transaction`) whenever it processes a `CLEAROUT` order. The
   worker's own connection pool is deliberately configured (in
   `shopflow-worker.service`) with a ceiling *above* `max_connections`, so
   Postgres itself — not the worker's local pool — is always what
   eventually refuses new connections. The API, meanwhile, doesn't pool
   connections at all (see `common/dbutil.py`'s `new_connection()`) — it
   opens one per request and closes it immediately after, specifically so
   it keeps needing *new* connections from Postgres rather than quietly
   reusing a small set of already-open ones that would stay healthy
   regardless of what the worker is doing. (An earlier version of this
   lab pooled the API too; once warmed, it never needed a new connection
   again, so checkout never actually failed during the incident — the
   frontend symptom the whole exercise is built around just didn't show
   up.) Baseline Postgres connection usage is therefore near-zero most of
   the time rather than a flat handful. With `max_connections=40` and the
   default loadgen settings, exhaustion takes roughly 9-13 minutes of
   normal traffic.

   `idle_in_transaction_session_timeout` eventually reaps each orphaned
   session, but at the default leak rate this is **not** a working
   self-heal: new leaks refill freed capacity about as fast as the
   timeout clears it (leak demand over one 15-minute window is
   `~3.6/min * 15 ≈ 54`, more than the ~32-38 connections actually
   available), so once saturated, `numbackends` just stays pegged near
   `max_connections` rather than recovering. The real fix is
   `systemctl restart shopflow-worker`: killing the old process drops its
   leaked sockets immediately, which Postgres notices right away and
   cleans up — a fast, near-complete recovery, much quicker than waiting
   out the 15-minute timeout. It's a reset, not a cure, though: the
   restarted worker runs the same buggy code and will start leaking again
   if CLEAROUT orders keep coming. Either way, it does **not** fix the
   worker's own in-memory pool bookkeeping for the connections it already
   lost, and any orders placed during the incident stay stuck at
   `status='PENDING'` until the worker is restarted. Tune the timeout to
   fit your session length; see `../INSTRUCTOR_GUIDE.md` for the full
   math.

3. Create a read-only monitoring role for Telegraf/VCF Operations
   (see `../monitoring/telegraf-db.conf`):

   ```sql
   CREATE ROLE telegraf WITH LOGIN PASSWORD 'telegraf';
   GRANT pg_monitor TO telegraf;
   GRANT CONNECT ON DATABASE shopflow TO telegraf;
   ```

4. Allow the middle-tier VM to connect: add an entry to `pg_hba.conf` for
   its address/subnet, and set `listen_addresses = '*'` in
   `postgresql.conf`.

5. Point the middle tier at this host via `SHOPFLOW_DB_DSN`, e.g.:

   ```
   SHOPFLOW_DB_DSN="host=vcf-db01 dbname=shopflow user=shopflow password=shopflow%shopflow%"
   ```

   The API and worker processes each tag their own connections with a
   distinct `application_name` (`shopflow-api` / `shopflow-worker`, set
   automatically by their systemd units — see `common/dbutil.py`), so
   even though both run on the same VM you can always tell which process
   is holding a given connection.

For students who get DB access during the exercise, `pg_stat_activity` is
the ground truth for the leak:

```sql
SELECT pid, application_name, client_addr, state, now() - state_change AS age, query
FROM pg_stat_activity
WHERE datname = 'shopflow'
ORDER BY age DESC;
```

Every row with `application_name = 'shopflow-worker'` and
`state = 'idle in transaction'` is a leaked connection from a `CLEAROUT`
order; `shopflow-api` rows should never accumulate like this.

## Optional: forwarding worker failures to syslog

`middle/worker.py` can forward each `ORDER_FAILED` log line (the
exception raised by the seeded bug) to a syslog server — off by default.
On `vcf-middle01`, uncomment and set the `SHOPFLOW_SYSLOG_*` lines in
`shopflow-worker.service`, then:

```bash
sudo systemctl daemon-reload
sudo systemctl restart shopflow-worker
```

Note this is a precursor signal, not the 5xx itself: the worker never
sees HTTP status codes (that's nginx/the API's layer), so what lands in
syslog is the order-processing failure that, a few minutes later, is
what causes checkout to start returning 5xx once enough of them have
leaked a connection each. See `monitoring/METRICS.md` for how this fits
alongside the metrics and synthetic-check signals.

## Rotating the `shopflow` password on an already-deployed lab

Everything above reflects the current default (`shopflow%shopflow%`). If
you're changing it again later, three things need to agree: the actual
Postgres role, the middle tier's env file, and this repo (for the next
redeploy). The `telegraf` role is separate and untouched by this.

1. **On vcf-db01**, change the role password:

   ```bash
   sudo -u postgres psql -c "ALTER ROLE shopflow WITH PASSWORD 'NEW_PASSWORD_HERE';"
   ```

   Existing connections aren't dropped by this, but anything that
   reconnects afterward (including a worker reconnecting after an
   idle-in-transaction timeout) needs the new password — so the API and
   worker processes must be restarted, not just left running.

2. **On vcf-middle01**, update the DSN both services read:

   ```bash
   sudo sed -i "s/password=[^ ]*/password=NEW_PASSWORD_HERE/" /opt/shopflow/middle/middle.env
   sudo systemctl restart shopflow-api shopflow-worker
   ```

   Check it took: `grep SHOPFLOW_DB_DSN /opt/shopflow/middle/middle.env`
   and confirm both services came back up with
   `systemctl status shopflow-api shopflow-worker`.

3. **In this repo** (so a future `install_db.sh` / `install_middle.sh`
   run doesn't recreate the old password): update the `password=shopflow...`
   values in `deploy/install_db.sh`, `deploy/install_middle.sh`,
   `db/seed.py`, `common/dbutil.py`, and this file.

A password containing `!` (like the current default) is safe as-is in
all of the above — libpq DSN strings and systemd env files don't treat
`!` specially. The one place to be careful is typing it directly at an
**interactive** bash prompt outside of single quotes: bash's history
expansion can mangle a literal `!`. Prefer single-quoted strings
(`'shopflow%shopflow%'`) when typing it by hand, or paste it directly
into `psql`'s own prompt, which isn't affected.
