#!/usr/bin/env bash
# Deploy the traffic generator on vcf-loadgen01 (optional 4th VM, kept
# separate from the tiers under test so its own resource usage doesn't
# muddy their metrics). Run install_common.sh first.
#
# Usage: sudo ./install_loadgen.sh
set -euo pipefail

LAB_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

mkdir -p /opt/shopflow/loadgen
cp "$LAB_SRC"/loadgen/*.py /opt/shopflow/loadgen/
cp "$LAB_SRC"/loadgen/requirements.txt /opt/shopflow/loadgen/

/opt/shopflow/venv/bin/pip install --quiet -r /opt/shopflow/loadgen/requirements.txt

if [ ! -f /opt/shopflow/loadgen/loadgen.env ]; then
    cat > /opt/shopflow/loadgen/loadgen.env <<'EOF'
SHOPFLOW_BASE_URL=http://vcf-frontend01
SHOPFLOW_VUSERS=15
SHOPFLOW_CHECKOUT_EVERY_S=5
SHOPFLOW_CLEAROUT_WEIGHT=0.02
EOF
fi

chown -R shopflow:shopflow /opt/shopflow/loadgen

install -m 0644 "$LAB_SRC/loadgen/shopflow-loadgen.service" /etc/systemd/system/shopflow-loadgen.service
systemctl daemon-reload
systemctl enable --now shopflow-loadgen

echo "Load generator installed. Edit /opt/shopflow/loadgen/loadgen.env to tune"
echo "timing, then: systemctl restart shopflow-loadgen"
echo "No 4th VM to spare? Run 'python3 loadgen/loadgen.py' from any machine"
echo "that can reach vcf-frontend01 instead -- see loadgen/loadgen.py."
