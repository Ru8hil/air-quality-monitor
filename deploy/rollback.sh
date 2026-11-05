#!/usr/bin/env bash
# On the VM: switch back to the previous release and restart the services.
# Usage: sudo bash /opt/airq/current/deploy/rollback.sh
set -euo pipefail
APP_ROOT=/opt/airq
CURRENT=$(readlink -f "$APP_ROOT/current")
# shellcheck disable=SC2012  # release names are <hex>-<digits>
PREVIOUS=$(ls -1dt "$APP_ROOT"/releases/*/ | sed 's:/$::' | grep -vx "$CURRENT" | head -n 1 || true)
[[ -n "$PREVIOUS" ]] || { echo "No previous release available"; exit 1; }
echo "Rolling back: $(basename "$CURRENT") -> $(basename "$PREVIOUS")"
ln -sfn "$PREVIOUS" "$APP_ROOT/current.new"
mv -T "$APP_ROOT/current.new" "$APP_ROOT/current"
systemctl reset-failed airq-consumer airq-dashboard 2>/dev/null || true
systemctl restart airq-consumer airq-dashboard
systemctl --no-pager status airq-consumer airq-dashboard | head -20
