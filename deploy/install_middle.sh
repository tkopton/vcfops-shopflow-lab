#!/usr/bin/env bash
# Deploy the middle tier on vcf-middle01: local Redis (cache + job queue),
# the API process, and the worker process. Run install_common.sh first.
# Debian/Ubuntu; adapt package names for RHEL-family.
#
# Usage: sudo ./install_middle.sh
set -euo pipefail

LAB_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

apt-get update -qq
apt-get install -y -qq redis-server
sed -i "s/^bind .*/bind 127.0.0.1 -::1/" /etc/redis/redis.conf
systemctl restart redis-server
systemctl enable redis-server

mkdir -p /opt/shopflow/middle
cp "$LAB_SRC"/middle/*.py /opt/shopflow/middle/
cp "$LAB_SRC"/middle/gunicorn_conf.py /opt/shopflow/middle/
cp "$LAB_SRC"/middle/requirements.txt /opt/shopflow/middle/

/opt/shopflow/venv/bin/pip install --quiet -r /opt/shopflow/middle/requirements.txt

if [ ! -f /opt/shopflow/middle/middle.env ]; then
    cat > /opt/shopflow/middle/middle.env <<'EOF'
SHOPFLOW_DB_DSN=host=vcf-db01 dbname=shopflow user=shopflow password=shopflow%shopflow%
SHOPFLOW_DB_POOL_MIN=2
# SHOPFLOW_DB_POOL_MAX is intentionally NOT set here -- the API and
# worker services each set their own value via Environment= in their
# unit files (shopflow-api.service / shopflow-worker.service), since
# they need very different ceilings. See the comments in those files.
SHOPFLOW_REDIS_HOST=localhost
SHOPFLOW_REDIS_PORT=6379
SHOPFLOW_QUEUE_KEY=shopflow:orders
SHOPFLOW_PRODUCTS_TTL=300
SHOPFLOW_FEATURED_TTL=8
EOF
fi

chown -R shopflow:shopflow /opt/shopflow/middle

install -m 0644 "$LAB_SRC/middle/shopflow-api.service" /etc/systemd/system/shopflow-api.service
install -m 0644 "$LAB_SRC/middle/shopflow-worker.service" /etc/systemd/system/shopflow-worker.service
systemctl daemon-reload
systemctl enable --now shopflow-api
systemctl enable --now shopflow-worker

echo "Middle tier installed (redis + API + worker, all local)."
echo "Edit /opt/shopflow/middle/middle.env if hostnames differ, then:"
echo "  systemctl restart shopflow-api shopflow-worker"
