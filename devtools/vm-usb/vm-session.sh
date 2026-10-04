#!/usr/bin/env bash
# Windows VM session with the ControlVault2 reader, recording usbmon on the
# host (the capture continues through re-enumeration: it records the bus).
#
#   pkexec bash vm-session.sh start           mask fprintd and pcscd, start the VM
#   pkexec bash vm-session.sh capture NAME    record windows-captures/NAME.pcap
#                                           (stops any capture still running)
#   pkexec bash vm-session.sh stopcap         stop the capture
#   pkexec bash vm-session.sh reset           disconnect and reconnect the reader
#                                           (re-enumeration test)
#   pkexec bash vm-session.sh end             stop capture, shut the VM down,
#                                           restore fprintd and pcscd
set -uo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root (pkexec or sudo)" >&2; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
CAPDIR="$(cd "$HERE/../.." && pwd)/windows-captures"
USER_NAME="${SUDO_USER:-${PKEXEC_UID:+$(id -nu "$PKEXEC_UID")}}"
VM=win11
V="virsh -c qemu:///system"
PIDFILE=/run/cv2-vm-capture.pid

reader() {   # prints "busnum devnum port vid:pid" of the reader
  for d in /sys/bus/usb/devices/*; do
    if [[ "$(cat "$d/idVendor" 2>/dev/null)" == 0a5c && "$(cat "$d/idProduct" 2>/dev/null)" == 583? ]]; then
      echo "$(cat "$d/busnum") $(cat "$d/devnum") $(basename "$d") 0a5c:$(cat "$d/idProduct")"; return
    fi
  done
}

stopcap() {
  if [[ -f $PIDFILE ]]; then
    kill "$(cat $PIDFILE)" 2>/dev/null; sleep 1; rm -f $PIDFILE
    chown -R "$USER_NAME" "$CAPDIR" 2>/dev/null
    echo ">>> capture stopped"
  fi
}

case "${1:-}" in
  start)
    systemctl mask --runtime fprintd pcscd.socket pcscd >/dev/null 2>&1
    systemctl stop fprintd pcscd.socket pcscd 2>/dev/null
    echo ">>> fprintd and pcscd masked; reader: $(reader)"
    $V domstate $VM | grep -q running || $V start $VM
    ;;
  capture)
    [[ -n "${2:-}" ]] || { echo "usage: capture NAME" >&2; exit 1; }
    stopcap
    read -r BUS _ <<<"$(reader)"
    [[ -n "${BUS:-}" ]] || BUS=1
    modprobe usbmon
    mkdir -p "$CAPDIR"
    tcpdump -q -i "usbmon$BUS" -s 0 -U -w "$CAPDIR/$2.pcap" 2>/dev/null &
    echo $! > $PIDFILE
    echo ">>> recording usbmon$BUS -> windows-captures/$2.pcap"
    ;;
  stopcap)
    stopcap
    ;;
  reset)
    # Disable and re-enable the hub port: a real disconnect, so the reader
    # re-enumerates with a new device number, like the chip's own resets
    read -r BUS DEV PORT ID <<<"$(reader)"
    if [[ $PORT == *.* ]]; then
      HUB=${PORT%.*}; CTL=/sys/bus/usb/devices/$HUB:1.0/$HUB-port${PORT##*.}/disable
    else
      CTL=/sys/bus/usb/devices/$BUS-0:1.0/usb$BUS-port${PORT#*-}/disable
    fi
    echo ">>> cycling port $PORT ($ID, device $DEV)"
    echo 1 > "$CTL"; sleep 2; echo 0 > "$CTL"
    sleep 4
    echo ">>> reader now: $(reader)"
    $V dumpxml $VM | grep -A4 "<hostdev mode='subsystem' type='usb'" | grep -E "address bus|vendor|product"
    journalctl -t cv2-vm-usb --since "-1min" -o cat --no-pager
    ;;
  end)
    stopcap
    if $V domstate $VM | grep -q running; then
      $V shutdown $VM
      for _ in $(seq 1 60); do $V domstate $VM | grep -q 'shut off' && break; sleep 2; done
    fi
    systemctl unmask --runtime fprintd pcscd.socket pcscd >/dev/null 2>&1
    systemctl start pcscd.socket 2>/dev/null
    echo ">>> VM: $($V domstate $VM); fprintd and pcscd restored"
    ;;
  *)
    sed -n '2,15p' "$0"; exit 1
    ;;
esac
