#!/usr/bin/env bash
# Deploy the frontend (reverse proxy) tier on vcf-frontend01. Debian/Ubuntu.
# Edit frontend/nginx-shopflow.conf's upstream block first if
# vcf-middle01 has a different hostname, or rely on DNS/hosts-file
# entries.
#
# Usage: sudo ./install_frontend.sh
set -euo pipefail

LAB_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

apt-get update -qq
apt-get install -y -qq nginx

rm -f /etc/nginx/sites-enabled/default
cp "$LAB_SRC/frontend/nginx-shopflow.conf" /etc/nginx/conf.d/shopflow.conf

nginx -t
systemctl restart nginx
systemctl enable nginx

echo "Frontend tier installed. Confirm vcf-middle01 resolves (DNS or /etc/hosts)."
