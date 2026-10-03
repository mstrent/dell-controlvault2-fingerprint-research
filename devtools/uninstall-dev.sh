#!/usr/bin/env bash
# Revert install-dev.sh.
# Run as: pkexec bash devtools/uninstall-dev.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root (pkexec)" >&2; exit 1; }
USER_NAME="${PKEXEC_UID:+$(id -nu "$PKEXEC_UID")}"; USER_NAME="${USER_NAME:-${SUDO_USER:-}}"; [[ -n $USER_NAME ]] || { echo "run with pkexec or sudo" >&2; exit 1; }
SRC="$(getent passwd "$USER_NAME" | cut -d: -f6)/projects/libfprint"

rm -f /etc/systemd/system/fprintd.service.d/cv2-dev.conf
ninja -C "$SRC/build-install" uninstall
systemctl daemon-reload
systemctl restart fprintd || true
echo "fprintd uses the system libfprint again"
