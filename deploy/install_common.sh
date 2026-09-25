#!/usr/bin/env bash
# Common setup shared by the middle-tier and loadgen VMs: a service user,
# /opt/shopflow layout, and a Python venv. Run this first on any VM that
# will run Python components (not needed on vcf-db01 or vcf-frontend01,
# which only run their respective native services).
#
# Usage: sudo ./install_common.sh
set -euo pipefail

LAB_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! id shopflow &>/dev/null; then
    useradd --system --home-dir /opt/shopflow --create-home --shell /usr/sbin/nologin shopflow
fi

mkdir -p /opt/shopflow
cp -r "$LAB_SRC/common" /opt/shopflow/common

if [ ! -d /opt/shopflow/venv ]; then
    python3 -m venv /opt/shopflow/venv
fi

chown -R shopflow:shopflow /opt/shopflow
echo "Common setup complete. LAB_SRC=$LAB_SRC"
