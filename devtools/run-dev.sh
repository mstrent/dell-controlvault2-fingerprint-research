#!/usr/bin/env bash
# Run cv2-dev.py against the libfprint build tree with fprintd out of the way,
# then restore fprintd.
# Run as: pkexec bash devtools/run-dev.sh <action> [finger]
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root (pkexec)" >&2; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="${PKEXEC_UID:+$(id -nu "$PKEXEC_UID")}"; USER_NAME="${USER_NAME:-${SUDO_USER:-}}"; [[ -n $USER_NAME ]] || { echo "run with pkexec or sudo" >&2; exit 1; }
USER_HOME="$(getent passwd "$USER_NAME" | cut -d: -f6)"
export LIBFPRINT_BUILD="${LIBFPRINT_BUILD:-$USER_HOME/projects/libfprint/build}"

restore() {
  systemctl unmask --runtime fprintd >/dev/null 2>&1 || true
  systemctl start fprintd || true
  [[ -d "$HERE/prints" ]] && chown -R "$USER_NAME" "$HERE/prints"
  echo ">>> fprintd restored"
}
trap restore EXIT
systemctl mask --runtime fprintd >/dev/null
systemctl stop fprintd
python3 -u "$HERE/cv2-dev.py" "$@"
