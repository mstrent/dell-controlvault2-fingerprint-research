#!/usr/bin/env python3
"""Print the cv2 template handle of every print fprintd has stored.
Read-only. Run as: pkexec python3 devtools/fprintd-handles.py"""
import glob

import gi

gi.require_version('FPrint', '2.0')
from gi.repository import FPrint  # noqa: E402

for path in sorted(glob.glob('/var/lib/fprint/*/*/*/*')):
    with open(path, 'rb') as f:
        p = FPrint.Print.deserialize(f.read())
    data = p.props.fpi_data
    handle = '%08x' % data.unpack()[0] if data and data.get_type_string() == '(u)' else '-'
    print('%-10s %-20s %-6s %s' % (p.props.username, p.props.finger.value_nick,
                                   p.props.driver, handle))
