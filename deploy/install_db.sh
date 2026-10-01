#!/usr/bin/env bash
# Deploy the DB tier on vcf-db01. Debian/Ubuntu; adapt package names for
# RHEL-family (postgresql-server, postgresql-setup --initdb, etc).
#
# Usage: sudo ./install_db.sh [subnet-cidr-for-app-tiers, e.g. 10.0.0.0/24]
set -euo pipefail

LAB_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_SUBNET="${1:-0.0.0.0/0}"

apt-get update -qq
apt-get install -y -qq postgresql postgresql-contrib python3-psycopg2

PGVER="$(psql -V | grep -oP '\d+' | head -1)"
PGCONF="$(pg_lsclusters -h 2>/dev/null | awk '{print $6"/postgresql.conf"}' | head -1)"
if [ -z "$PGCONF" ] || [ ! -f "$PGCONF" ]; then
    PGCONF="/etc/postgresql/${PGVER}/main/postgresql.conf"
fi
PGHBA="$(dirname "$PGCONF")/pg_hba.conf"

sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
   IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'shopflow') THEN
      CREATE ROLE shopflow WITH LOGIN PASSWORD 'shopflow%shopflow%';
   END IF;
   IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'telegraf') THEN
      CREATE ROLE telegraf WITH LOGIN PASSWORD 'telegraf';
      GRANT pg_monitor TO telegraf;
   END IF;
END
\$\$;
SQL
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname = 'shopflow'" | grep -q 1 || \
    sudo -u postgres createdb shopflow -O shopflow
sudo -u postgres psql -d shopflow -v ON_ERROR_STOP=1 -f "$LAB_SRC/db/schema.sql"
sudo -u postgres psql -d shopflow -c "GRANT CONNECT ON DATABASE shopflow TO telegraf;"

python3 "$LAB_SRC/db/seed.py" --dsn "host=localhost dbname=shopflow user=shopflow password=shopflow%shopflow%"

# Lab-sized connection ceiling + the idle-in-transaction reaper. See
# db/README.md and INSTRUCTOR_GUIDE.md for the sizing rationale.
sed -i "/^max_connections/d;/^idle_in_transaction_session_timeout/d;/^listen_addresses/d" "$PGCONF"
{
  echo "listen_addresses = '*'"
  echo "max_connections = 40"
  echo "idle_in_transaction_session_timeout = 900000  # 15 min, ms"
} >> "$PGCONF"

echo "host shopflow shopflow ${APP_SUBNET} md5" >> "$PGHBA"
echo "host shopflow telegraf ${APP_SUBNET} md5" >> "$PGHBA"
echo "host shopflow telegraf 127.0.0.1/32 md5" >> "$PGHBA"

systemctl restart postgresql

echo "DB tier installed. max_connections=40, idle_in_transaction_session_timeout=15min."
echo "Adjust ${PGCONF} / ${PGHBA} further if your subnet layout differs."
