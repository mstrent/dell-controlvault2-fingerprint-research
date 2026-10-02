#!/usr/bin/env python3
"""Decode CV (ControlVault) traffic from a usbmon pcap (LINKTYPE_USB_LINUX_MMAPPED).

Usage: cvdump.py capture.pcap [--cmd 0x6e,0x6c] [--full]
Prints each bulk OUT command and bulk IN reply on endpoint 1 with the CV
header decoded; --full hex-dumps the whole message.
"""
import struct, sys

def packets(path):
    with open(path, 'rb') as f:
        gh = f.read(24)
        magic = struct.unpack('<I', gh[:4])[0]
        assert magic in (0xa1b2c3d4, 0xa1b23c4d), "not a little-endian pcap"
        linktype = struct.unpack('<I', gh[20:24])[0]
        assert linktype in (189, 220), "unexpected linktype %d" % linktype
        hlen = 64 if linktype == 220 else 48
        while True:
            ph = f.read(16)
            if len(ph) < 16:
                return
            ts_s, ts_us, incl, _ = struct.unpack('<IIII', ph)
            data = f.read(incl)
            (urb_id, ev, xfer, epnum, devnum, busnum, flag_setup, flag_data,
             _, _, status, length, len_cap) = struct.unpack('<QBBBBHccqiiII', data[:40])
            yield ts_s + ts_us / 1e6, chr(ev), xfer, epnum, devnum, data[hlen:hlen + len_cap]

def hexdump(b, indent='      '):
    for i in range(0, len(b), 16):
        chunk = b[i:i + 16]
        print('%s%04x  %-48s %s' % (indent, i, ' '.join('%02x' % x for x in chunk),
              ''.join(chr(x) if 32 <= x < 127 else '.' for x in chunk)))

def main():
    args = sys.argv[1:]
    full = '--full' in args
    want = None
    if '--cmd' in args:
        want = {int(x, 16) for x in args[args.index('--cmd') + 1].split(',')}
    path = [a for a in args if a.endswith('.pcap')][0]
    t0 = None
    last_cmd = None
    for ts, ev, xfer, ep, dev, payload in packets(path):
        if xfer != 3 or (ep & 0x7f) != 1 or len(payload) < 0x2c:
            continue  # bulk transfers on EP1 carrying a CV header only
        t0 = t0 if t0 is not None else ts
        out = not (ep & 0x80)
        if out and ev != 'S' or (not out) and ev != 'C':
            continue
        total, = struct.unpack_from('<I', payload, 4)
        cmd, flags = struct.unpack_from('<HH', payload, 8)
        status, = struct.unpack_from('<I', payload, 0x14)
        if out:
            last_cmd = cmd
        shown = cmd if out else last_cmd
        if want and shown not in want:
            continue
        print('%7.2f %s cmd=0x%02x flags=0x%04x len=%d%s' % (
            ts - t0, 'OUT' if out else ' IN', cmd, flags, total,
            '' if out else ' status=0x%x' % status))
        if full:
            hexdump(payload[:total])

if __name__ == '__main__':
    main()
