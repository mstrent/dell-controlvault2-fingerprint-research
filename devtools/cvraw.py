#!/usr/bin/env python3
"""Dump every CV message (bulk OUT/IN on EP1) and interrupt (EP 0x85) in a
usbmon pcap as hex, one per line: '<time> OUT|IN|INT <hex>'."""
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from cvdump import packets  # noqa: E402

t0 = None
for ts, ev, xfer, ep, dev, p in packets(sys.argv[1]):
    if ev not in 'SC':
        continue
    if xfer == 1 and ep == 0x85 and ev == 'C':
        t0 = t0 or ts
        print('%7.2f INT %s' % (ts - t0, p.hex(' ')))
    elif xfer == 3 and (ep & 0x7f) == 1 and len(p) >= 0x2c:
        out = not (ep & 0x80)
        if (out and ev == 'S') or (not out and ev == 'C'):
            t0 = t0 or ts
            n = struct.unpack_from('<I', p, 4)[0]
            print('%7.2f %s %s' % (ts - t0, 'OUT' if out else ' IN', p[:n].hex()))
