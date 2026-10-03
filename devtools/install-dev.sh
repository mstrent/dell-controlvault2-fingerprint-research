#!/usr/bin/env bash
# Install the libfprint build with the cv2 driver into /usr/local and make
# fprintd use it. Revert with uninstall-dev.sh.
# Run as: pkexec bash devtools/install-dev.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root (pkexec)" >&2; exit 1; }
USER_NAME="${PKEXEC_UID:+$(id -nu "$PKEXEC_UID")}"; USER_NAME="${USER_NAME:-${SUDO_USER:-}}"; [[ -n $USER_NAME ]] || { echo "run with pkexec or sudo" >&2; exit 1; }
SRC="$(getent passwd "$USER_NAME" | cut -d: -f6)/projects/libfprint"
cd "$SRC"

if [[ ! -d "$SRC/build-install" ]]; then
  runuser -u "$USER_NAME" -- meson setup "$SRC/build-install" "$SRC" \
    --prefix=/usr/local -Ddrivers=cv2 -Dintrospection=false -Ddoc=false \
    -Dudev_rules=disabled -Dudev_hwdb=disabled -Dinstalled-tests=false
fi
runuser -u "$USER_NAME" -- meson compile -C "$SRC/build-install"
meson install -C "$SRC/build-install" --no-rebuild
restorecon -R /usr/local/lib64 || true

mkdir -p /etc/systemd/system/fprintd.service.d
cat > /etc/systemd/system/fprintd.service.d/cv2-dev.conf <<'EOF'
[Service]
Environment=LD_LIBRARY_PATH=/usr/local/lib64
EOF
systemctl daemon-reload
systemctl restart fprintd
echo "fprintd now uses /usr/local/lib64/libfprint-2.so.2"
