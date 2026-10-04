#!/usr/bin/env bash
# Install the udev hook that keeps the ControlVault2 reader passed through to
# the Windows VM across re-enumeration, and make the VM's own hostdev entry
# optional (so the VM still starts if the reader is missing or mid-flash).
# Run as: pkexec bash devtools/vm-usb/install.sh      (undo: uninstall.sh)
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root (pkexec)" >&2; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
VM=win11
RULE=/etc/udev/rules.d/90-cv2-vm-usb.rules

PORT=""
for d in /sys/bus/usb/devices/*; do
  if [[ "$(cat "$d/idVendor" 2>/dev/null)" == 0a5c && "$(cat "$d/idProduct" 2>/dev/null)" == 583? ]]; then
    PORT=$(basename "$d"); break
  fi
done
[[ -n "$PORT" ]] || { echo "ControlVault2 reader (0a5c:583x) not found" >&2; exit 1; }
echo ">>> reader is on USB port $PORT"

install -m 0755 "$HERE/cv2-vm-usb" /usr/local/sbin/cv2-vm-usb
cat > "$RULE" <<EOF
# Pass whatever appears on the ControlVault2 reader's port ($PORT) to the
# running $VM VM (installed by cv2-research devtools/vm-usb/install.sh)
ACTION=="add", SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", KERNEL=="$PORT", RUN+="/usr/bin/systemd-run --no-block --collect /usr/local/sbin/cv2-vm-usb add \$env{BUSNUM} \$env{DEVNUM}"
ACTION=="remove", SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", KERNEL=="$PORT", RUN+="/usr/bin/systemd-run --no-block --collect /usr/local/sbin/cv2-vm-usb remove"
EOF
restorecon -F /usr/local/sbin/cv2-vm-usb "$RULE" 2>/dev/null || true
udevadm control --reload
echo ">>> installed $RULE and /usr/local/sbin/cv2-vm-usb"

# startupPolicy='optional' on the reader's hostdev (the VM's only hostdev),
# so the VM still starts if the reader is missing or mid-flash
virt-xml -c qemu:///system "$VM" --edit --hostdev source.startupPolicy=optional --define >/dev/null
echo ">>> $VM: reader hostdev is startupPolicy=optional"
