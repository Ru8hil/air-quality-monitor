#!/usr/bin/env bash
# Runs on the Azure VM as root. Called over SSH by .github/workflows/deploy.yml,
# which first copies release.tar.gz and airq.env into the given directory.
#
#   remote_install.sh <upload-dir> <release-id>
#
# 1. prepares the VM the first time (packages, service user, folders) - idempotent
# 2. installs the configuration file written from the GitHub secrets
# 3. unpacks the release and hands over to install_release.sh
set -euo pipefail
exec </dev/null   # nothing below may read the deploy password from stdin

UPLOAD=${1:?usage: remote_install.sh <upload-dir> <release-id>}
ID=${2:?usage: remote_install.sh <upload-dir> <release-id>}
export DEBIAN_FRONTEND=noninteractive

log() { echo "[prepare $(date -u +%H:%M:%S)] $*"; }

if ! dpkg -s python3-venv python3-pip curl >/dev/null 2>&1; then
  log "Installing Python and tools (first deployment only, about 1-2 minutes)"
  apt-get -o DPkg::Lock::Timeout=600 update -qq
  apt-get -o DPkg::Lock::Timeout=600 install -y -qq python3-venv python3-pip curl
fi

if ! id airq >/dev/null 2>&1; then
  log "Creating the airq service account"
  useradd --system --home-dir /var/lib/airq --no-create-home --shell /usr/sbin/nologin airq
fi
install -d -o root -g root -m 0755 /opt/airq /opt/airq/releases
install -d -o airq -g airq -m 0750 /var/lib/airq /var/lib/airq/incoming /var/lib/airq/quarantine
install -d -o root -g airq -m 0750 /etc/airq

log "Installing configuration (/etc/airq/airq.env)"
install -m 0640 -o root -g airq "$UPLOAD/airq.env" /etc/airq/airq.env

REL=/opt/airq/releases/$ID
rm -rf "$REL"
mkdir -p "$REL"
tar -xzf "$UPLOAD/release.tar.gz" -C "$REL"
echo "$ID" > "$REL/REVISION"
rm -rf "$UPLOAD"

exec bash "$REL/deploy/install_release.sh" "$REL" "$ID"
