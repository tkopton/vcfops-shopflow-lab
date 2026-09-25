# DB tier — vcf-db01

1. Install PostgreSQL 14+ and create the database/role:

   ```bash
   sudo -u postgres createuser shopflow --pwprompt   # password: shopflow (or your own; update DSNs)
   sudo -u postgres createdb shopflow -O shopflow
   psql -U shopflow -h localhost -d shopflow -f schema.sql
   python3 -m pip install psycopg2-binary
   python3 seed.py --dsn "host=localhost dbname=shopflow user=shopflow password=shopflow"
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
   (`idle in transaction`) whenever it processes a `CLEAROUT` order. With
   `max_connections=40` and the default loadgen settings, the pool fills in
   roughly 12-15 minutes of normal traffic. `idle_in_transaction_session_timeout`
   is what eventually reaps the orphaned sessions and gives the environment
   its "recovers on its own, then breaks again" rhythm — a real setting many
   real environments configure as a safety net, and itself a clue worth
   noticing. Tune both to fit your session length; see
   `../INSTRUCTOR_GUIDE.md` for the full math.

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
   SHOPFLOW_DB_DSN="host=vcf-db01 dbname=shopflow user=shopflow password=shopflow"
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
