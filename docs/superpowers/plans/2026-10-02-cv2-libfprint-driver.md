# CV2 libfprint Driver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An upstreamable libfprint match-on-chip driver (`cv2`) for the Dell ControlVault2 / Broadcom BCM5880 reader (USB `0a5c:5834`) that enrolls, verifies, identifies and deletes fingerprints, with unit and umockdev tests.

**Architecture:** A GLib-level protocol layer (`cv2-proto.c`: request builders, reply parser, status/firmware tables, small pure helpers) is unit-tested against bytes copied from USB captures. A device layer (`cv2.c`: `FpiDeviceCv2`) runs one `FpiSsm` per operation on top of a small command transport (bulk OUT → interrupt "reply ready" → bulk IN), with a shared capture sub-SSM and a cleanup SSM that always leaves the chip idle. Device-layer behaviour is verified on the real reader with a dev script, then frozen into umockdev recordings.

**Tech Stack:** C (GLib 2.68 API, GObject, GUsb via libfprint's `FpiUsbTransfer`), meson, GLib test framework, Python + PyGObject (`FPrint` typelib) for umockdev tests and the dev script, umockdev, tshark.

**Spec:** `docs/superpowers/specs/2026-10-02-cv2-libfprint-driver-design.md` (research repo `~/projects/cv2-research`). Protocol: `protocol/PROTOCOL.md`. Read both before starting.

## Repositories and machine

- **Research repo** `~/projects/cv2-research` (git, local only): protocol notes, captures, `cvtool.py`, spec, this plan, and the new `devtools/`.
- **libfprint fork** `~/projects/libfprint`, branch `cv2-driver`, cloned from `https://gitlab.freedesktop.org/libfprint/libfprint.git`. All driver code goes here. Nothing is pushed anywhere in this plan.
- **Hardware:** the Latitude 7490 this runs on has the reader, firmware `00412015`. Steps marked **(hardware, user)** need a human to press fingers and approve a `pkexec` prompt. fprintd must be out of the way for those steps; the provided scripts mask and restore it.
- `~/projects/cv2-fingerprint` holds the patched-TOD stopgap that currently provides fingerprint login. Do not modify it except where Task 11 says so.

## Global Constraints

- Upstream libfprint conventions: GObject C, `FpiSsm`, `FpiUsbTransfer`, GLib ≤ 2.68 API (`GLIB_VERSION_MAX_ALLOWED`), builds without new warnings under libfprint's `common_cflags`, passes `scripts/uncrustify.sh`.
- Protocol knowledge comes from USB captures only; nothing from the proprietary binary.
- Interface 0 only. Never touch interfaces 1–3 (smart card / vendor), never send firmware-update commands, never call `g_usb_device_reset` (it would disturb the smart-card interfaces).
- Device ID table: `0a5c:5834` only. Features: `VERIFY | IDENTIFY | STORAGE | STORAGE_DELETE`. Not `DUPLICATES_CHECK`, `STORAGE_LIST`, `STORAGE_CLEAR`. Scan type press. 4 enroll stages.
- Print driver data: `GVariant "(u)"` holding the chip's 32-bit template handle; print type `FPI_PRINT_RAW`, device-stored.
- Enroll requires `USH_REL_UPGRADE_VER` ≥ `00412015`. Older or `0x24` at commit → `FP_DEVICE_ERROR_NOT_SUPPORTED` whose message says to update with Dell's ControlVault2 firmware package.
- Command transfers: 5 s timeout. Finger wait: no timeout.
- libfprint commit subjects: `cv2: …` for driver code, `tests/cv2: …` for tests. End every commit message with `Co-Authored-By: Claude <noreply@anthropic.com>`.

## Spec refinements made by this plan

Captures examined while planning showed details the spec didn't capture. Task 1 writes these into the spec and `PROTOCOL.md`; every later task assumes them.

1. **Bulk IN buffer.** The stock driver reads replies with a 4196-byte buffer. The commit reply's interrupt announced 65 bytes and 65 arrived, but the header says 64. Rule: read up to `CV2_REPLY_BUF_LEN` (4196); the header's total length must be ≤ bytes read; trailing bytes are ignored.
2. **Version text** is newline-separated `KEY:VALUE` lines in a kind-1 string parameter (not space-separated). Real reply: 848 bytes (multi-packet).
3. **No finger-lift wait.** The stock driver sends the next `0x66` immediately after each `0x6c`; the chip produces one `type 3` event per press.
4. **Cancellation between commands.** Command transfers are never cancelled mid-flight (that would leave an unread reply and desynchronise the next command). The transport checks for cancellation before sending each command; only the finger wait uses the cancellable.
5. **Cleanup always sends `0x68`** when a session is open (a `0x68` with nothing pending returns status 0, `cvtool.log`), so there is no `capture_pending` flag.
6. **Stale replies.** If a reply's command ID doesn't match the request, the transport discards it and waits for the next one (at most 2 times) before failing with a protocol error. A reply can be left over after a timeout or a killed client.
7. **Finger status.** `NEEDED` while waiting, `PRESENT` on the finger event, `NONE` after the sample/match reply and at operation end.
8. **Close after success.** A failing `0x04` after a successful operation is logged, not reported (the template is already committed or deleted).
9. **`0x85` is common.** `cvtool.log` shows `0x66` returning `0x85` at the start of every new client, so the cancel-and-retry path runs routinely.
10. **Device removal needs no driver code.** libfprint's core (`fpi-device.c`) returns `FP_DEVICE_ERROR_REMOVED` for any operation that completes after the device was removed, so the driver doesn't map `G_USB_DEVICE_ERROR_NO_DEVICE` itself.

## Review Focus

Inputs the spec implies but that the main tests may not exercise, most likely first. Each has a test or check in the owning task.

1. **A reply left over from a previous client or a timed-out command** → the transport must resynchronise, not fail every later command. Pinned by `test_peek_cmd` (Task 4) plus Task 10's check that the recording includes the `0x85` retry, and the Task 7 hardware step that kills the dev script mid-enroll and runs again.
2. **Prints whose data isn't ours** (empty template, a print restored from the TOD driver's storage) → `FP_DEVICE_ERROR_DATA_INVALID` without touching the reader. Pinned in `tests/cv2/custom.py` (Task 10) and the Task 8 hardware step.
3. **A firmware that needs more than 4 accepted samples** (old firmware needed 13) → progress stays at stage 3 until the done flag, then 4; no assertion or overflow. Pinned by `test_enroll_next_stage` (Task 5).
4. **The chip reports a match for a handle the host didn't send** → protocol error, never a match on the wrong print. Pinned by `test_template_index` (Task 5).
5. **Cancel or suspend in the middle of an operation** → the chip ends idle and the next operation works. Pinned by `tests/cv2-cancel` (Task 10) and Task 11's Ctrl-C and suspend checks.

---

### Task 1: Probe the plaintext version query and record protocol refinements (research repo)

**Files:**
- Modify: `~/projects/cv2-research/cvtool.py`
- Modify: `~/projects/cv2-research/protocol/PROTOCOL.md`
- Modify: `~/projects/cv2-research/docs/superpowers/specs/2026-10-02-cv2-libfprint-driver-design.md`

**Interfaces:**
- Produces: `CV2_VERSION_FLAGS` value (`0x0440`, `0x0040`, or "unsupported") used by Task 3 and Task 6.

- [ ] **Step 1: Add `0x39` and a `version` action to cvtool**

In `cvtool.py`, add `0x39: 'get_version'` to `ALLOWED`:

```python
ALLOWED = {0x02: 'open', 0x04: 'close', 0x66: 'capture_start', 0x68: 'capture_cancel',
           0x6c: 'enroll_update', 0x6d: 'enroll_discard', 0x6e: 'enroll_commit',
           0x8a: 'enrollment_started', 0x2f: 'match', 0x0a: 'delete_template',
           0x39: 'get_version'}
```

Add this function above `def main():`

```python
def do_version(cv, flags):
    try:
        st, r = cv.call(0x39, [param(0, u32(0))], hdr_handle=0, flags=flags, quiet=True)
    except usb.core.USBTimeoutError:
        log('   flags 0x%04x -> no reply (timeout)' % flags)
        return
    log('   flags 0x%04x -> status 0x%x' % (flags, st))
    if st == 0 and len(r) >= 0x34:
        kind, n = struct.unpack_from('<II', r, 0x2c)
        text = r[0x34:0x34 + n].split(b'\0')[0].decode('ascii', 'replace')
        log('   param kind %d, %d bytes:' % (kind, n))
        log(text)
```

In `main()`, add `'version'` to the `choices` list, add the option, and dispatch it:

```python
    ap.add_argument('action', choices=['commit-noenroll', 'enroll', 'match', 'delete', 'version'])
    ...
    ap.add_argument('--flags', default='0x0440', help='header flags for the version query')
    ...
        if a.action == 'version':
            do_version(cv, int(a.flags, 16))
        elif a.action == 'match':
```

- [ ] **Step 2: Run it with the plaintext flags (hardware, user)**

Run: `pkexec bash ~/projects/cv2-research/run-cvtool.sh version --flags 0x0440`
Expected (if supported): `flags 0x0440 -> status 0x0`, `param kind 1, 796 bytes:` and text starting `USH_CHIPID:05810211`, including `USH_REL_UPGRADE_VER:00412015`.

- [ ] **Step 3: If Step 2 did not return status 0, try the Windows flags (hardware, user)**

Run: `pkexec bash ~/projects/cv2-research/run-cvtool.sh version --flags 0x0040`

Record the outcome as one of:
- **A:** `0x0440` works → `CV2_VERSION_FLAGS = 0x0440` (the plan's code as written).
- **B:** only `0x0040` works → in Task 3 set `CV2_VERSION_FLAGS` to `0x0040` and change `VERSION_REQ`'s flag bytes from `4004` to `4000` (first line, bytes 10–11: `...390040040200...` → `...390040000200...`).
- **C:** neither works → in Task 3 still add the builder (unit-tested), but in Task 6 make `cv2_open` call `fpi_device_open_complete (dev, NULL)` right after claiming the interface instead of sending the query (leave `version_cb` out). The firmware gate then relies on `0x24` at commit.

- [ ] **Step 4: Check the chip still works after the probe (hardware, user)**

Run: `pkexec bash ~/projects/cv2-research/run-cvtool.sh commit-noenroll`
Expected: `0x02` status 0, both commits status `0x8d` (no enrollment in progress), `0x68` and `0x04` status 0. This shows normal commands still work after `0x39`.

- [ ] **Step 5: Update `protocol/PROTOCOL.md`**

Make these edits:
- In *Firmware requirement*, replace "returned by command `0x39` (see below) as space-separated `KEY:VALUE` text" with "returned by command `0x39` as newline-separated `KEY:VALUE` lines in a kind-1 string parameter (796 bytes on `00412015`; the reply is 848 bytes, several USB packets)".
- In the *Commands* table row for `0x39`, replace the "Seen only in captures of the Windows driver…unconfirmed" text with the Step 2/3 outcome, e.g. "Plaintext request: flags `0x0440`, header handle 0, param `0:4 = 0`; reply `1:n` version text (tested 2026-10-02 with `cvtool.py version`)".
- In *Transport* step 3, append: "The stock driver reads with a 4196-byte buffer. The interrupt length can exceed the message (commit: interrupt 65, header 64, 65 bytes read); trust the header's total length."
- In *Capture and finger events*, append: "There is no finger-removed event. The stock driver starts the next capture immediately after each sample; the chip raises one `type 3` event per press."
- In *Status codes*, `0x85` row, append to "Observed meaning": "seen on the first `0x66` of every new client after another one exited".
- In *Open questions*, delete item 6 (version query) if outcome A or B.

- [ ] **Step 6: Add the refinements to the spec**

Append this section to the end of `docs/superpowers/specs/2026-10-02-cv2-libfprint-driver-design.md`:

```markdown
## Amendments (2026-10-02, from implementation planning)

Captures examined during planning refine the design above; where they
conflict, this section wins.

1. Bulk IN replies are read with a 4196-byte buffer. The header's total length
   must be ≤ bytes read; trailing bytes are ignored (the commit reply's
   interrupt announced 65 bytes for a 64-byte message).
2. The version text is newline-separated `KEY:VALUE` lines.
3. There is no finger-lift wait between enroll samples; the stock driver
   doesn't wait and the chip raises one finger event per press.
4. Command transfers are not cancelled mid-flight. The transport checks for
   cancellation before sending each command; only the finger wait uses the
   cancellable. This prevents an unread reply from desynchronising the next
   command.
5. Cleanup always sends `0x68` when a session is open (it returns 0 with
   nothing pending), so there is no `capture_pending` flag.
6. A reply whose command ID doesn't match the request is discarded (at most
   twice) before failing with a protocol error.
7. Finger status: `NEEDED` while waiting for a press, `PRESENT` on the finger
   event, `NONE` after the reply and at operation end.
8. A failing `0x04` after a successful operation is logged, not reported.
9. Device removal: libfprint's core already reports `FP_DEVICE_ERROR_REMOVED`
   for operations that complete after removal, so the driver has no mapping
   for `G_USB_DEVICE_ERROR_NO_DEVICE` and no unit test for it.
```

- [ ] **Step 7: Commit (research repo)**

```bash
cd ~/projects/cv2-research
git add cvtool.py cvtool.log protocol/PROTOCOL.md docs/superpowers/specs/2026-10-02-cv2-libfprint-driver-design.md
git commit -m "Version query probe; protocol and spec refinements from planning

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: libfprint checkout, driver skeleton and hardware dev tools

**Files:**
- Create: `~/projects/libfprint` (clone), branch `cv2-driver`
- Create: `libfprint/drivers/cv2/cv2.h`, `libfprint/drivers/cv2/cv2.c`
- Modify: `meson.build` (`drivers_info`), `libfprint/meson.build` (`driver_sources`)
- Modify: `libfprint/fprint-list-udev-hwdb.c` (remove `0a5c:5834` from the unsupported list), `data/autosuspend.hwdb` (regenerated)
- Create (research repo): `devtools/run-dev.sh`, `devtools/cv2-dev.py`, `devtools/.gitignore`

**Interfaces:**
- Produces: type `FpiDeviceCv2` (`FPI_DEVICE_CV2 ()`), driver id `"cv2"`, `CV2_INTERFACE`; dev-script actions `info`, `enroll <finger>`, `verify <finger>`, `identify`, `delete <finger>`, `cancel-enroll`.

- [ ] **Step 1: Install build and test dependencies**

```bash
sudo dnf -y builddep libfprint
sudo dnf -y install git meson ninja-build gcc umockdev umockdev-devel wireshark-cli python3-gobject uncrustify
```

Expected: both commands finish without errors.

- [ ] **Step 2: Clone and branch**

```bash
cd ~/projects
git clone https://gitlab.freedesktop.org/libfprint/libfprint.git
cd libfprint
git switch -c cv2-driver
```

- [ ] **Step 3: Baseline build and tests**

```bash
cd ~/projects/libfprint
meson setup build -Ddoc=false
meson compile -C build
meson test -C build --print-errorlogs
```

Expected: build succeeds; tests pass or skip (driver tests skip with exit 77 when umockdev is missing; with it installed they pass). Write down any test that already fails on a clean tree so it isn't blamed on the driver later.

- [ ] **Step 4: Register the driver in meson**

In `meson.build`, inside `drivers_info`, add after `'secugen': {},`:

```meson
    'cv2': {},
```

In `libfprint/meson.build`, inside `driver_sources`, add after the `'secugen'` line:

```meson
    'cv2' : files('drivers/cv2/cv2.c'),
```

- [ ] **Step 5: Write the skeleton driver**

Create `libfprint/drivers/cv2/cv2.h`:

```c
/*
 * Broadcom ControlVault2 (BCM5880) fingerprint driver
 *
 * Copyright (C) 2026 Matt Trent <matt@thetrents.org>
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 *
 * This library is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
 * Lesser General Public License for more details.
 *
 * You should have received a copy of the GNU Lesser General Public
 * License along with this library; if not, write to the Free Software
 * Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
 */

#pragma once

#include "fpi-device.h"

G_DECLARE_FINAL_TYPE (FpiDeviceCv2, fpi_device_cv2, FPI, DEVICE_CV2, FpDevice)
```

Create `libfprint/drivers/cv2/cv2.c`:

```c
/*
 * Broadcom ControlVault2 (BCM5880) fingerprint driver
 *
 * Copyright (C) 2026 Matt Trent <matt@thetrents.org>
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 *
 * This library is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
 * Lesser General Public License for more details.
 *
 * You should have received a copy of the GNU Lesser General Public
 * License along with this library; if not, write to the Free Software
 * Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
 */

/*
 * Match-on-chip driver for the fingerprint function of the Dell ControlVault2
 * (Broadcom BCM5880, USB 0a5c:5834). Only interface 0 is used; the
 * smart-card interfaces are left alone.
 *
 * The protocol was worked out from USB captures of the reader only.
 *
 * Security: templates are stored and matched inside the chip, and no
 * fingerprint images or templates cross USB. The plaintext interface used
 * here does not authenticate replies, so the host trusts the reader's match
 * result, as with other match-on-chip drivers in libfprint.
 */

#define FP_COMPONENT "cv2"

#include "drivers_api.h"

#include "cv2.h"

#define CV2_INTERFACE 0

struct _FpiDeviceCv2
{
  FpDevice parent;
};

G_DEFINE_TYPE (FpiDeviceCv2, fpi_device_cv2, FP_TYPE_DEVICE)

static const FpIdEntry id_table[] = {
  { .vid = 0x0a5c, .pid = 0x5834, },
  { .vid = 0,      .pid = 0,      .driver_data = 0 },
};

/* ---- Device open and close ---- */

static void
cv2_open (FpDevice *dev)
{
  GError *error = NULL;

  if (!g_usb_device_claim_interface (fpi_device_get_usb_device (dev),
                                     CV2_INTERFACE, 0, &error))
    {
      fpi_device_open_complete (dev, error);
      return;
    }

  fpi_device_open_complete (dev, NULL);
}

static void
cv2_close (FpDevice *dev)
{
  GError *error = NULL;

  g_usb_device_release_interface (fpi_device_get_usb_device (dev),
                                  CV2_INTERFACE, 0, &error);
  fpi_device_close_complete (dev, error);
}

/* ---- GObject ---- */

static void
fpi_device_cv2_init (FpiDeviceCv2 *self)
{
}

static void
fpi_device_cv2_class_init (FpiDeviceCv2Class *klass)
{
  FpDeviceClass *dev_class = FP_DEVICE_CLASS (klass);

  dev_class->id = FP_COMPONENT;
  dev_class->full_name = "Broadcom ControlVault2 (BCM5880) Fingerprint Reader";
  dev_class->type = FP_DEVICE_TYPE_USB;
  dev_class->id_table = id_table;
  dev_class->scan_type = FP_SCAN_TYPE_PRESS;
  dev_class->nr_enroll_stages = 4;
  dev_class->temp_hot_seconds = -1;

  dev_class->open = cv2_open;
  dev_class->close = cv2_close;

  fpi_device_class_auto_initialize_features (dev_class);
}
```

- [ ] **Step 6: Remove the device from the unsupported list and regenerate the hwdb**

In `libfprint/fprint-list-udev-hwdb.c`, delete the line:

```c
  { .vid = 0x0a5c, .pid = 0x5834 },
```

Then:

```bash
cd ~/projects/libfprint
meson compile -C build
meson compile -C build sync-udev-hwdb
git diff --stat data/autosuspend.hwdb
```

Expected: `data/autosuspend.hwdb` changes: `usb:v0A5Cp5834*` leaves the unsupported block and appears under `# Supported by libfprint driver cv2`.

- [ ] **Step 7: Run the hwdb and metainfo tests**

Run: `meson test -C build udev-hwdb metainfo-validate --print-errorlogs`
Expected: both PASS (metainfo-validate may SKIP if `appstreamcli` isn't installed).

- [ ] **Step 8: Write the dev tools (research repo)**

Create `~/projects/cv2-research/devtools/.gitignore`:

```
prints/
```

Create `~/projects/cv2-research/devtools/run-dev.sh`:

```bash
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
```

Create `~/projects/cv2-research/devtools/cv2-dev.py`:

```python
#!/usr/bin/env python3
"""Drive the cv2 libfprint driver from a build tree, for hardware checks.

Run through run-dev.sh (root, fprintd stopped):
  pkexec bash devtools/run-dev.sh info
  pkexec bash devtools/run-dev.sh enroll right-index
  pkexec bash devtools/run-dev.sh verify right-index
  pkexec bash devtools/run-dev.sh identify
  pkexec bash devtools/run-dev.sh delete right-index
  pkexec bash devtools/run-dev.sh verify-bogus
  pkexec bash devtools/run-dev.sh cancel-enroll
Enrolled prints are saved in devtools/prints/<finger>.print.
"""
import os
import sys

BUILD = os.environ.get('LIBFPRINT_BUILD',
                       os.path.expanduser('~/projects/libfprint/build'))
LIB = os.path.join(BUILD, 'libfprint')

# Re-exec with the build tree's library and typelib, like
# tests/create-driver-test.py does.
if os.environ.get('LD_LIBRARY_PATH') != LIB:
    os.environ['LD_LIBRARY_PATH'] = LIB
    os.environ['GI_TYPELIB_PATH'] = LIB
    os.environ['FP_DRIVERS_ALLOWLIST'] = 'cv2'
    os.environ.setdefault('G_MESSAGES_DEBUG', 'libfprint-cv2 libfprint-device')
    os.execv(sys.executable, [sys.executable] + sys.argv)

import gi
gi.require_version('FPrint', '2.0')
from gi.repository import FPrint, GLib, Gio

PRINTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'prints')
FINGERS = {
    'right-index': FPrint.Finger.RIGHT_INDEX,
    'left-index': FPrint.Finger.LEFT_INDEX,
    'right-thumb': FPrint.Finger.RIGHT_THUMB,
}


def open_device():
    ctx = FPrint.Context()
    ctx.enumerate()
    devs = [d for d in ctx.get_devices() if d.get_driver() == 'cv2']
    if not devs:
        sys.exit('no cv2 device (is the driver built? is fprintd stopped?)')
    d = devs[0]
    d.open_sync()
    return ctx, d


def print_path(finger):
    return os.path.join(PRINTS, finger + '.print')


def save(finger, p):
    os.makedirs(PRINTS, exist_ok=True)
    with open(print_path(finger), 'wb') as f:
        f.write(p.serialize())


def load(finger):
    with open(print_path(finger), 'rb') as f:
        return FPrint.Print.deserialize(f.read())


def progress(dev, stage, pnt, data, error):
    note = ' (retry: %s)' % error.message if error else ''
    print('  stage %d/%d%s' % (stage, dev.get_nr_enroll_stages(), note))


def do_enroll(d, finger):
    print('Press %s on the reader, lifting between presses.' % finger)
    template = FPrint.Print.new(d)
    template.set_finger(FINGERS[finger])
    p = d.enroll_sync(template, None, progress, None)
    save(finger, p)
    print('ENROLLED %s' % finger)


def do_cancel_enroll(d, ctx):
    cancellable = Gio.Cancellable()
    stages = []
    result = {}

    def on_progress(dev, stage, pnt, data, error):
        progress(dev, stage, pnt, data, error)
        if not error:
            stages.append(stage)

    def on_status(dev, pspec):
        if (stages and not cancellable.is_cancelled() and
                dev.get_finger_status() & FPrint.FingerStatusFlags.NEEDED):
            print('  cancelling while waiting for the second press')
            cancellable.cancel()

    def done(dev, res):
        try:
            dev.enroll_finish(res)
            result['error'] = None
        except GLib.Error as e:
            result['error'] = e

    handler = d.connect('notify::finger-status', on_status)
    print('Press right-index ONCE, then wait.')
    template = FPrint.Print.new(d)
    template.set_finger(FPrint.Finger.RIGHT_INDEX)
    d.enroll(template, cancellable=cancellable, progress_cb=on_progress,
             callback=done)
    while 'error' not in result:
        ctx.iteration(True)
    d.disconnect(handler)
    e = result['error']
    ok = e is not None and e.matches(Gio.io_error_quark(),
                                     Gio.IOErrorEnum.CANCELLED)
    print('CANCELLED' if ok else 'UNEXPECTED RESULT: %r' % e)
    print('finger status after cancel: %s' % d.get_finger_status())


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    action = sys.argv[1]
    finger = sys.argv[2] if len(sys.argv) > 2 else None
    ctx, d = open_device()
    try:
        if action == 'info':
            print('driver %s, %s' % (d.get_driver(), d.get_name()))
            print('features: %s' % d.get_features())
            print('enroll stages: %d' % d.get_nr_enroll_stages())
        elif action == 'enroll':
            do_enroll(d, finger)
        elif action == 'verify':
            print('Press a finger to check against %s.' % finger)
            ok, _ = d.verify_sync(load(finger))
            print('MATCH' if ok else 'NO MATCH')
        elif action == 'identify':
            names = sorted(f[:-6] for f in os.listdir(PRINTS) if f.endswith('.print'))
            gallery = [load(n) for n in names]
            print('Press a finger; gallery: %s' % ', '.join(names))
            match, _ = d.identify_sync(gallery)
            if match is None:
                print('NO MATCH')
            else:
                print('MATCH %s' % names[[g.equal(match) for g in gallery].index(True)])
        elif action == 'delete':
            d.delete_print_sync(load(finger))
            os.remove(print_path(finger))
            print('DELETED %s' % finger)
        elif action == 'verify-bogus':
            try:
                d.verify_sync(FPrint.Print.new(d))
                print('UNEXPECTED: verify succeeded')
            except GLib.Error as e:
                invalid = e.matches(FPrint.DeviceError.quark(),
                                    FPrint.DeviceError.DATA_INVALID)
                print('DATA_INVALID' if invalid else 'UNEXPECTED ERROR: %s' % e)
        elif action == 'cancel-enroll':
            do_cancel_enroll(d, ctx)
        else:
            sys.exit('unknown action %s' % action)
    finally:
        d.close_sync()


if __name__ == '__main__':
    main()
```

- [ ] **Step 9: Smoke test the skeleton on hardware (hardware, user)**

Run: `pkexec bash ~/projects/cv2-research/devtools/run-dev.sh info`
Expected: `driver cv2, Broadcom ControlVault2 (BCM5880) Fingerprint Reader`, `enroll stages: 4`, then `>>> fprintd restored`.

- [ ] **Step 10: Commit (libfprint, then research repo)**

```bash
cd ~/projects/libfprint
git add meson.build libfprint/meson.build libfprint/drivers/cv2 libfprint/fprint-list-udev-hwdb.c data/autosuspend.hwdb
git commit -m "cv2: Add Broadcom ControlVault2 (0a5c:5834) driver skeleton

Co-Authored-By: Claude <noreply@anthropic.com>"
scripts/uncrustify.sh && git diff --quiet || git commit -a --amend --no-edit

cd ~/projects/cv2-research
git add devtools
git commit -m "devtools: run the libfprint cv2 driver from a build tree

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: Protocol header and request builders

**Files:**
- Create: `libfprint/drivers/cv2/cv2-proto.h`, `libfprint/drivers/cv2/cv2-proto.c`
- Create: `tests/test-cv2-proto.c`
- Modify: `libfprint/meson.build` (add `cv2-proto.c` to the `cv2` sources), `tests/meson.build` (register the unit test)

**Interfaces:**
- Produces (all in `cv2-proto.h`, used by every later task):
  - constants `CV2_HEADER_LEN` (0x2c), `CV2_CAPTURE_ID_LEN` (20), `CV2_REPLY_BUF_LEN` (4196), `CV2_FW_MIN_ENROLL` (0x00412015)
  - enums `Cv2ParamKind`, `Cv2Command` (`CV2_CMD_*`), `Cv2Status` (`CV2_STATUS_*`), `Cv2Outcome` (`CV2_OUTCOME_*`), `Cv2FwSupport` (`CV2_FW_*`)
  - structs `Cv2Param { guint32 kind; guint32 len; const guint8 *data; }`, `Cv2Reply { guint16 cmd; guint16 flags; guint32 session; guint32 status; GBytes *raw; GArray *params; }`
  - builders `GByteArray *cv2_build_*` (listed in the header below), each returning a complete message owned by the caller.

- [ ] **Step 1: Write the header**

Create `libfprint/drivers/cv2/cv2-proto.h` (license block as in `cv2.h`, then):

```c
#pragma once

#include <glib.h>

#define CV2_HEADER_LEN     0x2c
#define CV2_CAPTURE_ID_LEN 20
/* Bulk IN buffer size; the stock driver uses the same */
#define CV2_REPLY_BUF_LEN  4196
/* First firmware (USH_REL_UPGRADE_VER) whose enrollment commit works */
#define CV2_FW_MIN_ENROLL  0x00412015

typedef enum {
  CV2_PARAM_VALUE  = 0,
  CV2_PARAM_STRING = 1,
  CV2_PARAM_BLOCK  = 2,
  CV2_PARAM_BUFFER = 3,
} Cv2ParamKind;

typedef enum {
  CV2_CMD_OPEN_SESSION   = 0x02,
  CV2_CMD_CLOSE_SESSION  = 0x04,
  CV2_CMD_DELETE         = 0x0a,
  CV2_CMD_MATCH          = 0x2f,
  CV2_CMD_GET_VERSION    = 0x39,
  CV2_CMD_CAPTURE_START  = 0x66,
  CV2_CMD_CAPTURE_CANCEL = 0x68,
  CV2_CMD_ENROLL_SAMPLE  = 0x6c,
  CV2_CMD_ENROLL_DISCARD = 0x6d,
  CV2_CMD_ENROLL_COMMIT  = 0x6e,
  CV2_CMD_ENROLL_BEGIN   = 0x8a,
} Cv2Command;

typedef enum {
  CV2_STATUS_OK              = 0x00,
  CV2_STATUS_FW_TOO_OLD      = 0x24,
  CV2_STATUS_NO_TEMPLATES    = 0x47,
  CV2_STATUS_SAMPLE_REJECTED = 0x59,
  CV2_STATUS_CAPTURE_PENDING = 0x85,
} Cv2Status;

typedef enum {
  CV2_OUTCOME_OK,              /* status 0 */
  CV2_OUTCOME_NO_MATCH,        /* match with no templates: treat as no match */
  CV2_OUTCOME_RETRY,           /* sample rejected; error is a retry error */
  CV2_OUTCOME_CAPTURE_PENDING, /* cancel the capture and retry once; error is BUSY */
  CV2_OUTCOME_ERROR,           /* error is set */
} Cv2Outcome;

typedef enum {
  CV2_FW_UNKNOWN,
  CV2_FW_TOO_OLD,
  CV2_FW_OK,
} Cv2FwSupport;

typedef struct
{
  guint32       kind;
  guint32       len;
  const guint8 *data;  /* points into Cv2Reply.raw */
} Cv2Param;

typedef struct
{
  guint16  cmd;
  guint16  flags;
  guint32  session;
  guint32  status;
  GBytes  *raw;     /* the message, header total length bytes */
  GArray  *params;  /* of Cv2Param */
} Cv2Reply;

/* Requests. Each returns a complete message owned by the caller. */
GByteArray *cv2_build_open_session (void);
GByteArray *cv2_build_close_session (guint32 session);
GByteArray *cv2_build_get_version (void);
GByteArray *cv2_build_enroll_begin (void);
GByteArray *cv2_build_capture_start (guint32 session);
GByteArray *cv2_build_capture_cancel (void);
GByteArray *cv2_build_enroll_sample (guint32       session,
                                     const guint8 *capture_id);
GByteArray *cv2_build_enroll_commit (guint32       session,
                                     const guint8 *result_id);
GByteArray *cv2_build_enroll_discard (void);
GByteArray *cv2_build_match (guint32        session,
                             const guint32 *templates,
                             guint          n_templates);
GByteArray *cv2_build_delete (guint32 session,
                              guint32 template_id);

/* Replies */
gboolean cv2_message_peek_cmd (const guint8 *buf,
                               gsize         len,
                               guint16      *cmd);
Cv2Reply *cv2_reply_parse (const guint8 *buf,
                           gsize         len,
                           guint16       expected_cmd,
                           GError      **error);
void cv2_reply_free (Cv2Reply *reply);
gboolean cv2_reply_get_session (const Cv2Reply *reply,
                                guint32        *session,
                                GError        **error);
gboolean cv2_reply_get_capture_id (const Cv2Reply *reply,
                                   guint8         *capture_id,
                                   GError        **error);
gboolean cv2_reply_get_sample (const Cv2Reply *reply,
                               gboolean       *done,
                               guint8         *result_id,
                               GError        **error);
gboolean cv2_reply_get_template (const Cv2Reply *reply,
                                 guint32        *template_id,
                                 GError        **error);
gboolean cv2_reply_get_match (const Cv2Reply *reply,
                              gboolean       *matched,
                              guint32        *template_id,
                              GError        **error);
gchar *cv2_reply_get_version_text (const Cv2Reply *reply,
                                   GError        **error);

/* Status codes, firmware, helpers */
Cv2Outcome cv2_status_classify (guint16  cmd,
                                guint32  status,
                                GError **error);
gchar *cv2_version_lookup (const gchar *text,
                           const gchar *key);
Cv2FwSupport cv2_firmware_enroll_support (const gchar *upgrade_ver);
GError *cv2_firmware_error (const gchar *upgrade_ver);
gint cv2_template_index (const guint32 *templates,
                         guint          n_templates,
                         guint32        template_id);
gint cv2_enroll_next_stage (gint     stage,
                            gboolean done,
                            gint     nr_stages);

G_DEFINE_AUTOPTR_CLEANUP_FUNC (Cv2Reply, cv2_reply_free)
```

- [ ] **Step 2: Register the source and the unit test in meson**

In `libfprint/meson.build`, change the `cv2` entry to:

```meson
    'cv2' : files(
        'drivers/cv2/cv2.c',
        'drivers/cv2/cv2-proto.c',
    ),
```

In `tests/meson.build`, add directly after the `foreach test_name: unit_tests … endforeach` block:

```meson
if 'cv2' in drivers
    test_cv2_proto = executable('test-cv2-proto',
        sources: [
            'test-cv2-proto.c',
            files('../libfprint/drivers/cv2/cv2-proto.c'),
        ],
        include_directories: include_directories('../libfprint/drivers/cv2'),
        dependencies: libfprint_private_dep,
        c_args: common_cflags,
        install: false,
    )
    test('cv2-proto',
        test_cv2_proto,
        suite: ['unit-tests'],
        env: envs,
    )
endif
```

- [ ] **Step 3: Write the failing builder tests**

Create `tests/test-cv2-proto.c`. The fixtures were copied from `protocol/captures/*.pcap`, `cvtool.log` and `windows-captures/03.pcap` (session handle `0x14019c8c` throughout). The first eleven request fixtures were checked byte-for-byte against the captures while writing this plan. `VERSION_REQ` is the Windows request with the plaintext flags `0x0440`; `COMMIT_REPLY` keeps the one trailing byte the reader sent.

```c
/*
 * Unit tests for the cv2 protocol helpers
 *
 * Copyright (C) 2026 Matt Trent <matt@thetrents.org>
 *
 * (LGPL block as in cv2.h)
 */

#include <string.h>

#include "fpi-device.h"
#include "cv2-proto.h"

#define SESSION 0x14019c8c

static const char OPEN_REQ[] =
  "0100000060000000020040040200000000000000000000000000000000000000"
  "0000000000000000340000000000000004000000440000000100000007000000"
  "6d7941707049440001000000080000006d795573657249440100000000000000";

static const char CLOSE_REQ[] =
  "0100000038000000040040040200000000000000000000000000000000000000"
  "00000000000000000c00000000000000040000008c9c0114";

static const char BEGIN_REQ[] =
  "01000000380000008a0040040200000000000000000000000000000000000000"
  "00000000000000000c000000000000000400000000000000";

static const char CAPTURE_START_REQ[] =
  "010000005000000066004204020000008c9c0114000000000000000000000000"
  "00000000000000002400000000000000040000008c9c01140000000004000000"
  "02000000000000000400000023000000";

static const char CANCEL_REQ[] =
  "0100000038000000680040040200000000000000000000000000000000000000"
  "00000000000000000c000000000000000400000000000000";

static const char SAMPLE_REQ[] =
  "010000005c0000006c004004020000008c9c0114000000000000000000000000"
  "00000000000000003000000000000000040000008c9c01140000000014000000"
  "ac27493d858230b6ace063b560297f594315db270200000000000000";

static const char COMMIT_REQ[] =
  "010000006c0000006e004004020000008c9c0114000000000000000000000000"
  "00000000000000004000000000000000040000008c9c01140000000014000000"
  "593f0a9481c697258513c8a921687629e44542f0020000000000000002000000"
  "000000000300000000000000";

static const char DISCARD_REQ[] =
  "01000000380000006d0040040200000000000000000000000000000000000000"
  "00000000000000000c000000000000000400000000000000";

static const char MATCH1_REQ[] =
  "01000000680000002f004204020000008c9c0114000000000000000000000000"
  "00000000000000003c00000000000000040000008c9c01140000000004000000"
  "480000000000000004000000e253000000000000040000000000000002000000"
  "0400000007a0df00";

static const char MATCH2_REQ[] =
  "010000006c0000002f004204020000008c9c0114000000000000000000000000"
  "00000000000000004000000000000000040000008c9c01140000000004000000"
  "480000000000000004000000e253000000000000040000000000000002000000"
  "080000002f6236001c835b00";

static const char DELETE_REQ[] =
  "010000004c0000000a004204020000008c9c0114000000000000000000000000"
  "00000000000000002000000000000000040000008c9c01140000000004000000"
  "2f6236000200000000000000";

static const char VERSION_REQ[] =
  "0100000038000000390040040200000000000000000000000000000000000000"
  "00000000000000000c000000000000000400000000000000";

static const char CAPTURE_ID[] = "ac27493d858230b6ace063b560297f594315db27";
static const char FINAL_ID[] = "593f0a9481c697258513c8a921687629e44542f0";

static GBytes *
hex_bytes (const char *hex)
{
  gsize len = strlen (hex) / 2;
  guint8 *buf = g_malloc (len);

  g_assert_cmpuint (strlen (hex) % 2, ==, 0);
  for (gsize i = 0; i < len; i++)
    buf[i] = (g_ascii_xdigit_value (hex[2 * i]) << 4) |
             g_ascii_xdigit_value (hex[2 * i + 1]);

  return g_bytes_new_take (buf, len);
}

static void
assert_message (GByteArray *message, const char *expected_hex)
{
  g_autoptr(GByteArray) owned = message;
  g_autoptr(GBytes) expected = hex_bytes (expected_hex);
  gsize len;
  const guint8 *data = g_bytes_get_data (expected, &len);

  g_assert_cmpmem (owned->data, owned->len, data, len);
}

static void
test_build_requests (void)
{
  g_autoptr(GBytes) capture_id = hex_bytes (CAPTURE_ID);
  g_autoptr(GBytes) final_id = hex_bytes (FINAL_ID);
  const guint32 one[] = { 0x00dfa007 };
  const guint32 two[] = { 0x0036622f, 0x005b831c };

  assert_message (cv2_build_open_session (), OPEN_REQ);
  assert_message (cv2_build_close_session (SESSION), CLOSE_REQ);
  assert_message (cv2_build_enroll_begin (), BEGIN_REQ);
  assert_message (cv2_build_capture_start (SESSION), CAPTURE_START_REQ);
  assert_message (cv2_build_capture_cancel (), CANCEL_REQ);
  assert_message (cv2_build_enroll_sample (SESSION, g_bytes_get_data (capture_id, NULL)),
                  SAMPLE_REQ);
  assert_message (cv2_build_enroll_commit (SESSION, g_bytes_get_data (final_id, NULL)),
                  COMMIT_REQ);
  assert_message (cv2_build_enroll_discard (), DISCARD_REQ);
  assert_message (cv2_build_match (SESSION, one, G_N_ELEMENTS (one)), MATCH1_REQ);
  assert_message (cv2_build_match (SESSION, two, G_N_ELEMENTS (two)), MATCH2_REQ);
  assert_message (cv2_build_delete (SESSION, 0x0036622f), DELETE_REQ);
  assert_message (cv2_build_get_version (), VERSION_REQ);
}

int
main (int argc, char *argv[])
{
  g_test_init (&argc, &argv, NULL);

  g_test_add_func ("/cv2/build/requests", test_build_requests);

  return g_test_run ();
}
```

Create an empty `libfprint/drivers/cv2/cv2-proto.c` containing only the license block and:

```c
#define FP_COMPONENT "cv2"

#include <string.h>

#include "drivers_api.h"

#include "cv2-proto.h"
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `meson compile -C build 2>&1 | tail -5`
Expected: FAIL: link errors `undefined reference to 'cv2_build_open_session'` (and the other builders) for `test-cv2-proto`.

- [ ] **Step 5: Implement the builders**

Append to `cv2-proto.c`:

```c
/* Header flags. 0x0442 marks the capture-related commands. */
#define CV2_FLAGS_DEFAULT       0x0440
#define CV2_FLAGS_CAPTURE       0x0442
#define CV2_VERSION_FLAGS       0x0440

/* Constants the stock driver sends; their meaning is unknown */
#define CV2_OPEN_ARG            0x44
#define CV2_CAPTURE_ARG         2
#define CV2_CAPTURE_MODE_SAMPLE 0x23
#define CV2_MATCH_ARG1          0x48
#define CV2_MATCH_ARG2          0x53e2

static void
put_u16 (guint8 *p, guint16 value)
{
  value = GUINT16_TO_LE (value);
  memcpy (p, &value, sizeof (value));
}

static void
put_u32 (guint8 *p, guint32 value)
{
  value = GUINT32_TO_LE (value);
  memcpy (p, &value, sizeof (value));
}

static guint16
cmd_flags (guint16 cmd)
{
  if (cmd == CV2_CMD_CAPTURE_START || cmd == CV2_CMD_MATCH || cmd == CV2_CMD_DELETE)
    return CV2_FLAGS_CAPTURE;
  if (cmd == CV2_CMD_GET_VERSION)
    return CV2_VERSION_FLAGS;
  return CV2_FLAGS_DEFAULT;
}

static GByteArray *
msg_new (guint16 cmd, guint32 session)
{
  GByteArray *msg = g_byte_array_sized_new (128);

  g_byte_array_set_size (msg, CV2_HEADER_LEN);
  memset (msg->data, 0, CV2_HEADER_LEN);
  put_u32 (msg->data + 0x00, 1);
  put_u16 (msg->data + 0x08, cmd);
  put_u16 (msg->data + 0x0a, cmd_flags (cmd));
  put_u32 (msg->data + 0x0c, 2);
  put_u32 (msg->data + 0x10, session);

  return msg;
}

/* The declared length can differ from the bytes sent: the open request
 * declares 7 for "myAppID" but sends the NUL too. */
static void
msg_add_param (GByteArray *msg,
               guint32     kind,
               guint32     declared_len,
               const void *data,
               gsize       data_len)
{
  static const guint8 zeros[4] = { 0 };
  guint8 hdr[8];

  put_u32 (hdr, kind);
  put_u32 (hdr + 4, declared_len);
  g_byte_array_append (msg, hdr, sizeof (hdr));

  if (data_len > 0)
    {
      g_byte_array_append (msg, data, data_len);
      g_byte_array_append (msg, zeros, (4 - data_len % 4) % 4);
    }
}

static void
msg_add_u32 (GByteArray *msg, guint32 value)
{
  guint8 buf[4];

  put_u32 (buf, value);
  msg_add_param (msg, CV2_PARAM_VALUE, sizeof (buf), buf, sizeof (buf));
}

static GByteArray *
msg_finish (GByteArray *msg)
{
  put_u32 (msg->data + 0x04, msg->len);
  put_u32 (msg->data + 0x28, msg->len - CV2_HEADER_LEN);
  return msg;
}

GByteArray *
cv2_build_open_session (void)
{
  GByteArray *msg = msg_new (CV2_CMD_OPEN_SESSION, 0);

  msg_add_u32 (msg, CV2_OPEN_ARG);
  msg_add_param (msg, CV2_PARAM_STRING, 7, "myAppID", 8);
  msg_add_param (msg, CV2_PARAM_STRING, 8, "myUserID", 8);
  msg_add_param (msg, CV2_PARAM_STRING, 0, NULL, 0);
  return msg_finish (msg);
}

GByteArray *
cv2_build_close_session (guint32 session)
{
  GByteArray *msg = msg_new (CV2_CMD_CLOSE_SESSION, 0);

  msg_add_u32 (msg, session);
  return msg_finish (msg);
}

GByteArray *
cv2_build_get_version (void)
{
  GByteArray *msg = msg_new (CV2_CMD_GET_VERSION, 0);

  msg_add_u32 (msg, 0);
  return msg_finish (msg);
}

GByteArray *
cv2_build_enroll_begin (void)
{
  GByteArray *msg = msg_new (CV2_CMD_ENROLL_BEGIN, 0);

  msg_add_u32 (msg, 0);
  return msg_finish (msg);
}

GByteArray *
cv2_build_capture_start (guint32 session)
{
  GByteArray *msg = msg_new (CV2_CMD_CAPTURE_START, session);

  msg_add_u32 (msg, session);
  msg_add_u32 (msg, CV2_CAPTURE_ARG);
  msg_add_u32 (msg, CV2_CAPTURE_MODE_SAMPLE);
  return msg_finish (msg);
}

GByteArray *
cv2_build_capture_cancel (void)
{
  GByteArray *msg = msg_new (CV2_CMD_CAPTURE_CANCEL, 0);

  msg_add_u32 (msg, 0);
  return msg_finish (msg);
}

GByteArray *
cv2_build_enroll_sample (guint32 session, const guint8 *capture_id)
{
  GByteArray *msg = msg_new (CV2_CMD_ENROLL_SAMPLE, session);

  msg_add_u32 (msg, session);
  msg_add_param (msg, CV2_PARAM_VALUE, CV2_CAPTURE_ID_LEN, capture_id, CV2_CAPTURE_ID_LEN);
  msg_add_param (msg, CV2_PARAM_BLOCK, 0, NULL, 0);
  return msg_finish (msg);
}

GByteArray *
cv2_build_enroll_commit (guint32 session, const guint8 *result_id)
{
  GByteArray *msg = msg_new (CV2_CMD_ENROLL_COMMIT, session);

  msg_add_u32 (msg, session);
  msg_add_param (msg, CV2_PARAM_VALUE, CV2_CAPTURE_ID_LEN, result_id, CV2_CAPTURE_ID_LEN);
  msg_add_param (msg, CV2_PARAM_BLOCK, 0, NULL, 0);
  msg_add_param (msg, CV2_PARAM_BLOCK, 0, NULL, 0);
  msg_add_param (msg, CV2_PARAM_BUFFER, 0, NULL, 0);
  return msg_finish (msg);
}

GByteArray *
cv2_build_enroll_discard (void)
{
  GByteArray *msg = msg_new (CV2_CMD_ENROLL_DISCARD, 0);

  msg_add_u32 (msg, 0);
  return msg_finish (msg);
}

GByteArray *
cv2_build_match (guint32 session, const guint32 *templates, guint n_templates)
{
  GByteArray *msg = msg_new (CV2_CMD_MATCH, session);
  g_autofree guint8 *list = g_malloc0 (MAX (n_templates, 1) * 4);

  for (guint i = 0; i < n_templates; i++)
    put_u32 (list + 4 * i, templates[i]);

  msg_add_u32 (msg, session);
  msg_add_u32 (msg, CV2_MATCH_ARG1);
  msg_add_u32 (msg, CV2_MATCH_ARG2);
  msg_add_u32 (msg, 0);
  msg_add_param (msg, CV2_PARAM_BLOCK, n_templates * 4, list, n_templates * 4);
  return msg_finish (msg);
}

GByteArray *
cv2_build_delete (guint32 session, guint32 template_id)
{
  GByteArray *msg = msg_new (CV2_CMD_DELETE, session);

  msg_add_u32 (msg, session);
  msg_add_u32 (msg, template_id);
  msg_add_param (msg, CV2_PARAM_BLOCK, 0, NULL, 0);
  return msg_finish (msg);
}
```

If Task 1's outcome was **B**, set `CV2_VERSION_FLAGS` to `0x0040` and patch `VERSION_REQ` as Task 1 Step 3 describes.

- [ ] **Step 6: Run the test to verify it passes**

Run: `meson compile -C build && meson test -C build cv2-proto --print-errorlogs`
Expected: `cv2-proto OK`.

- [ ] **Step 7: Commit**

```bash
cd ~/projects/libfprint
git add libfprint/drivers/cv2/cv2-proto.h libfprint/drivers/cv2/cv2-proto.c libfprint/meson.build tests/meson.build tests/test-cv2-proto.c
git commit -m "cv2: Add protocol request builders

Co-Authored-By: Claude <noreply@anthropic.com>"
scripts/uncrustify.sh && git diff --quiet || git commit -a --amend --no-edit
```

---

### Task 4: Reply parser and typed getters

**Files:**
- Modify: `libfprint/drivers/cv2/cv2-proto.c`
- Modify: `tests/test-cv2-proto.c`

**Interfaces:**
- Consumes: `Cv2Reply`, `Cv2Param`, declarations from Task 3's header.
- Produces: `cv2_message_peek_cmd`, `cv2_reply_parse`, `cv2_reply_free`, `cv2_reply_get_session`, `cv2_reply_get_capture_id`, `cv2_reply_get_sample`, `cv2_reply_get_template`, `cv2_reply_get_match`, `cv2_reply_get_version_text`. All errors are `FP_DEVICE_ERROR_PROTO`. The parser does **not** fail on a nonzero status (failed replies are header-only); callers classify the status first and only call getters for status 0.

- [ ] **Step 1: Write the failing parser tests**

Add these fixtures to `tests/test-cv2-proto.c` after `FINAL_ID`:

```c
static const char OPEN_REPLY[] =
  "010000003800000002004005020000008c9c0114000000000000000000000000"
  "00000000000000000c00000000000000040000008c9c0114";

static const char CAPTURE_START_REPLY[] =
  "010000004800000066004205020000008c9c0114000000000000000000000000"
  "00000000000000001c0000000000000014000000ac27493d858230b6ace063b5"
  "60297f594315db27";

static const char CAPTURE_PENDING_REPLY[] =
  "010000002c00000066004205020000008c9c0114850000000000000000000000"
  "000000000000000000000000";

static const char SAMPLE_REPLY[] =
  "01000000600000006c004005020000008c9c0114000000000000000000000000"
  "0000000000000000340000000000000004000000009c01140000000014000000"
  "ac27493d858230b6ace063b560297f594315db27000000000400000000000000";

static const char SAMPLE_DONE_REPLY[] =
  "01000000600000006c004005020000008c9c0114000000000000000000000000"
  "0000000000000000340000000000000004000000019c01140000000014000000"
  "593f0a9481c697258513c8a921687629e44542f0000000000400000000000000";

static const char SAMPLE_REJECTED_REPLY[] =
  "010000002c0000006c00400502000000889c0114590000000000000000000000"
  "000000000000000000000000";

static const char COMMIT_REPLY[] =
  "01000000400000006e004005020000008c9c0114000000000000000000000000"
  "00000000000000001400000003000000000000000000000004000000805a7800"
  "00";

static const char MATCH_HIT_REPLY[] =
  "01000000440000002f004205020000008c9c0114000000000000000000000000"
  "0000000000000000180000000000000004000000010000000000000004000000"
  "07a0df00";

static const char MATCH_MISS_REPLY[] =
  "01000000440000002f004205020000008c9c0114000000000000000000000000"
  "0000000000000000180000000000000004000000000000000000000004000000"
  "00000000";

static const char MATCH_EMPTY_REPLY[] =
  "010000002c0000002f004205020000008c9c0114470000000000000000000000"
  "000000000000000000000000";

static const char CANCEL_REPLY[] =
  "010000002c000000680040050200000000000000000000000000000000000000"
  "000000000000000000000000";

static const char VERSION_REPLY[] =
  "0100000050030000390040010200000000000000000000000000000000000000"
  "000000000000000024030000010000001c0300005553485f4348495049443a30"
  "353831303231310a5553485f434849505f5355425f5645523a30303030303538"
  "310a5553485f52454c5f5645523a30313032303030300a5553485f52454c5f55"
  "5047524144455f5645523a30303431323031350a5553485f52454c5f4255494c"
  "445f5645523a30303030303030300a5553485f424f4f545f434d443a35383830"
  "303130300a5553485f5245424f4f545f434e543a30303030303030300a555348"
  "5f5342495f4d4f44453a30303030303030300a5553485f464c534857525f434e"
  "543a30303030303030300a5553485f4f54505f53544154533a30303030303030"
  "310a5553485f435553545f49443a36303030303030310a5553485f435553545f"
  "5245563a30303030303030300a5553485f5343445f524f4d5f5645523a303130"
  "31303630300a5553485f43565f5645523a30313030303233350a5553485f4343"
  "49445f5645523a30303033303130310a5553485f4849445f5645523a30303030"
  "303032630a5553485f44505f5645525f55505045523a30303035303030300a55"
  "53485f44505f5645525f4c4f5745523a30303033303365300a5553485f525341"
  "5f5645523a30303030303130310a5553485f49505f52455649443a3030303030"
  "3030300a5553485f5057525f49444c455f54494d453a30303034393365300a55"
  "53485f5057525f4d4f44453a30303030303030330a5553485f555342445f5049"
  "443a30303030353833340a5553485f5359535f49443a30303030303030320a55"
  "53485f46505f4e4558545f53454e534f523a30303031306130310a5553485f46"
  "505f44505f53454e534f523a30303030303030300a5553485f46505f53594e41"
  "50544943535f53454e534f523a30303030303030300a5553485f4145535f4b45"
  "595f56414c49443a30303030303030310a5553485f424f4152445f49443a6263"
  "3030303030300a5553485f494d475f535441543a30303030303030300a555348"
  "5f494d475f464c4147533a30303030303037650a5553485f4355525f5449434b"
  "3a30303034323430320a5553485f4255494c445f54494d453a41756720313820"
  "323032352031373a35343a34300a0000";
```

Add these helpers and tests before `main`:

```c
static GByteArray *
hex_array (const char *hex)
{
  g_autoptr(GBytes) bytes = hex_bytes (hex);
  gsize len;
  const guint8 *data = g_bytes_get_data (bytes, &len);
  GByteArray *array = g_byte_array_sized_new (len);

  g_byte_array_append (array, data, len);
  return array;
}

static Cv2Reply *
parse_hex (const char *hex, guint16 cmd, GError **error)
{
  g_autoptr(GBytes) bytes = hex_bytes (hex);
  gsize len;
  const guint8 *data = g_bytes_get_data (bytes, &len);

  return cv2_reply_parse (data, len, cmd, error);
}

static Cv2Reply *
parse_array (GByteArray *array, guint16 cmd, GError **error)
{
  return cv2_reply_parse (array->data, array->len, cmd, error);
}

static void
assert_id (const guint8 *id, const char *expected_hex)
{
  g_autoptr(GBytes) expected = hex_bytes (expected_hex);
  gsize len;
  const guint8 *data = g_bytes_get_data (expected, &len);

  g_assert_cmpmem (id, CV2_CAPTURE_ID_LEN, data, len);
}

static void
test_parse_open (void)
{
  g_autoptr(GError) error = NULL;
  g_autoptr(Cv2Reply) reply = parse_hex (OPEN_REPLY, CV2_CMD_OPEN_SESSION, &error);
  guint32 session = 0;

  g_assert_no_error (error);
  g_assert_cmpuint (reply->cmd, ==, CV2_CMD_OPEN_SESSION);
  g_assert_cmpuint (reply->flags, ==, 0x0540);
  g_assert_cmpuint (reply->status, ==, 0);
  g_assert_cmpuint (reply->session, ==, SESSION);
  g_assert_cmpuint (reply->params->len, ==, 1);
  g_assert_true (cv2_reply_get_session (reply, &session, &error));
  g_assert_no_error (error);
  g_assert_cmpuint (session, ==, SESSION);
}

static void
test_parse_capture_and_samples (void)
{
  g_autoptr(GError) error = NULL;
  g_autoptr(Cv2Reply) start = parse_hex (CAPTURE_START_REPLY, CV2_CMD_CAPTURE_START, &error);
  g_autoptr(Cv2Reply) sample = NULL;
  g_autoptr(Cv2Reply) done_reply = NULL;
  guint8 id[CV2_CAPTURE_ID_LEN];
  gboolean done = TRUE;

  g_assert_no_error (error);
  g_assert_true (cv2_reply_get_capture_id (start, id, &error));
  assert_id (id, CAPTURE_ID);

  sample = parse_hex (SAMPLE_REPLY, CV2_CMD_ENROLL_SAMPLE, &error);
  g_assert_no_error (error);
  g_assert_true (cv2_reply_get_sample (sample, &done, id, &error));
  g_assert_false (done);
  assert_id (id, CAPTURE_ID);

  done_reply = parse_hex (SAMPLE_DONE_REPLY, CV2_CMD_ENROLL_SAMPLE, &error);
  g_assert_no_error (error);
  g_assert_true (cv2_reply_get_sample (done_reply, &done, id, &error));
  g_assert_true (done);
  assert_id (id, FINAL_ID);
}

static void
test_parse_commit_with_trailing_byte (void)
{
  g_autoptr(GError) error = NULL;
  g_autoptr(Cv2Reply) reply = parse_hex (COMMIT_REPLY, CV2_CMD_ENROLL_COMMIT, &error);
  guint32 template_id = 0;

  g_assert_no_error (error);
  g_assert_cmpuint (g_bytes_get_size (reply->raw), ==, 0x40);
  g_assert_true (cv2_reply_get_template (reply, &template_id, &error));
  g_assert_cmphex (template_id, ==, 0x00785a80);
}

static void
test_parse_match (void)
{
  g_autoptr(GError) error = NULL;
  g_autoptr(Cv2Reply) hit = parse_hex (MATCH_HIT_REPLY, CV2_CMD_MATCH, &error);
  g_autoptr(Cv2Reply) miss = NULL;
  gboolean matched = FALSE;
  guint32 template_id = 0;

  g_assert_no_error (error);
  g_assert_true (cv2_reply_get_match (hit, &matched, &template_id, &error));
  g_assert_true (matched);
  g_assert_cmphex (template_id, ==, 0x00dfa007);

  miss = parse_hex (MATCH_MISS_REPLY, CV2_CMD_MATCH, &error);
  g_assert_no_error (error);
  g_assert_true (cv2_reply_get_match (miss, &matched, &template_id, &error));
  g_assert_false (matched);
  g_assert_cmphex (template_id, ==, 0);
}

static void
test_parse_header_only (void)
{
  struct
  {
    const char *hex;
    guint16     cmd;
    guint32     status;
  } cases[] = {
    { SAMPLE_REJECTED_REPLY, CV2_CMD_ENROLL_SAMPLE, CV2_STATUS_SAMPLE_REJECTED },
    { CAPTURE_PENDING_REPLY, CV2_CMD_CAPTURE_START, CV2_STATUS_CAPTURE_PENDING },
    { MATCH_EMPTY_REPLY, CV2_CMD_MATCH, CV2_STATUS_NO_TEMPLATES },
    { CANCEL_REPLY, CV2_CMD_CAPTURE_CANCEL, CV2_STATUS_OK },
  };

  for (guint i = 0; i < G_N_ELEMENTS (cases); i++)
    {
      g_autoptr(GError) error = NULL;
      g_autoptr(Cv2Reply) reply = parse_hex (cases[i].hex, cases[i].cmd, &error);

      g_assert_no_error (error);
      g_assert_cmphex (reply->status, ==, cases[i].status);
      g_assert_cmpuint (reply->params->len, ==, 0);
    }
}

static void
test_parse_version (void)
{
  g_autoptr(GError) error = NULL;
  g_autoptr(Cv2Reply) reply = parse_hex (VERSION_REPLY, CV2_CMD_GET_VERSION, &error);
  g_autofree gchar *text = NULL;

  g_assert_no_error (error);
  text = cv2_reply_get_version_text (reply, &error);
  g_assert_no_error (error);
  g_assert_true (g_str_has_prefix (text, "USH_CHIPID:05810211\n"));
  g_assert_nonnull (strstr (text, "\nUSH_REL_UPGRADE_VER:00412015\n"));
  g_assert_true (g_str_has_suffix (text, "USH_BUILD_TIME:Aug 18 2025 17:54:40\n"));
}

static void
assert_proto_error (GError *error)
{
  g_assert_error (error, FP_DEVICE_ERROR, FP_DEVICE_ERROR_PROTO);
}

static void
test_parse_malformed (void)
{
  g_autoptr(GByteArray) msg = NULL;
  g_autoptr(GError) error = NULL;

  /* Truncated header */
  msg = hex_array (OPEN_REPLY);
  g_byte_array_set_size (msg, 0x20);
  g_assert_null (parse_array (msg, CV2_CMD_OPEN_SESSION, &error));
  assert_proto_error (error);
  g_clear_error (&error);
  g_clear_pointer (&msg, g_byte_array_unref);

  /* Header length larger than the bytes read */
  msg = hex_array (OPEN_REPLY);
  g_byte_array_set_size (msg, msg->len - 4);
  g_assert_null (parse_array (msg, CV2_CMD_OPEN_SESSION, &error));
  assert_proto_error (error);
  g_clear_error (&error);
  g_clear_pointer (&msg, g_byte_array_unref);

  /* Bad magic */
  msg = hex_array (OPEN_REPLY);
  msg->data[0] = 2;
  g_assert_null (parse_array (msg, CV2_CMD_OPEN_SESSION, &error));
  assert_proto_error (error);
  g_clear_error (&error);
  g_clear_pointer (&msg, g_byte_array_unref);

  /* Reply for another command */
  g_assert_null (parse_hex (OPEN_REPLY, CV2_CMD_CLOSE_SESSION, &error));
  assert_proto_error (error);
  g_clear_error (&error);

  /* Parameter area length disagrees with the message length */
  msg = hex_array (OPEN_REPLY);
  msg->data[0x28] = 0x10;
  g_assert_null (parse_array (msg, CV2_CMD_OPEN_SESSION, &error));
  assert_proto_error (error);
  g_clear_error (&error);
  g_clear_pointer (&msg, g_byte_array_unref);

  /* Parameter overruns the message */
  msg = hex_array (OPEN_REPLY);
  msg->data[0x30] = 0x40;
  g_assert_null (parse_array (msg, CV2_CMD_OPEN_SESSION, &error));
  assert_proto_error (error);
  g_clear_error (&error);
  g_clear_pointer (&msg, g_byte_array_unref);

  /* Truncated parameter header: 4 bytes of parameter area */
  msg = hex_array (CANCEL_REPLY);
  g_byte_array_set_size (msg, msg->len + 4);
  memset (msg->data + CV2_HEADER_LEN, 0, 4);
  msg->data[0x04] = 0x30;
  msg->data[0x28] = 0x04;
  g_assert_null (parse_array (msg, CV2_CMD_CAPTURE_CANCEL, &error));
  assert_proto_error (error);
  g_clear_error (&error);
  g_clear_pointer (&msg, g_byte_array_unref);
}

static void
test_getters_reject_wrong_shape (void)
{
  g_autoptr(GError) error = NULL;
  g_autoptr(Cv2Reply) start = parse_hex (CAPTURE_START_REPLY, CV2_CMD_CAPTURE_START, NULL);
  g_autoptr(Cv2Reply) open = parse_hex (OPEN_REPLY, CV2_CMD_OPEN_SESSION, NULL);
  g_autoptr(Cv2Reply) hit = NULL;
  g_autoptr(GByteArray) msg = NULL;
  gboolean matched;
  guint32 value;
  gchar *text;

  /* 0:20 where 0:4 is expected */
  g_assert_false (cv2_reply_get_session (start, &value, &error));
  assert_proto_error (error);
  g_clear_error (&error);

  /* Missing parameter */
  g_assert_false (cv2_reply_get_template (open, &value, &error));
  assert_proto_error (error);
  g_clear_error (&error);

  /* Not a string parameter */
  text = cv2_reply_get_version_text (open, &error);
  g_assert_null (text);
  assert_proto_error (error);
  g_clear_error (&error);

  /* Match flag other than 0 or 1 */
  msg = hex_array (MATCH_HIT_REPLY);
  msg->data[0x34] = 2;
  hit = parse_array (msg, CV2_CMD_MATCH, NULL);
  g_assert_false (cv2_reply_get_match (hit, &matched, &value, &error));
  assert_proto_error (error);
}

static void
test_peek_cmd (void)
{
  g_autoptr(GBytes) open = hex_bytes (OPEN_REPLY);
  gsize len;
  const guint8 *data = g_bytes_get_data (open, &len);
  guint16 cmd = 0;

  g_assert_true (cv2_message_peek_cmd (data, len, &cmd));
  g_assert_cmpuint (cmd, ==, CV2_CMD_OPEN_SESSION);
  g_assert_false (cv2_message_peek_cmd (data, 10, &cmd));
}
```

Register them in `main` after the builder test:

```c
  g_test_add_func ("/cv2/parse/open", test_parse_open);
  g_test_add_func ("/cv2/parse/capture-and-samples", test_parse_capture_and_samples);
  g_test_add_func ("/cv2/parse/commit-trailing-byte", test_parse_commit_with_trailing_byte);
  g_test_add_func ("/cv2/parse/match", test_parse_match);
  g_test_add_func ("/cv2/parse/header-only", test_parse_header_only);
  g_test_add_func ("/cv2/parse/version", test_parse_version);
  g_test_add_func ("/cv2/parse/malformed", test_parse_malformed);
  g_test_add_func ("/cv2/parse/wrong-shape", test_getters_reject_wrong_shape);
  g_test_add_func ("/cv2/parse/peek-cmd", test_peek_cmd);
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `meson compile -C build 2>&1 | tail -5`
Expected: FAIL: `undefined reference to 'cv2_reply_parse'` and the getters.

- [ ] **Step 3: Implement the parser and getters**

Append to `cv2-proto.c`:

```c
#define set_proto_error(error, ...) \
  g_propagate_error ((error), fpi_device_error_new_msg (FP_DEVICE_ERROR_PROTO, __VA_ARGS__))

static guint16
get_u16 (const guint8 *p)
{
  guint16 value;

  memcpy (&value, p, sizeof (value));
  return GUINT16_FROM_LE (value);
}

static guint32
get_u32 (const guint8 *p)
{
  guint32 value;

  memcpy (&value, p, sizeof (value));
  return GUINT32_FROM_LE (value);
}

gboolean
cv2_message_peek_cmd (const guint8 *buf, gsize len, guint16 *cmd)
{
  if (len < CV2_HEADER_LEN)
    return FALSE;

  *cmd = get_u16 (buf + 0x08);
  return TRUE;
}

void
cv2_reply_free (Cv2Reply *reply)
{
  if (!reply)
    return;

  g_clear_pointer (&reply->raw, g_bytes_unref);
  g_clear_pointer (&reply->params, g_array_unref);
  g_free (reply);
}

/* Bytes after the header's total length are ignored: the reader sometimes
 * sends one more byte than the message holds. */
Cv2Reply *
cv2_reply_parse (const guint8 *buf,
                 gsize         len,
                 guint16       expected_cmd,
                 GError      **error)
{
  g_autoptr(Cv2Reply) reply = NULL;
  const guint8 *data;
  guint32 total;
  guint32 params_len;
  gsize off;

  if (len < CV2_HEADER_LEN)
    {
      set_proto_error (error, "Reply too short (%" G_GSIZE_FORMAT " bytes)", len);
      return NULL;
    }

  total = get_u32 (buf + 0x04);
  if (get_u32 (buf) != 1 || total < CV2_HEADER_LEN || total > len)
    {
      set_proto_error (error, "Bad reply header (length %u, %" G_GSIZE_FORMAT " bytes read)",
                       total, len);
      return NULL;
    }

  reply = g_new0 (Cv2Reply, 1);
  reply->raw = g_bytes_new (buf, total);
  reply->params = g_array_new (FALSE, FALSE, sizeof (Cv2Param));
  data = g_bytes_get_data (reply->raw, NULL);
  reply->cmd = get_u16 (data + 0x08);
  reply->flags = get_u16 (data + 0x0a);
  reply->session = get_u32 (data + 0x10);
  reply->status = get_u32 (data + 0x14);

  if (reply->cmd != expected_cmd)
    {
      set_proto_error (error, "Reply is for command 0x%02x, expected 0x%02x",
                       reply->cmd, expected_cmd);
      return NULL;
    }

  params_len = get_u32 (data + 0x28);
  if (params_len != total - CV2_HEADER_LEN)
    {
      set_proto_error (error, "Parameter area length %u does not match message length %u",
                       params_len, total);
      return NULL;
    }

  off = CV2_HEADER_LEN;
  while (off < total)
    {
      Cv2Param param;
      gsize padded;

      if (total - off < 8)
        {
          set_proto_error (error, "Truncated parameter header in reply to 0x%02x", reply->cmd);
          return NULL;
        }

      param.kind = get_u32 (data + off);
      param.len = get_u32 (data + off + 4);
      off += 8;

      padded = ((gsize) param.len + 3) & ~(gsize) 3;
      if (padded > total - off)
        {
          set_proto_error (error, "Parameter %u overruns the reply to 0x%02x",
                           reply->params->len, reply->cmd);
          return NULL;
        }

      param.data = data + off;
      off += padded;
      g_array_append_val (reply->params, param);
    }

  return g_steal_pointer (&reply);
}

static const Cv2Param *
get_param (const Cv2Reply *reply,
           guint           index,
           guint32         kind,
           guint32         len,
           GError        **error)
{
  const Cv2Param *param;

  if (index >= reply->params->len)
    {
      set_proto_error (error, "Reply to 0x%02x has no parameter %u", reply->cmd, index);
      return NULL;
    }

  param = &g_array_index (reply->params, Cv2Param, index);
  if (param->kind != kind || param->len != len)
    {
      set_proto_error (error, "Reply to 0x%02x: parameter %u is %u:%u, expected %u:%u",
                       reply->cmd, index, param->kind, param->len, kind, len);
      return NULL;
    }

  return param;
}

gboolean
cv2_reply_get_session (const Cv2Reply *reply, guint32 *session, GError **error)
{
  const Cv2Param *param = get_param (reply, 0, CV2_PARAM_VALUE, 4, error);

  if (!param)
    return FALSE;

  *session = get_u32 (param->data);
  return TRUE;
}

gboolean
cv2_reply_get_capture_id (const Cv2Reply *reply, guint8 *capture_id, GError **error)
{
  const Cv2Param *param = get_param (reply, 0, CV2_PARAM_VALUE, CV2_CAPTURE_ID_LEN, error);

  if (!param)
    return FALSE;

  memcpy (capture_id, param->data, CV2_CAPTURE_ID_LEN);
  return TRUE;
}

/* The done flag is the low byte; the upper bytes carry unrelated data. */
gboolean
cv2_reply_get_sample (const Cv2Reply *reply,
                      gboolean       *done,
                      guint8         *result_id,
                      GError        **error)
{
  const Cv2Param *flag = get_param (reply, 0, CV2_PARAM_VALUE, 4, error);
  const Cv2Param *id;

  if (!flag)
    return FALSE;

  id = get_param (reply, 1, CV2_PARAM_VALUE, CV2_CAPTURE_ID_LEN, error);
  if (!id)
    return FALSE;

  *done = (get_u32 (flag->data) & 0xff) == 1;
  memcpy (result_id, id->data, CV2_CAPTURE_ID_LEN);
  return TRUE;
}

/* Parameter 0 is an empty buffer (3:0); the handle is parameter 1. */
gboolean
cv2_reply_get_template (const Cv2Reply *reply, guint32 *template_id, GError **error)
{
  const Cv2Param *param = get_param (reply, 1, CV2_PARAM_VALUE, 4, error);

  if (!param)
    return FALSE;

  *template_id = get_u32 (param->data);
  return TRUE;
}

gboolean
cv2_reply_get_match (const Cv2Reply *reply,
                     gboolean       *matched,
                     guint32        *template_id,
                     GError        **error)
{
  const Cv2Param *flag = get_param (reply, 0, CV2_PARAM_VALUE, 4, error);
  const Cv2Param *handle;
  guint32 value;

  if (!flag)
    return FALSE;

  handle = get_param (reply, 1, CV2_PARAM_VALUE, 4, error);
  if (!handle)
    return FALSE;

  value = get_u32 (flag->data);
  if (value > 1)
    {
      set_proto_error (error, "Unexpected match result %u", value);
      return FALSE;
    }

  *matched = value == 1;
  *template_id = get_u32 (handle->data);
  return TRUE;
}

gchar *
cv2_reply_get_version_text (const Cv2Reply *reply, GError **error)
{
  const Cv2Param *param;

  if (reply->params->len < 1 ||
      g_array_index (reply->params, Cv2Param, 0).kind != CV2_PARAM_STRING)
    {
      set_proto_error (error, "Version reply has no string parameter");
      return NULL;
    }

  param = &g_array_index (reply->params, Cv2Param, 0);
  return g_strndup ((const gchar *) param->data, param->len);
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `meson compile -C build && meson test -C build cv2-proto --print-errorlogs`
Expected: `cv2-proto OK`.

- [ ] **Step 5: Commit**

```bash
cd ~/projects/libfprint
git add libfprint/drivers/cv2/cv2-proto.c tests/test-cv2-proto.c
git commit -m "cv2: Add reply parser

Co-Authored-By: Claude <noreply@anthropic.com>"
scripts/uncrustify.sh && git diff --quiet || git commit -a --amend --no-edit
```

---

### Task 5: Status mapping, firmware gate and helpers

**Files:**
- Modify: `libfprint/drivers/cv2/cv2-proto.c`
- Modify: `tests/test-cv2-proto.c`

**Interfaces:**
- Produces: `cv2_status_classify`, `cv2_version_lookup`, `cv2_firmware_enroll_support`, `cv2_firmware_error`, `cv2_template_index` (index or -1), `cv2_enroll_next_stage`.

- [ ] **Step 1: Write the failing tests**

Add before `main`:

```c
static void
test_status_classify (void)
{
  struct
  {
    guint16    cmd;
    guint32    status;
    Cv2Outcome outcome;
    GQuark     domain;   /* 0: no error expected */
    gint       code;
  } cases[] = {
    { CV2_CMD_OPEN_SESSION, 0x00, CV2_OUTCOME_OK, 0, 0 },
    { CV2_CMD_ENROLL_SAMPLE, 0x59, CV2_OUTCOME_RETRY, 0, FP_DEVICE_RETRY_GENERAL },
    { CV2_CMD_CAPTURE_START, 0x85, CV2_OUTCOME_CAPTURE_PENDING, 0, FP_DEVICE_ERROR_BUSY },
    { CV2_CMD_MATCH, 0x47, CV2_OUTCOME_NO_MATCH, 0, 0 },
    { CV2_CMD_ENROLL_COMMIT, 0x24, CV2_OUTCOME_ERROR, 0, FP_DEVICE_ERROR_NOT_SUPPORTED },
    { CV2_CMD_DELETE, 0x13, CV2_OUTCOME_ERROR, 0, FP_DEVICE_ERROR_GENERAL },
    { CV2_CMD_ENROLL_COMMIT, 0x8d, CV2_OUTCOME_ERROR, 0, FP_DEVICE_ERROR_PROTO },
    { CV2_CMD_ENROLL_SAMPLE, 0x85, CV2_OUTCOME_ERROR, 0, FP_DEVICE_ERROR_PROTO },
    { CV2_CMD_CAPTURE_START, 0x47, CV2_OUTCOME_ERROR, 0, FP_DEVICE_ERROR_PROTO },
  };

  /* Quarks aren't constant expressions; fill them in here */
  cases[1].domain = FP_DEVICE_RETRY;
  for (guint i = 2; i < G_N_ELEMENTS (cases); i++)
    if (cases[i].outcome != CV2_OUTCOME_NO_MATCH)
      cases[i].domain = FP_DEVICE_ERROR;

  for (guint i = 0; i < G_N_ELEMENTS (cases); i++)
    {
      g_autoptr(GError) error = NULL;
      Cv2Outcome outcome = cv2_status_classify (cases[i].cmd, cases[i].status, &error);

      g_assert_cmpint (outcome, ==, cases[i].outcome);
      if (cases[i].domain)
        g_assert_error (error, cases[i].domain, cases[i].code);
      else
        g_assert_no_error (error);
    }
}

static void
test_version_lookup (void)
{
  g_autoptr(GError) error = NULL;
  g_autoptr(Cv2Reply) reply = parse_hex (VERSION_REPLY, CV2_CMD_GET_VERSION, &error);
  g_autofree gchar *text = cv2_reply_get_version_text (reply, &error);
  g_autofree gchar *upgrade = cv2_version_lookup (text, "USH_REL_UPGRADE_VER");
  g_autofree gchar *chip = cv2_version_lookup (text, "USH_CHIPID");
  g_autofree gchar *build = cv2_version_lookup (text, "USH_BUILD_TIME");
  g_autofree gchar *missing = cv2_version_lookup (text, "USH_NOPE");
  g_autofree gchar *prefix = cv2_version_lookup (text, "USH_REL");
  g_autofree gchar *reordered = cv2_version_lookup ("B:2\nUSH_REL_UPGRADE_VER:00412001\nA:1",
                                                    "USH_REL_UPGRADE_VER");
  g_autofree gchar *crlf = cv2_version_lookup ("USH_REL_UPGRADE_VER:00412015\r\n",
                                               "USH_REL_UPGRADE_VER");
  g_autofree gchar *garbage = cv2_version_lookup ("\x01\x02\x03", "USH_REL_UPGRADE_VER");

  g_assert_cmpstr (upgrade, ==, "00412015");
  g_assert_cmpstr (chip, ==, "05810211");
  g_assert_cmpstr (build, ==, "Aug 18 2025 17:54:40");
  g_assert_null (missing);
  g_assert_null (prefix);
  g_assert_cmpstr (reordered, ==, "00412001");
  g_assert_cmpstr (crlf, ==, "00412015");
  g_assert_null (garbage);
}

static void
test_firmware_gate (void)
{
  g_autoptr(GError) old = cv2_firmware_error ("00412001");
  g_autoptr(GError) unknown = cv2_firmware_error (NULL);

  g_assert_cmpint (cv2_firmware_enroll_support ("00412001"), ==, CV2_FW_TOO_OLD);
  g_assert_cmpint (cv2_firmware_enroll_support ("00412015"), ==, CV2_FW_OK);
  g_assert_cmpint (cv2_firmware_enroll_support ("00412100"), ==, CV2_FW_OK);
  g_assert_cmpint (cv2_firmware_enroll_support (NULL), ==, CV2_FW_UNKNOWN);
  g_assert_cmpint (cv2_firmware_enroll_support (""), ==, CV2_FW_UNKNOWN);
  g_assert_cmpint (cv2_firmware_enroll_support ("0041201"), ==, CV2_FW_UNKNOWN);
  g_assert_cmpint (cv2_firmware_enroll_support ("zz412015"), ==, CV2_FW_UNKNOWN);

  g_assert_error (old, FP_DEVICE_ERROR, FP_DEVICE_ERROR_NOT_SUPPORTED);
  g_assert_nonnull (strstr (old->message, "00412001"));
  g_assert_nonnull (strstr (old->message, "Dell"));
  g_assert_error (unknown, FP_DEVICE_ERROR, FP_DEVICE_ERROR_NOT_SUPPORTED);
}

static void
test_template_index (void)
{
  const guint32 templates[] = { 0x0036622f, 0x005b831c };

  g_assert_cmpint (cv2_template_index (templates, 2, 0x005b831c), ==, 1);
  g_assert_cmpint (cv2_template_index (templates, 2, 0x0036622f), ==, 0);
  g_assert_cmpint (cv2_template_index (templates, 2, 0x00dfa007), ==, -1);
  g_assert_cmpint (cv2_template_index (templates, 0, 0x0036622f), ==, -1);
}

static void
test_enroll_next_stage (void)
{
  /* Firmware 00412015 needs 4 samples; older firmware needed 13. */
  g_assert_cmpint (cv2_enroll_next_stage (0, FALSE, 4), ==, 1);
  g_assert_cmpint (cv2_enroll_next_stage (2, FALSE, 4), ==, 3);
  g_assert_cmpint (cv2_enroll_next_stage (3, FALSE, 4), ==, 3);
  g_assert_cmpint (cv2_enroll_next_stage (3, TRUE, 4), ==, 4);
  g_assert_cmpint (cv2_enroll_next_stage (1, TRUE, 4), ==, 4);
}
```

Register in `main`:

```c
  g_test_add_func ("/cv2/status/classify", test_status_classify);
  g_test_add_func ("/cv2/firmware/version-lookup", test_version_lookup);
  g_test_add_func ("/cv2/firmware/gate", test_firmware_gate);
  g_test_add_func ("/cv2/helpers/template-index", test_template_index);
  g_test_add_func ("/cv2/helpers/enroll-next-stage", test_enroll_next_stage);
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `meson compile -C build 2>&1 | tail -5`
Expected: FAIL: `undefined reference to 'cv2_status_classify'` and the others.

- [ ] **Step 3: Implement**

Append to `cv2-proto.c`:

```c
Cv2Outcome
cv2_status_classify (guint16 cmd, guint32 status, GError **error)
{
  if (status == CV2_STATUS_OK)
    return CV2_OUTCOME_OK;

  if (cmd == CV2_CMD_ENROLL_SAMPLE && status == CV2_STATUS_SAMPLE_REJECTED)
    {
      g_propagate_error (error, fpi_device_retry_new (FP_DEVICE_RETRY_GENERAL));
      return CV2_OUTCOME_RETRY;
    }

  if (cmd == CV2_CMD_CAPTURE_START && status == CV2_STATUS_CAPTURE_PENDING)
    {
      g_propagate_error (error,
                         fpi_device_error_new_msg (FP_DEVICE_ERROR_BUSY,
                                                   "A capture is still pending on the reader"));
      return CV2_OUTCOME_CAPTURE_PENDING;
    }

  if (cmd == CV2_CMD_MATCH && status == CV2_STATUS_NO_TEMPLATES)
    return CV2_OUTCOME_NO_MATCH;

  if (cmd == CV2_CMD_ENROLL_COMMIT && status == CV2_STATUS_FW_TOO_OLD)
    {
      g_propagate_error (error, cv2_firmware_error (NULL));
      return CV2_OUTCOME_ERROR;
    }

  if (cmd == CV2_CMD_DELETE)
    {
      g_propagate_error (error,
                         fpi_device_error_new_msg (FP_DEVICE_ERROR_GENERAL,
                                                   "Deleting the template failed (status 0x%02x)",
                                                   status));
      return CV2_OUTCOME_ERROR;
    }

  set_proto_error (error, "Command 0x%02x failed with status 0x%02x", cmd, status);
  return CV2_OUTCOME_ERROR;
}

gchar *
cv2_version_lookup (const gchar *text, const gchar *key)
{
  g_auto(GStrv) lines = g_strsplit (text, "\n", -1);
  gsize key_len = strlen (key);

  for (guint i = 0; lines[i]; i++)
    {
      if (strncmp (lines[i], key, key_len) == 0 && lines[i][key_len] == ':')
        return g_strstrip (g_strdup (lines[i] + key_len + 1));
    }

  return NULL;
}

Cv2FwSupport
cv2_firmware_enroll_support (const gchar *upgrade_ver)
{
  guint64 version;

  if (!upgrade_ver || strlen (upgrade_ver) != 8 ||
      !g_ascii_string_to_unsigned (upgrade_ver, 16, 0, G_MAXUINT32, &version, NULL))
    return CV2_FW_UNKNOWN;

  return version >= CV2_FW_MIN_ENROLL ? CV2_FW_OK : CV2_FW_TOO_OLD;
}

GError *
cv2_firmware_error (const gchar *upgrade_ver)
{
  return fpi_device_error_new_msg (FP_DEVICE_ERROR_NOT_SUPPORTED,
                                   "Enrolling needs ControlVault2 firmware %08x or newer "
                                   "(this reader has %s). Update it with Dell's "
                                   "ControlVault2 firmware package.",
                                   CV2_FW_MIN_ENROLL,
                                   upgrade_ver ? upgrade_ver : "an unknown version");
}

gint
cv2_template_index (const guint32 *templates, guint n_templates, guint32 template_id)
{
  for (guint i = 0; i < n_templates; i++)
    if (templates[i] == template_id)
      return i;

  return -1;
}

/* Stages advance per accepted sample, but never reach the last stage until
 * the reader says the enrollment is complete. */
gint
cv2_enroll_next_stage (gint stage, gboolean done, gint nr_stages)
{
  if (done)
    return nr_stages;

  return MIN (stage + 1, nr_stages - 1);
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `meson compile -C build && meson test -C build cv2-proto --print-errorlogs`
Expected: `cv2-proto OK`.

- [ ] **Step 5: Commit**

```bash
cd ~/projects/libfprint
git add libfprint/drivers/cv2/cv2-proto.c tests/test-cv2-proto.c
git commit -m "cv2: Add status mapping and firmware version gate

Co-Authored-By: Claude <noreply@anthropic.com>"
scripts/uncrustify.sh && git diff --quiet || git commit -a --amend --no-edit
```

---

### Task 6: Command transport and firmware version at open

**Files:**
- Modify: `libfprint/drivers/cv2/cv2.c` (replace the whole file)

**Interfaces:**
- Consumes: Task 3–5 protocol API.
- Produces (static, in `cv2.c`, used by Tasks 7–9):
  - `typedef void (*Cv2CmdCallback) (FpiDeviceCv2 *self, FpiSsm *ssm, Cv2Reply *reply, GError *error);` — `reply` and `error` are transfer-full; exactly one is non-NULL.
  - `static void cv2_cmd (FpiDeviceCv2 *self, FpiSsm *ssm, GByteArray *request, gboolean check_cancel, Cv2CmdCallback callback);` — takes ownership of `request`; `ssm` is passed through to the callback (may be NULL).
  - `static Cv2Reply *cmd_result (Cv2Reply *reply, GError **error);` — returns the reply if status is 0, otherwise NULL with `*error` set.
  - struct fields listed below; `self->fw_version`.

- [ ] **Step 1: Replace `cv2.c` with the transport version**

Keep the license and description comment blocks from Task 2, then:

```c
#define FP_COMPONENT "cv2"

#include "drivers_api.h"

#include "cv2-proto.h"
#include "cv2.h"

#define CV2_INTERFACE      0
#define CV2_EP_OUT         (0x01 | FPI_USB_ENDPOINT_OUT)
#define CV2_EP_IN          (0x01 | FPI_USB_ENDPOINT_IN)
#define CV2_EP_INT         (0x05 | FPI_USB_ENDPOINT_IN)
#define CV2_INT_BUF_LEN    32
#define CV2_INT_REPLY      0
#define CV2_INT_FINGER     3
#define CV2_CMD_TIMEOUT_MS 5000
#define CV2_ENROLL_STAGES  4
#define CV2_MAX_STALE      2

struct _FpiDeviceCv2
{
  FpDevice parent;

  gchar   *fw_version;     /* USH_REL_UPGRADE_VER, NULL if unknown */

  FpiSsm  *task_ssm;
  GError  *task_error;     /* held while cleanup runs */

  guint32  session;
  gboolean session_open;
  gboolean enrolling;
  gboolean capture_retried;
  guint8   capture_id[CV2_CAPTURE_ID_LEN];
  guint8   result_id[CV2_CAPTURE_ID_LEN];
  gint     enroll_stage;
  guint32  template_id;
  GArray  *templates;      /* guint32 handles being matched */
};

G_DEFINE_TYPE (FpiDeviceCv2, fpi_device_cv2, FP_TYPE_DEVICE)

static const FpIdEntry id_table[] = {
  { .vid = 0x0a5c, .pid = 0x5834, },
  { .vid = 0,      .pid = 0,      .driver_data = 0 },
};

/* ---- Transport ----
 *
 * A command is: request on bulk OUT, an interrupt saying a reply of N bytes
 * is ready, then the reply on bulk IN. Command transfers are never
 * cancelled half-way, as that would leave a reply behind for the next
 * command; cancellation is checked before sending instead.
 */

typedef void (*Cv2CmdCallback) (FpiDeviceCv2 *self,
                                FpiSsm       *ssm,
                                Cv2Reply     *reply,
                                GError       *error);

typedef struct
{
  guint16        cmd;
  GByteArray    *request;
  FpiSsm        *caller_ssm;
  Cv2CmdCallback callback;
  Cv2Reply      *reply;
  guint          stale;
} Cv2CmdData;

enum {
  CMD_SEND,
  CMD_WAIT_REPLY,
  CMD_RECV,
  CMD_NUM_STATES,
};

static void
cmd_data_free (gpointer user_data)
{
  Cv2CmdData *data = user_data;

  g_clear_pointer (&data->request, g_byte_array_unref);
  g_clear_pointer (&data->reply, cv2_reply_free);
  g_free (data);
}

static void
cmd_int_cb (FpiUsbTransfer *transfer,
            FpDevice       *dev,
            gpointer        user_data,
            GError         *error)
{
  FpiSsm *ssm = transfer->ssm;
  guint32 type, len;

  if (error)
    {
      fpi_ssm_mark_failed (ssm, error);
      return;
    }

  if (transfer->actual_length < 8)
    {
      fpi_ssm_mark_failed (ssm,
                           fpi_device_error_new_msg (FP_DEVICE_ERROR_PROTO,
                                                     "Short interrupt message (%" G_GSSIZE_FORMAT " bytes)",
                                                     transfer->actual_length));
      return;
    }

  type = FP_READ_UINT32_LE (transfer->buffer);
  len = FP_READ_UINT32_LE (transfer->buffer + 4);

  if (type == CV2_INT_FINGER)
    {
      fp_dbg ("Ignoring a finger event while waiting for a reply");
      fpi_ssm_jump_to_state (ssm, CMD_WAIT_REPLY);
      return;
    }

  if (type != CV2_INT_REPLY || len < CV2_HEADER_LEN || len > CV2_REPLY_BUF_LEN)
    {
      fpi_ssm_mark_failed (ssm,
                           fpi_device_error_new_msg (FP_DEVICE_ERROR_PROTO,
                                                     "Unexpected interrupt message (type %u, length %u)",
                                                     type, len));
      return;
    }

  fpi_ssm_next_state (ssm);
}

static void
cmd_recv_cb (FpiUsbTransfer *transfer,
             FpDevice       *dev,
             gpointer        user_data,
             GError         *error)
{
  FpiSsm *ssm = transfer->ssm;
  Cv2CmdData *data = fpi_ssm_get_data (ssm);
  GError *local_error = NULL;
  guint16 cmd;

  if (error)
    {
      fpi_ssm_mark_failed (ssm, error);
      return;
    }

  /* A reply left over from a timed-out command or an earlier client */
  if (cv2_message_peek_cmd (transfer->buffer, transfer->actual_length, &cmd) &&
      cmd != data->cmd && data->stale < CV2_MAX_STALE)
    {
      fp_dbg ("Discarding a stale reply to 0x%02x", cmd);
      data->stale++;
      fpi_ssm_jump_to_state (ssm, CMD_WAIT_REPLY);
      return;
    }

  data->reply = cv2_reply_parse (transfer->buffer, transfer->actual_length,
                                 data->cmd, &local_error);
  if (!data->reply)
    {
      fpi_ssm_mark_failed (ssm, local_error);
      return;
    }

  fp_dbg ("< 0x%02x status 0x%02x", data->cmd, data->reply->status);
  fpi_ssm_mark_completed (ssm);
}

static void
cmd_run_state (FpiSsm *ssm, FpDevice *dev)
{
  Cv2CmdData *data = fpi_ssm_get_data (ssm);
  FpiUsbTransfer *transfer = fpi_usb_transfer_new (dev);
  gsize len;

  transfer->ssm = ssm;

  switch (fpi_ssm_get_cur_state (ssm))
    {
    case CMD_SEND:
      fp_dbg ("> 0x%02x", data->cmd);
      len = data->request->len;
      fpi_usb_transfer_fill_bulk_full (transfer, CV2_EP_OUT,
                                       g_byte_array_free (g_steal_pointer (&data->request), FALSE),
                                       len, g_free);
      transfer->short_is_error = TRUE;
      fpi_usb_transfer_submit (transfer, CV2_CMD_TIMEOUT_MS, NULL,
                               fpi_ssm_usb_transfer_cb, NULL);
      break;

    case CMD_WAIT_REPLY:
      fpi_usb_transfer_fill_interrupt (transfer, CV2_EP_INT, CV2_INT_BUF_LEN);
      fpi_usb_transfer_submit (transfer, CV2_CMD_TIMEOUT_MS, NULL, cmd_int_cb, NULL);
      break;

    case CMD_RECV:
      fpi_usb_transfer_fill_bulk (transfer, CV2_EP_IN, CV2_REPLY_BUF_LEN);
      fpi_usb_transfer_submit (transfer, CV2_CMD_TIMEOUT_MS, NULL, cmd_recv_cb, NULL);
      break;
    }
}

static void
cmd_ssm_done (FpiSsm *ssm, FpDevice *dev, GError *error)
{
  Cv2CmdData *data = fpi_ssm_get_data (ssm);

  data->callback (FPI_DEVICE_CV2 (dev), data->caller_ssm,
                  error ? NULL : g_steal_pointer (&data->reply), error);
}

static void
cv2_cmd (FpiDeviceCv2  *self,
         FpiSsm        *ssm,
         GByteArray    *request,
         gboolean       check_cancel,
         Cv2CmdCallback callback)
{
  FpDevice *dev = FP_DEVICE (self);
  Cv2CmdData *data;
  FpiSsm *cmd_ssm;
  guint16 cmd = 0;

  if (check_cancel && fpi_device_action_is_cancelled (dev))
    {
      GError *error = NULL;

      g_cancellable_set_error_if_cancelled (fpi_device_get_cancellable (dev), &error);
      g_byte_array_unref (request);
      callback (self, ssm, NULL, error);
      return;
    }

  cv2_message_peek_cmd (request->data, request->len, &cmd);

  data = g_new0 (Cv2CmdData, 1);
  data->cmd = cmd;
  data->request = request;
  data->caller_ssm = ssm;
  data->callback = callback;

  cmd_ssm = fpi_ssm_new_full (dev, cmd_run_state, CMD_NUM_STATES, CMD_NUM_STATES, "cv2-cmd");
  fpi_ssm_set_data (cmd_ssm, data, cmd_data_free);
  fpi_ssm_silence_debug (cmd_ssm);
  fpi_ssm_start (cmd_ssm, cmd_ssm_done);
}

/* Returns the reply if the command succeeded; otherwise NULL with @error set. */
static Cv2Reply *
cmd_result (Cv2Reply *reply, GError **error)
{
  g_autoptr(Cv2Reply) owned = reply;

  if (*error)
    return NULL;

  if (cv2_status_classify (owned->cmd, owned->status, error) != CV2_OUTCOME_OK)
    {
      if (*error == NULL)
        *error = fpi_device_error_new_msg (FP_DEVICE_ERROR_PROTO,
                                           "Command 0x%02x failed with status 0x%02x",
                                           owned->cmd, owned->status);
      return NULL;
    }

  return g_steal_pointer (&owned);
}

/* ---- Device open and close ---- */

static void
version_cb (FpiDeviceCv2 *self, FpiSsm *ssm, Cv2Reply *reply, GError *error)
{
  g_autoptr(Cv2Reply) ok = cmd_result (reply, &error);
  g_autofree gchar *text = NULL;

  if (ok)
    text = cv2_reply_get_version_text (ok, &error);

  if (text)
    {
      self->fw_version = cv2_version_lookup (text, "USH_REL_UPGRADE_VER");
      fp_info ("ControlVault2 firmware %s",
               self->fw_version ? self->fw_version : "(no USH_REL_UPGRADE_VER)");
    }
  else
    {
      fp_info ("Could not read the firmware version: %s", error->message);
    }

  g_clear_error (&error);
  fpi_device_open_complete (FP_DEVICE (self), NULL);
}

static void
cv2_open (FpDevice *dev)
{
  FpiDeviceCv2 *self = FPI_DEVICE_CV2 (dev);
  GError *error = NULL;

  if (!g_usb_device_claim_interface (fpi_device_get_usb_device (dev),
                                     CV2_INTERFACE, 0, &error))
    {
      fpi_device_open_complete (dev, error);
      return;
    }

  g_clear_pointer (&self->fw_version, g_free);
  cv2_cmd (self, NULL, cv2_build_get_version (), FALSE, version_cb);
}

static void
cv2_close (FpDevice *dev)
{
  GError *error = NULL;

  g_usb_device_release_interface (fpi_device_get_usb_device (dev),
                                  CV2_INTERFACE, 0, &error);
  fpi_device_close_complete (dev, error);
}

/* ---- GObject ---- */

static void
fpi_device_cv2_init (FpiDeviceCv2 *self)
{
}

static void
fpi_device_cv2_finalize (GObject *object)
{
  FpiDeviceCv2 *self = FPI_DEVICE_CV2 (object);

  g_clear_pointer (&self->fw_version, g_free);
  g_clear_pointer (&self->templates, g_array_unref);
  g_clear_error (&self->task_error);

  G_OBJECT_CLASS (fpi_device_cv2_parent_class)->finalize (object);
}

static void
fpi_device_cv2_class_init (FpiDeviceCv2Class *klass)
{
  FpDeviceClass *dev_class = FP_DEVICE_CLASS (klass);
  GObjectClass *object_class = G_OBJECT_CLASS (klass);

  object_class->finalize = fpi_device_cv2_finalize;

  dev_class->id = FP_COMPONENT;
  dev_class->full_name = "Broadcom ControlVault2 (BCM5880) Fingerprint Reader";
  dev_class->type = FP_DEVICE_TYPE_USB;
  dev_class->id_table = id_table;
  dev_class->scan_type = FP_SCAN_TYPE_PRESS;
  dev_class->nr_enroll_stages = CV2_ENROLL_STAGES;
  dev_class->temp_hot_seconds = -1;

  dev_class->open = cv2_open;
  dev_class->close = cv2_close;

  fpi_device_class_auto_initialize_features (dev_class);
}
```

If Task 1's outcome was **C**, replace the last two lines of `cv2_open` with `fpi_device_open_complete (dev, NULL);` and delete `version_cb`. `cmd_result` then has no caller until Task 7; an unused-function warning until then is expected.

- [ ] **Step 2: Build**

Run: `meson compile -C build 2>&1 | grep -E "warning|error" | grep cv2`
Expected: no output (no warnings or errors in `cv2*` files).

- [ ] **Step 3: Check open reads the firmware version (hardware, user)**

Run: `pkexec bash ~/projects/cv2-research/devtools/run-dev.sh info 2>&1 | grep -E "firmware|> 0x|< 0x|driver cv2"`
Expected (outcome A or B):
```
... > 0x39
... < 0x39 status 0x00
... ControlVault2 firmware 00412015
driver cv2, Broadcom ControlVault2 (BCM5880) Fingerprint Reader
```

- [ ] **Step 4: Run the unit tests (regression)**

Run: `meson test -C build cv2-proto udev-hwdb --print-errorlogs`
Expected: both OK.

- [ ] **Step 5: Commit**

```bash
cd ~/projects/libfprint
git add libfprint/drivers/cv2/cv2.c
git commit -m "cv2: Add command transport and read the firmware version at open

Co-Authored-By: Claude <noreply@anthropic.com>"
scripts/uncrustify.sh && git diff --quiet || git commit -a --amend --no-edit
```

---

### Task 7: Operation lifecycle, capture and enroll

**Files:**
- Modify: `libfprint/drivers/cv2/cv2.c`

**Interfaces:**
- Consumes: `cv2_cmd`, `cmd_result`, struct fields (Task 6); `cv2_enroll_next_stage`, `cv2_firmware_enroll_support`, `cv2_firmware_error` (Task 5).
- Produces (static, used by Tasks 8–9):
  - `static void cv2_start_task (FpiDeviceCv2 *self, FpiSsmHandlerCallback handler, int nr_states, const char *name);`
  - `static void open_session_cb (...)`, `static void close_session_cb (...)` (`Cv2CmdCallback`s that advance their `ssm`)
  - `static FpiSsm *capture_ssm_new (FpiDeviceCv2 *self);` — starts a capture (with one `0x85` retry), waits for the finger, leaves the ID in `self->capture_id`.
  - `static void cv2_task_complete (FpiDeviceCv2 *self, GError *error);` handling all four actions.

- [ ] **Step 1: Add the lifecycle, capture and enroll sections**

Insert this block in `cv2.c` between the end of the transport section (after `cmd_result`) and `/* ---- Device open and close ---- */`:

```c
/* ---- Operation lifecycle ----
 *
 * Each operation opens a chip session, runs, and closes it. If it fails or
 * is cancelled while a session is open, a cleanup machine cancels any
 * capture, discards an unfinished enrollment and closes the session before
 * the original error is reported.
 */

static void task_ssm_done (FpiSsm   *ssm,
                           FpDevice *dev,
                           GError   *error);

static void
cv2_start_task (FpiDeviceCv2         *self,
                FpiSsmHandlerCallback handler,
                int                   nr_states,
                const char           *name)
{
  g_assert (self->task_ssm == NULL);

  self->session_open = FALSE;
  self->enrolling = FALSE;
  self->capture_retried = FALSE;
  self->enroll_stage = 0;

  self->task_ssm = fpi_ssm_new_full (FP_DEVICE (self), handler, nr_states, nr_states, name);
  fpi_ssm_start (self->task_ssm, task_ssm_done);
}

static void
cv2_task_complete (FpiDeviceCv2 *self, GError *error)
{
  FpDevice *dev = FP_DEVICE (self);
  FpiDeviceAction action = fpi_device_get_current_action (dev);
  FpPrint *print;

  fpi_device_report_finger_status (dev, FP_FINGER_STATUS_NONE);

  if (action == FPI_DEVICE_ACTION_ENROLL)
    {
      if (error)
        {
          fpi_device_enroll_complete (dev, NULL, error);
          return;
        }

      fpi_device_get_enroll_data (dev, &print);
      fpi_print_set_type (print, FPI_PRINT_RAW);
      fpi_print_set_device_stored (print, TRUE);
      g_object_set (print, "fpi-data", g_variant_new ("(u)", self->template_id), NULL);
      fpi_device_enroll_complete (dev, g_object_ref (print), NULL);
    }
  else if (action == FPI_DEVICE_ACTION_VERIFY)
    {
      fpi_device_verify_complete (dev, error);
    }
  else if (action == FPI_DEVICE_ACTION_IDENTIFY)
    {
      fpi_device_identify_complete (dev, error);
    }
  else if (action == FPI_DEVICE_ACTION_DELETE)
    {
      fpi_device_delete_complete (dev, error);
    }
  else
    {
      g_assert_not_reached ();
    }
}

enum {
  CLEANUP_CANCEL_CAPTURE,
  CLEANUP_DISCARD_ENROLLMENT,
  CLEANUP_CLOSE_SESSION,
  CLEANUP_NUM_STATES,
};

static void
cleanup_cmd_cb (FpiDeviceCv2 *self, FpiSsm *ssm, Cv2Reply *reply, GError *error)
{
  g_autoptr(Cv2Reply) ok = cmd_result (reply, &error);

  if (!ok)
    fp_dbg ("Cleanup step failed: %s", error->message);

  g_clear_error (&error);
  fpi_ssm_next_state (ssm);
}

static void
cleanup_run_state (FpiSsm *ssm, FpDevice *dev)
{
  FpiDeviceCv2 *self = FPI_DEVICE_CV2 (dev);

  switch (fpi_ssm_get_cur_state (ssm))
    {
    case CLEANUP_CANCEL_CAPTURE:
      cv2_cmd (self, ssm, cv2_build_capture_cancel (), FALSE, cleanup_cmd_cb);
      break;

    case CLEANUP_DISCARD_ENROLLMENT:
      if (!self->enrolling)
        {
          fpi_ssm_next_state (ssm);
          break;
        }
      cv2_cmd (self, ssm, cv2_build_enroll_discard (), FALSE, cleanup_cmd_cb);
      break;

    case CLEANUP_CLOSE_SESSION:
      cv2_cmd (self, ssm, cv2_build_close_session (self->session), FALSE, cleanup_cmd_cb);
      break;
    }
}

static void
cleanup_done (FpiSsm *ssm, FpDevice *dev, GError *error)
{
  FpiDeviceCv2 *self = FPI_DEVICE_CV2 (dev);

  /* Cleanup steps never fail the machine; see cleanup_cmd_cb */
  g_clear_error (&error);
  self->session_open = FALSE;
  self->enrolling = FALSE;
  cv2_task_complete (self, g_steal_pointer (&self->task_error));
}

static void
task_ssm_done (FpiSsm *ssm, FpDevice *dev, GError *error)
{
  FpiDeviceCv2 *self = FPI_DEVICE_CV2 (dev);
  FpiSsm *cleanup;

  self->task_ssm = NULL;

  if (error && self->session_open)
    {
      fp_dbg ("Operation failed (%s), cleaning up", error->message);
      self->task_error = error;
      cleanup = fpi_ssm_new_full (dev, cleanup_run_state, CLEANUP_NUM_STATES,
                                  CLEANUP_NUM_STATES, "cv2-cleanup");
      fpi_ssm_start (cleanup, cleanup_done);
      return;
    }

  cv2_task_complete (self, error);
}

static void
open_session_cb (FpiDeviceCv2 *self, FpiSsm *ssm, Cv2Reply *reply, GError *error)
{
  g_autoptr(Cv2Reply) ok = cmd_result (reply, &error);

  if (!ok || !cv2_reply_get_session (ok, &self->session, &error))
    {
      fpi_ssm_mark_failed (ssm, error);
      return;
    }

  self->session_open = TRUE;
  fpi_ssm_next_state (ssm);
}

/* The operation already succeeded, so a failed close is only logged. */
static void
close_session_cb (FpiDeviceCv2 *self, FpiSsm *ssm, Cv2Reply *reply, GError *error)
{
  g_autoptr(Cv2Reply) ok = cmd_result (reply, &error);

  if (!ok)
    fp_info ("Closing the session failed: %s", error->message);

  g_clear_error (&error);
  self->session_open = FALSE;
  fpi_ssm_next_state (ssm);
}

/* ---- Capture ----
 *
 * Starts a capture and waits for a finger. If a capture is still pending
 * (left by another client), cancels it and retries once.
 */

enum {
  CAPTURE_START,
  CAPTURE_CANCEL_PENDING,
  CAPTURE_WAIT_FINGER,
  CAPTURE_NUM_STATES,
};

static void
capture_start_cb (FpiDeviceCv2 *self, FpiSsm *ssm, Cv2Reply *reply, GError *error)
{
  g_autoptr(Cv2Reply) owned = reply;
  Cv2Outcome outcome;

  if (error)
    {
      fpi_ssm_mark_failed (ssm, error);
      return;
    }

  outcome = cv2_status_classify (CV2_CMD_CAPTURE_START, owned->status, &error);
  if (outcome == CV2_OUTCOME_CAPTURE_PENDING && !self->capture_retried)
    {
      fp_dbg ("A capture is still pending; cancelling it and retrying");
      g_clear_error (&error);
      self->capture_retried = TRUE;
      fpi_ssm_jump_to_state (ssm, CAPTURE_CANCEL_PENDING);
      return;
    }

  if (outcome != CV2_OUTCOME_OK)
    {
      fpi_ssm_mark_failed (ssm, error);
      return;
    }

  if (!cv2_reply_get_capture_id (owned, self->capture_id, &error))
    {
      fpi_ssm_mark_failed (ssm, error);
      return;
    }

  self->capture_retried = FALSE;
  fpi_ssm_jump_to_state (ssm, CAPTURE_WAIT_FINGER);
}

static void
capture_cancel_pending_cb (FpiDeviceCv2 *self, FpiSsm *ssm, Cv2Reply *reply, GError *error)
{
  g_autoptr(Cv2Reply) ok = cmd_result (reply, &error);

  if (!ok)
    {
      fpi_ssm_mark_failed (ssm, error);
      return;
    }

  fpi_ssm_jump_to_state (ssm, CAPTURE_START);
}

static void
wait_finger_cb (FpiUsbTransfer *transfer,
                FpDevice       *dev,
                gpointer        user_data,
                GError         *error)
{
  guint32 type;

  if (error)
    {
      fpi_ssm_mark_failed (transfer->ssm, error);
      return;
    }

  if (transfer->actual_length < 8)
    {
      fpi_ssm_mark_failed (transfer->ssm,
                           fpi_device_error_new_msg (FP_DEVICE_ERROR_PROTO,
                                                     "Short interrupt message (%" G_GSSIZE_FORMAT " bytes)",
                                                     transfer->actual_length));
      return;
    }

  type = FP_READ_UINT32_LE (transfer->buffer);
  if (type != CV2_INT_FINGER)
    {
      fpi_ssm_mark_failed (transfer->ssm,
                           fpi_device_error_new_msg (FP_DEVICE_ERROR_PROTO,
                                                     "Expected a finger event, got interrupt type %u",
                                                     type));
      return;
    }

  fpi_device_report_finger_status_changes (dev, FP_FINGER_STATUS_PRESENT,
                                           FP_FINGER_STATUS_NEEDED);
  fpi_ssm_mark_completed (transfer->ssm);
}

static void
capture_run_state (FpiSsm *ssm, FpDevice *dev)
{
  FpiDeviceCv2 *self = FPI_DEVICE_CV2 (dev);
  FpiUsbTransfer *transfer;

  switch (fpi_ssm_get_cur_state (ssm))
    {
    case CAPTURE_START:
      cv2_cmd (self, ssm, cv2_build_capture_start (self->session), TRUE, capture_start_cb);
      break;

    case CAPTURE_CANCEL_PENDING:
      cv2_cmd (self, ssm, cv2_build_capture_cancel (), TRUE, capture_cancel_pending_cb);
      break;

    case CAPTURE_WAIT_FINGER:
      transfer = fpi_usb_transfer_new (dev);
      transfer->ssm = ssm;
      fpi_usb_transfer_fill_interrupt (transfer, CV2_EP_INT, CV2_INT_BUF_LEN);
      fpi_usb_transfer_submit (transfer, 0, fpi_device_get_cancellable (dev),
                               wait_finger_cb, NULL);
      fpi_device_report_finger_status (dev, FP_FINGER_STATUS_NEEDED);
      break;
    }
}

static FpiSsm *
capture_ssm_new (FpiDeviceCv2 *self)
{
  return fpi_ssm_new_full (FP_DEVICE (self), capture_run_state, CAPTURE_NUM_STATES,
                           CAPTURE_NUM_STATES, "cv2-capture");
}

/* ---- Enroll ---- */

enum {
  ENROLL_OPEN_SESSION,
  ENROLL_BEGIN,
  ENROLL_CAPTURE,
  ENROLL_SAMPLE,
  ENROLL_COMMIT,
  ENROLL_CLOSE_SESSION,
  ENROLL_NUM_STATES,
};

static void
enroll_begin_cb (FpiDeviceCv2 *self, FpiSsm *ssm, Cv2Reply *reply, GError *error)
{
  g_autoptr(Cv2Reply) ok = cmd_result (reply, &error);

  if (!ok)
    {
      fpi_ssm_mark_failed (ssm, error);
      return;
    }

  self->enrolling = TRUE;
  fpi_ssm_next_state (ssm);
}

static void
enroll_sample_cb (FpiDeviceCv2 *self, FpiSsm *ssm, Cv2Reply *reply, GError *error)
{
  FpDevice *dev = FP_DEVICE (self);
  g_autoptr(Cv2Reply) owned = reply;
  Cv2Outcome outcome;
  gboolean done = FALSE;

  if (error)
    {
      fpi_ssm_mark_failed (ssm, error);
      return;
    }

  fpi_device_report_finger_status (dev, FP_FINGER_STATUS_NONE);

  outcome = cv2_status_classify (CV2_CMD_ENROLL_SAMPLE, owned->status, &error);
  if (outcome == CV2_OUTCOME_RETRY)
    {
      fpi_device_enroll_progress (dev, self->enroll_stage, NULL, g_steal_pointer (&error));
      fpi_ssm_jump_to_state (ssm, ENROLL_CAPTURE);
      return;
    }

  if (outcome != CV2_OUTCOME_OK ||
      !cv2_reply_get_sample (owned, &done, self->result_id, &error))
    {
      fpi_ssm_mark_failed (ssm, error);
      return;
    }

  self->enroll_stage = cv2_enroll_next_stage (self->enroll_stage, done, CV2_ENROLL_STAGES);
  fpi_device_enroll_progress (dev, self->enroll_stage, NULL, NULL);

  if (done)
    fpi_ssm_next_state (ssm);
  else
    fpi_ssm_jump_to_state (ssm, ENROLL_CAPTURE);
}

static void
enroll_commit_cb (FpiDeviceCv2 *self, FpiSsm *ssm, Cv2Reply *reply, GError *error)
{
  g_autoptr(Cv2Reply) ok = cmd_result (reply, &error);

  if (!ok || !cv2_reply_get_template (ok, &self->template_id, &error))
    {
      fpi_ssm_mark_failed (ssm, error);
      return;
    }

  self->enrolling = FALSE;
  fp_info ("Enrolled template 0x%08x", self->template_id);
  fpi_ssm_next_state (ssm);
}

static void
enroll_run_state (FpiSsm *ssm, FpDevice *dev)
{
  FpiDeviceCv2 *self = FPI_DEVICE_CV2 (dev);

  switch (fpi_ssm_get_cur_state (ssm))
    {
    case ENROLL_OPEN_SESSION:
      cv2_cmd (self, ssm, cv2_build_open_session (), TRUE, open_session_cb);
      break;

    case ENROLL_BEGIN:
      cv2_cmd (self, ssm, cv2_build_enroll_begin (), TRUE, enroll_begin_cb);
      break;

    case ENROLL_CAPTURE:
      fpi_ssm_start_subsm (ssm, capture_ssm_new (self));
      break;

    case ENROLL_SAMPLE:
      cv2_cmd (self, ssm, cv2_build_enroll_sample (self->session, self->capture_id),
               TRUE, enroll_sample_cb);
      break;

    case ENROLL_COMMIT:
      cv2_cmd (self, ssm, cv2_build_enroll_commit (self->session, self->result_id),
               TRUE, enroll_commit_cb);
      break;

    case ENROLL_CLOSE_SESSION:
      cv2_cmd (self, ssm, cv2_build_close_session (self->session), FALSE, close_session_cb);
      break;
    }
}

static void
cv2_enroll (FpDevice *dev)
{
  FpiDeviceCv2 *self = FPI_DEVICE_CV2 (dev);

  if (cv2_firmware_enroll_support (self->fw_version) == CV2_FW_TOO_OLD)
    {
      fpi_device_enroll_complete (dev, NULL, cv2_firmware_error (self->fw_version));
      return;
    }

  cv2_start_task (self, enroll_run_state, ENROLL_NUM_STATES, "cv2-enroll");
}
```

In `fpi_device_cv2_class_init`, add after `dev_class->close = cv2_close;`:

```c
  dev_class->enroll = cv2_enroll;
```

- [ ] **Step 2: Build**

Run: `meson compile -C build 2>&1 | grep -E "warning|error" | grep cv2`
Expected: no output.

- [ ] **Step 3: Enroll on hardware (hardware, user)**

Run: `pkexec bash ~/projects/cv2-research/devtools/run-dev.sh enroll right-index 2>&1 | grep -vE "^\s*$" | grep -E "stage|ENROLLED|Enrolled template|pending|error|warn"`
Expected: a few `stage N/4` lines (a retry line is fine), `Enrolled template 0x........`, `ENROLLED right-index`.
The first run after fprintd stops may log `A capture is still pending; cancelling it and retrying` — that is the `0x85` path working.

- [ ] **Step 4: Cancel during an enrollment (hardware, user)**

Run: `pkexec bash ~/projects/cv2-research/devtools/run-dev.sh cancel-enroll 2>&1 | grep -E "stage|cancelling|CANCELLED|UNEXPECTED|> 0x|< 0x|finger status"`
Expected: `stage 1/4`, `cancelling while waiting for the second press`, then `> 0x68`, `> 0x6d`, `> 0x04` with their replies, then `CANCELLED` and `finger status after cancel: <flags NONE …>` (shown as 0 / `FP_FINGER_STATUS_NONE`).

- [ ] **Step 5: Recover from a killed client (hardware, user)**

Run `pkexec bash ~/projects/cv2-research/devtools/run-dev.sh enroll left-index`, press once, then press Ctrl-C in the terminal (this kills the script without cleanup). Then run the same enroll command again and complete it.
Expected: the second run succeeds (`ENROLLED left-index`), possibly after `A capture is still pending; cancelling it and retrying` or `Discarding a stale reply`. Note in the task report which of these happened.

- [ ] **Step 6: Commit**

```bash
cd ~/projects/libfprint
git add libfprint/drivers/cv2/cv2.c
git commit -m "cv2: Add enrollment

Co-Authored-By: Claude <noreply@anthropic.com>"
scripts/uncrustify.sh && git diff --quiet || git commit -a --amend --no-edit
```

---

### Task 8: Verify and identify

**Files:**
- Modify: `libfprint/drivers/cv2/cv2.c`

**Interfaces:**
- Consumes: `cv2_start_task`, `open_session_cb`, `close_session_cb`, `capture_ssm_new` (Task 7); `cv2_build_match`, `cv2_template_index` (Tasks 3, 5).
- Produces: `static gboolean cv2_print_get_template (FpPrint *print, guint32 *template_id);` (used by Task 9).

- [ ] **Step 1: Add the match section**

Insert before `/* ---- Device open and close ---- */`:

```c
/* ---- Verify and identify ----
 *
 * Verify is identify against one print: one capture, then one match command
 * with every template handle.
 */

enum {
  MATCH_OPEN_SESSION,
  MATCH_CAPTURE,
  MATCH_MATCH,
  MATCH_CANCEL_CAPTURE,
  MATCH_CLOSE_SESSION,
  MATCH_NUM_STATES,
};

/* Prints store the chip's template handle as "(u)". */
static gboolean
cv2_print_get_template (FpPrint *print, guint32 *template_id)
{
  g_autoptr(GVariant) data = NULL;

  g_object_get (print, "fpi-data", &data, NULL);
  if (!data || !g_variant_check_format_string (data, "(u)", FALSE))
    return FALSE;

  g_variant_get (data, "(u)", template_id);
  return TRUE;
}

static FpPrint *
match_print_at (FpiDeviceCv2 *self, guint pos)
{
  FpDevice *dev = FP_DEVICE (self);
  GPtrArray *prints;
  FpPrint *print;

  if (fpi_device_get_current_action (dev) == FPI_DEVICE_ACTION_VERIFY)
    {
      fpi_device_get_verify_data (dev, &print);
      return print;
    }

  fpi_device_get_identify_data (dev, &prints);
  return g_ptr_array_index (prints, pos);
}

static void
match_cb (FpiDeviceCv2 *self, FpiSsm *ssm, Cv2Reply *reply, GError *error)
{
  FpDevice *dev = FP_DEVICE (self);
  g_autoptr(Cv2Reply) owned = reply;
  Cv2Outcome outcome;
  gboolean matched = FALSE;
  guint32 template_id = 0;
  FpPrint *match = NULL;

  if (error)
    {
      fpi_ssm_mark_failed (ssm, error);
      return;
    }

  fpi_device_report_finger_status (dev, FP_FINGER_STATUS_NONE);

  outcome = cv2_status_classify (CV2_CMD_MATCH, owned->status, &error);
  if (outcome == CV2_OUTCOME_OK)
    {
      if (!cv2_reply_get_match (owned, &matched, &template_id, &error))
        {
          fpi_ssm_mark_failed (ssm, error);
          return;
        }
    }
  else if (outcome != CV2_OUTCOME_NO_MATCH)
    {
      fpi_ssm_mark_failed (ssm, error);
      return;
    }

  if (matched)
    {
      gint pos = cv2_template_index ((const guint32 *) self->templates->data,
                                     self->templates->len, template_id);

      if (pos < 0)
        {
          fpi_ssm_mark_failed (ssm,
                               fpi_device_error_new_msg (FP_DEVICE_ERROR_PROTO,
                                                         "Reader matched template 0x%08x, which was not requested",
                                                         template_id));
          return;
        }
      match = match_print_at (self, pos);
    }

  if (fpi_device_get_current_action (dev) == FPI_DEVICE_ACTION_VERIFY)
    fpi_device_verify_report (dev, match ? FPI_MATCH_SUCCESS : FPI_MATCH_FAIL, NULL, NULL);
  else
    fpi_device_identify_report (dev, match, NULL, NULL);

  fpi_ssm_next_state (ssm);
}

/* The result is already reported, so a failed cancel is only logged. */
static void
match_cancel_capture_cb (FpiDeviceCv2 *self, FpiSsm *ssm, Cv2Reply *reply, GError *error)
{
  g_autoptr(Cv2Reply) ok = cmd_result (reply, &error);

  if (!ok)
    fp_info ("Cancelling the capture failed: %s", error->message);

  g_clear_error (&error);
  fpi_ssm_next_state (ssm);
}

static void
match_run_state (FpiSsm *ssm, FpDevice *dev)
{
  FpiDeviceCv2 *self = FPI_DEVICE_CV2 (dev);

  switch (fpi_ssm_get_cur_state (ssm))
    {
    case MATCH_OPEN_SESSION:
      cv2_cmd (self, ssm, cv2_build_open_session (), TRUE, open_session_cb);
      break;

    case MATCH_CAPTURE:
      fpi_ssm_start_subsm (ssm, capture_ssm_new (self));
      break;

    case MATCH_MATCH:
      cv2_cmd (self, ssm,
               cv2_build_match (self->session, (const guint32 *) self->templates->data,
                                self->templates->len),
               TRUE, match_cb);
      break;

    case MATCH_CANCEL_CAPTURE:
      cv2_cmd (self, ssm, cv2_build_capture_cancel (), FALSE, match_cancel_capture_cb);
      break;

    case MATCH_CLOSE_SESSION:
      cv2_cmd (self, ssm, cv2_build_close_session (self->session), FALSE, close_session_cb);
      break;
    }
}

static void
cv2_verify_identify (FpDevice *dev)
{
  FpiDeviceCv2 *self = FPI_DEVICE_CV2 (dev);
  gboolean verify = fpi_device_get_current_action (dev) == FPI_DEVICE_ACTION_VERIFY;
  GPtrArray *prints = NULL;
  FpPrint *print = NULL;
  guint32 template_id;

  g_clear_pointer (&self->templates, g_array_unref);
  self->templates = g_array_new (FALSE, FALSE, sizeof (guint32));

  if (verify)
    {
      fpi_device_get_verify_data (dev, &print);
      if (!cv2_print_get_template (print, &template_id))
        {
          fpi_device_verify_complete (dev, fpi_device_error_new (FP_DEVICE_ERROR_DATA_INVALID));
          return;
        }
      g_array_append_val (self->templates, template_id);
    }
  else
    {
      fpi_device_get_identify_data (dev, &prints);
      for (guint i = 0; i < prints->len; i++)
        {
          if (!cv2_print_get_template (g_ptr_array_index (prints, i), &template_id))
            {
              fpi_device_identify_complete (dev,
                                            fpi_device_error_new (FP_DEVICE_ERROR_DATA_INVALID));
              return;
            }
          g_array_append_val (self->templates, template_id);
        }

      if (self->templates->len == 0)
        {
          fpi_device_identify_report (dev, NULL, NULL, NULL);
          fpi_device_identify_complete (dev, NULL);
          return;
        }
    }

  cv2_start_task (self, match_run_state, MATCH_NUM_STATES, "cv2-match");
}
```

In `fpi_device_cv2_class_init`, add after `dev_class->enroll = cv2_enroll;`:

```c
  dev_class->verify = cv2_verify_identify;
  dev_class->identify = cv2_verify_identify;
```

- [ ] **Step 2: Build**

Run: `meson compile -C build 2>&1 | grep -E "warning|error" | grep cv2`
Expected: no output.

- [ ] **Step 3: Verify and identify on hardware (hardware, user)**

Uses the right-index and left-index prints enrolled in Task 7. Run each and press the finger named in "press":

| Command | Press | Expected last line |
|---|---|---|
| `run-dev.sh verify right-index` | right index | `MATCH` |
| `run-dev.sh verify right-index` | right thumb | `NO MATCH` |
| `run-dev.sh identify` | left index | `MATCH left-index` |
| `run-dev.sh identify` | right index | `MATCH right-index` |
| `run-dev.sh identify` | right thumb | `NO MATCH` |
| `run-dev.sh verify-bogus` | nothing | `DATA_INVALID` |

(Each command is `pkexec bash ~/projects/cv2-research/devtools/run-dev.sh …`.) The `verify-bogus` run must finish without asking for a finger.

- [ ] **Step 4: Commit**

```bash
cd ~/projects/libfprint
git add libfprint/drivers/cv2/cv2.c
git commit -m "cv2: Add verify and identify

Co-Authored-By: Claude <noreply@anthropic.com>"
scripts/uncrustify.sh && git diff --quiet || git commit -a --amend --no-edit
```

---

### Task 9: Delete, and stale-handle behaviour

**Files:**
- Modify: `libfprint/drivers/cv2/cv2.c`
- Modify (research repo): `protocol/PROTOCOL.md` (answer open question 3)
- Possibly modify: `libfprint/drivers/cv2/cv2-proto.c`, `tests/test-cv2-proto.c` (Step 5)

**Interfaces:**
- Consumes: `cv2_start_task`, `open_session_cb`, `close_session_cb` (Task 7); `cv2_print_get_template` (Task 8); `cv2_build_delete` (Task 3).
- Produces: `FP_DEVICE_FEATURE_STORAGE` set on the class.

- [ ] **Step 1: Add the delete section**

Insert before `/* ---- Device open and close ---- */`:

```c
/* ---- Delete ---- */

enum {
  DELETE_OPEN_SESSION,
  DELETE_TEMPLATE,
  DELETE_CLOSE_SESSION,
  DELETE_NUM_STATES,
};

static void
delete_template_cb (FpiDeviceCv2 *self, FpiSsm *ssm, Cv2Reply *reply, GError *error)
{
  g_autoptr(Cv2Reply) ok = cmd_result (reply, &error);

  if (!ok)
    {
      fpi_ssm_mark_failed (ssm, error);
      return;
    }

  fp_info ("Deleted template 0x%08x", self->template_id);
  fpi_ssm_next_state (ssm);
}

static void
delete_run_state (FpiSsm *ssm, FpDevice *dev)
{
  FpiDeviceCv2 *self = FPI_DEVICE_CV2 (dev);

  switch (fpi_ssm_get_cur_state (ssm))
    {
    case DELETE_OPEN_SESSION:
      cv2_cmd (self, ssm, cv2_build_open_session (), TRUE, open_session_cb);
      break;

    case DELETE_TEMPLATE:
      cv2_cmd (self, ssm, cv2_build_delete (self->session, self->template_id),
               TRUE, delete_template_cb);
      break;

    case DELETE_CLOSE_SESSION:
      cv2_cmd (self, ssm, cv2_build_close_session (self->session), FALSE, close_session_cb);
      break;
    }
}

static void
cv2_delete (FpDevice *dev)
{
  FpiDeviceCv2 *self = FPI_DEVICE_CV2 (dev);
  FpPrint *print = NULL;

  fpi_device_get_delete_data (dev, &print);
  if (!cv2_print_get_template (print, &self->template_id))
    {
      fpi_device_delete_complete (dev, fpi_device_error_new (FP_DEVICE_ERROR_DATA_INVALID));
      return;
    }

  cv2_start_task (self, delete_run_state, DELETE_NUM_STATES, "cv2-delete");
}
```

In `fpi_device_cv2_class_init`, add after the identify line:

```c
  dev_class->delete = cv2_delete;
```

and after `fpi_device_class_auto_initialize_features (dev_class);`:

```c
  /* Prints live on the chip, but it can't list them; fprintd's storage is
   * the source of truth. */
  dev_class->features |= FP_DEVICE_FEATURE_STORAGE;
```

- [ ] **Step 2: Build**

Run: `meson compile -C build 2>&1 | grep -E "warning|error" | grep cv2`
Expected: no output.

- [ ] **Step 3: Delete on hardware, and probe a deleted handle (hardware, user)**

First copy the right-index print so the deleted handle can be tried afterwards:

```bash
cp ~/projects/cv2-research/devtools/prints/right-index.print ~/projects/cv2-research/devtools/prints/stale.print
```

Then, each with `pkexec bash ~/projects/cv2-research/devtools/run-dev.sh …`:

1. `delete right-index` → expected `Deleted template 0x…`, `DELETED right-index`.
2. `verify stale`, pressing the right index → record what happens (the match status and result in the debug log, and the final line).
3. `delete stale` → record what happens (the `0x0a` status in the debug log, and the final line).
4. `delete left-index` → `DELETED left-index`.
5. `rm ~/projects/cv2-research/devtools/prints/stale.print`.

- [ ] **Step 4: Record the stale-handle behaviour in PROTOCOL.md (research repo)**

In `protocol/PROTOCOL.md`, move open question 3 into the *Verify* and *Delete* sections as observed facts, e.g. "`0x2f` with a deleted handle: status 0, result 0 (no match)" and "`0x0a` with a deleted handle: status 0x…". Commit:

```bash
cd ~/projects/cv2-research
git add protocol/PROTOCOL.md cvtool.log
git commit -m "PROTOCOL: behaviour of match and delete with a deleted handle

Co-Authored-By: Claude <noreply@anthropic.com>"
```

- [ ] **Step 5: Map a stale-handle delete status to DATA_NOT_FOUND (only if Step 3.3 returned a specific nonzero status)**

If deleting the stale handle returned a nonzero status `S`, the user-facing error should be "not found", so fprintd can treat the print as already gone. Add a test row to `test_status_classify` (replace `0xSS` with `S`):

```c
    { CV2_CMD_DELETE, 0xSS, CV2_OUTCOME_ERROR, 0, FP_DEVICE_ERROR_DATA_NOT_FOUND },
```

Run `meson test -C build cv2-proto` and confirm it fails (the row gets `FP_DEVICE_ERROR_GENERAL`). Then add to `cv2-proto.h`'s `Cv2Status`:

```c
  CV2_STATUS_NO_TEMPLATE     = 0xSS,
```

and in `cv2_status_classify`, before the `if (cmd == CV2_CMD_DELETE)` block:

```c
  if (cmd == CV2_CMD_DELETE && status == CV2_STATUS_NO_TEMPLATE)
    {
      g_propagate_error (error,
                         fpi_device_error_new_msg (FP_DEVICE_ERROR_DATA_NOT_FOUND,
                                                   "The template is not on the reader"));
      return CV2_OUTCOME_ERROR;
    }
```

Run `meson test -C build cv2-proto` → OK. If the stale delete returned 0, skip this step.

- [ ] **Step 6: Commit**

```bash
cd ~/projects/libfprint
git add libfprint/drivers/cv2 tests/test-cv2-proto.c
git commit -m "cv2: Add template deletion

Co-Authored-By: Claude <noreply@anthropic.com>"
scripts/uncrustify.sh && git diff --quiet || git commit -a --amend --no-edit
```

---

### Task 10: umockdev recordings

**Files:**
- Create: `tests/cv2/custom.py`, `tests/cv2/device`, `tests/cv2/custom.pcapng`
- Create: `tests/cv2-cancel/custom.py`, `tests/cv2-cancel/device`, `tests/cv2-cancel/custom.pcapng`
- Modify: `tests/meson.build` (`drivers_tests`)

**Interfaces:**
- Consumes: the finished driver (Tasks 6–9).

- [ ] **Step 1: Write the main test script**

Create `tests/cv2/custom.py` (mode 755):

```python
#!/usr/bin/env python3

import sys
import traceback

import gi

gi.require_version('FPrint', '2.0')
from gi.repository import FPrint, GLib

# Exit with error on any exception, including those in async callbacks
sys.excepthook = lambda *args: (traceback.print_exception(*args), sys.exit(1))

c = FPrint.Context()
c.enumerate()
devices = c.get_devices()

d = devices[0]
del devices

assert d.get_driver() == 'cv2'
assert not d.has_feature(FPrint.DeviceFeature.CAPTURE)
assert d.has_feature(FPrint.DeviceFeature.VERIFY)
assert d.has_feature(FPrint.DeviceFeature.IDENTIFY)
assert d.has_feature(FPrint.DeviceFeature.STORAGE)
assert d.has_feature(FPrint.DeviceFeature.STORAGE_DELETE)
assert not d.has_feature(FPrint.DeviceFeature.STORAGE_LIST)
assert not d.has_feature(FPrint.DeviceFeature.STORAGE_CLEAR)
assert not d.has_feature(FPrint.DeviceFeature.DUPLICATES_CHECK)

d.open_sync()

retries = 0


def enroll_progress(dev, stage, pnt, data, error):
    global retries
    if error:
        retries += 1
    print('enroll progress: stage %d, error %s' % (stage, error))


def enroll(finger, prompt):
    global retries
    retries = 0
    print(prompt)
    template = FPrint.Print.new(d)
    template.set_finger(finger)
    assert d.get_finger_status() == FPrint.FingerStatusFlags.NONE
    p = d.enroll_sync(template, None, enroll_progress, None)
    assert d.get_finger_status() == FPrint.FingerStatusFlags.NONE
    return p, retries


print('a print without template data is rejected without using the reader')
try:
    d.verify_sync(FPrint.Print.new(d))
    raise AssertionError('verify with an empty print succeeded')
except GLib.Error as e:
    assert e.matches(FPrint.DeviceError.quark(), FPrint.DeviceError.DATA_INVALID), e

p_right, r = enroll(FPrint.Finger.RIGHT_INDEX,
                    '>>> Enroll RIGHT INDEX. Make one deliberately bad press '
                    '(just touch the edge of the fingertip), then press normally.')
assert r >= 1, 'record again: no sample was rejected'

print('>>> Verify: press RIGHT INDEX')
ok, _ = d.verify_sync(p_right)
assert ok

print('>>> Verify: press RIGHT THUMB (not enrolled)')
ok, _ = d.verify_sync(p_right)
assert not ok
assert d.get_finger_status() == FPrint.FingerStatusFlags.NONE

p_left, _ = enroll(FPrint.Finger.LEFT_INDEX, '>>> Enroll LEFT INDEX')

gallery = [FPrint.Print.deserialize(p.serialize()) for p in (p_right, p_left)]

print('>>> Identify: press LEFT INDEX')
match, _ = d.identify_sync(gallery)
assert match is not None and match.equal(p_left)

print('>>> Identify: press RIGHT THUMB (not enrolled)')
match, _ = d.identify_sync(gallery)
assert match is None

print('deleting both prints')
d.delete_print_sync(p_right)
d.delete_print_sync(p_left)

d.close_sync()

del d
del c
```

- [ ] **Step 2: Write the cancellation test script**

Create `tests/cv2-cancel/custom.py` (mode 755):

```python
#!/usr/bin/env python3

import sys
import traceback

import gi

gi.require_version('FPrint', '2.0')
from gi.repository import FPrint, GLib, Gio

sys.excepthook = lambda *args: (traceback.print_exception(*args), sys.exit(1))

ctx = GLib.main_context_default()

c = FPrint.Context()
c.enumerate()
devices = c.get_devices()

d = devices[0]
del devices

assert d.get_driver() == 'cv2'
d.open_sync()

cancellable = Gio.Cancellable()
stages = []
result = {}


def progress(dev, stage, pnt, data, error):
    print('enroll progress: stage %d, error %s' % (stage, error))
    if not error:
        stages.append(stage)


def on_finger_status(dev, pspec):
    # Cancel while the reader waits for the second press. This point is
    # deterministic, so the replay sees the same USB traffic.
    if (stages and not cancellable.is_cancelled() and
            dev.get_finger_status() & FPrint.FingerStatusFlags.NEEDED):
        print('cancelling')
        cancellable.cancel()


def done(dev, res):
    try:
        dev.enroll_finish(res)
        result['error'] = None
    except GLib.Error as e:
        result['error'] = e


handler = d.connect('notify::finger-status', on_finger_status)

print('>>> Enroll RIGHT INDEX: press ONCE, then wait')
template = FPrint.Print.new(d)
template.set_finger(FPrint.Finger.RIGHT_INDEX)
d.enroll(template, cancellable=cancellable, progress_cb=progress, callback=done)

while 'error' not in result:
    ctx.iteration(True)

d.disconnect(handler)
assert result['error'] is not None
assert result['error'].matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED), result['error']
assert stages == [1], stages
assert d.get_finger_status() == FPrint.FingerStatusFlags.NONE

print('>>> The reader is usable again: enroll RIGHT INDEX fully')
template = FPrint.Print.new(d)
template.set_finger(FPrint.Finger.RIGHT_INDEX)
p = d.enroll_sync(template, None, progress, None)

print('deleting the print')
d.delete_print_sync(p)

d.close_sync()

del d
del c
```

- [ ] **Step 3: Record both tests (hardware, user)**

```bash
cd ~/projects/libfprint
meson compile -C build
chmod +x build/tests/create-driver-test.py
pkexec bash -c 'modprobe usbmon; systemctl mask --runtime fprintd; systemctl stop fprintd;
  '"$PWD"'/build/tests/create-driver-test.py --test custom cv2;
  '"$PWD"'/build/tests/create-driver-test.py --test custom cv2 cancel;
  systemctl unmask --runtime fprintd; systemctl start fprintd'
sudo chown -R "$USER" tests/cv2 tests/cv2-cancel
```

Follow the `>>>` prompts. Expected: each run ends without a Python traceback and writes `device` and `custom.pcapng` into its directory. If `custom.py` fails (for example no rejected sample), run that recording again.

- [ ] **Step 4: Check the recordings**

```bash
grep -i -E "serial|ATTR\{serial\}" tests/cv2/device tests/cv2-cancel/device
tshark -r tests/cv2/custom.pcapng -T fields -e usb.capdata 2>/dev/null | tr -d ':' \
  | grep -cE '^010000002c0000006600420502000000[0-9a-f]{8}85000000'
tshark -r tests/cv2-cancel/custom.pcapng -T fields -e usb.capdata 2>/dev/null | tr -d ':' \
  | grep -cE '^01000000380000006d004004'
```

Expected: the first command shows any USB serial number. If it is a unique per-device value, tell the user before committing (it would be published with the MR). The second line counts `0x85` replies in the main recording (≥ 1 is good — the retry path is covered; 0 is acceptable, note it in the task report). The third line is ≥ 1 (the discard was recorded).

- [ ] **Step 5: Register the tests**

In `tests/meson.build`, add to `drivers_tests` after `'secugen': {},`:

```meson
    'cv2': {},
    'cv2-cancel': {},
```

- [ ] **Step 6: Run the replays**

Run: `meson test -C build cv2 cv2-cancel --print-errorlogs`
Expected: both OK. If a replay fails with a USB mismatch, compare the replay log's last transfers with the recording; the usual cause is a code path that differs between recording and replay (for example timing-dependent behaviour), which must be fixed in the driver, not in the recording.

- [ ] **Step 7: Run the full test suite**

Run: `meson test -C build --print-errorlogs`
Expected: everything that passed in Task 2 Step 3 still passes, plus `cv2-proto`, `cv2`, `cv2-cancel`.

- [ ] **Step 8: Commit**

```bash
cd ~/projects/libfprint
git add tests/cv2 tests/cv2-cancel tests/meson.build
git commit -m "tests/cv2: Add enroll, verify, identify, delete and cancellation recordings

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 11: Run fprintd on the new driver and the manual checklist

**Files:**
- Create: `/etc/systemd/system/fprintd.service.d/cv2-dev.conf` (system)
- Create (research repo): `devtools/install-dev.sh`, `devtools/uninstall-dev.sh`

**Interfaces:**
- Consumes: the finished driver.

This switches the laptop from the patched TOD driver to the new driver. Password login keeps working throughout (`authselect` `with-fingerprint` falls back to the password).

- [ ] **Step 1: Write the install and uninstall scripts (research repo)**

Create `~/projects/cv2-research/devtools/install-dev.sh`:

```bash
#!/usr/bin/env bash
# Install the libfprint build with the cv2 driver into /usr/local and make
# fprintd use it. Revert with uninstall-dev.sh.
# Run as: pkexec bash devtools/install-dev.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root (pkexec)" >&2; exit 1; }
USER_NAME="${PKEXEC_UID:+$(id -nu "$PKEXEC_UID")}"; USER_NAME="${USER_NAME:-${SUDO_USER:-}}"; [[ -n $USER_NAME ]] || { echo "run with pkexec or sudo" >&2; exit 1; }
SRC="$(getent passwd "$USER_NAME" | cut -d: -f6)/projects/libfprint"

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
```

Create `~/projects/cv2-research/devtools/uninstall-dev.sh`:

```bash
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
```

Commit them:

```bash
cd ~/projects/cv2-research
git add devtools/install-dev.sh devtools/uninstall-dev.sh
git commit -m "devtools: install the cv2 build for fprintd and revert it

Co-Authored-By: Claude <noreply@anthropic.com>"
```

- [ ] **Step 2: Remove the TOD prints and the TOD driver (hardware, user)**

```bash
fprintd-delete "$USER"
pkexec bash ~/projects/cv2-fingerprint/uninstall-fedora.sh
rpm -q libfprint
```

`fprintd-delete` must run first, while the TOD driver can still delete its templates from the chip. Expected: the uninstall script ends with `Reverted to stock Fedora libfprint.` (it also removes the fprintd debug drop-in), and `rpm -q libfprint` shows the Fedora package.

- [ ] **Step 3: Install the new driver for fprintd (hardware, user)**

Run: `pkexec bash ~/projects/cv2-research/devtools/install-dev.sh`
Then check fprintd loads it:

```bash
fprintd-list "$USER"
sudo grep libfprint /proc/$(pidof fprintd)/maps | awk '{print $6}' | sort -u
```

Expected: `fprintd-list` names the device `Broadcom ControlVault2 (BCM5880) Fingerprint Reader` with no fingers enrolled; the maps line is `/usr/local/lib64/libfprint-2.so.2.0.0`.

- [ ] **Step 4: Run the manual checklist (hardware, user)**

Tick each in the task report:

- [ ] `fprintd-enroll -f right-index-finger` completes; `fprintd-verify` matches the right index and rejects another finger.
- [ ] `sudo -k; sudo true` accepts the right index.
- [ ] Lock the screen and unlock with the right index; log out and log in at GDM with it.
- [ ] Reboot; `fprintd-verify` still matches (the print survives on the chip and in fprintd's storage).
- [ ] Press Ctrl-C during `fprintd-enroll`, then run `fprintd-verify` — it works.
- [ ] Press Ctrl-C during `fprintd-verify`, then run it again — it works.
- [ ] Start `fprintd-verify`, suspend the laptop (`systemctl suspend`), resume, then run `fprintd-verify` again — it works.
- [ ] `fprintd-delete "$USER"` succeeds; `fprintd-list "$USER"` shows no fingers.
- [ ] Enroll the fingers you actually want to use for login.

If any item fails, collect `journalctl -u fprintd -b` (with `G_MESSAGES_DEBUG=all` added to the drop-in's `Environment=` line if more detail is needed), fix the driver with a test that reproduces the failure, and repeat.

To go back to the TOD stopgap at any point: `pkexec bash ~/projects/cv2-research/devtools/uninstall-dev.sh`, then `pkexec bash ~/projects/cv2-fingerprint/install-fedora.sh`.

---

### Task 12: Final review and merge-request draft

**Files:**
- Create (research repo): `docs/upstream-mr.md`
- Modify (research repo): `HANDOFF.md`

- [ ] **Step 1: Full clean build and test**

```bash
cd ~/projects/libfprint
rm -rf build && meson setup build -Ddoc=false && meson compile -C build
meson test -C build --print-errorlogs
scripts/uncrustify.sh && git diff --exit-code
```

Expected: all tests pass (same pre-existing failures as Task 2, if any); uncrustify leaves no diff.

- [ ] **Step 2: Review the branch against the spec**

Run: `git log --oneline master..cv2-driver` and `git diff master..cv2-driver --stat`.
Check, against the spec and the "Spec refinements" list in this plan: features, firmware gate and message, `(u)` print data, timeouts, cleanup order (`0x68`, `0x6d`, `0x04`), interface 0 only, no USB reset, the header comment's security statement. Note anything that differs.

- [ ] **Step 3: Draft the merge-request description (research repo)**

Create `~/projects/cv2-research/docs/upstream-mr.md`:

```markdown
# cv2: Add Broadcom ControlVault2 (BCM5880, 0a5c:5834) driver

Match-on-chip driver for the fingerprint function of the Dell ControlVault2
reader found in Latitude 7490 and similar machines.

- Features: enroll, verify, identify, delete. Prints store the chip's 32-bit
  template handle; the chip can't list its templates, so there is no
  list/clear.
- Uses USB interface 0 only; the smart-card interfaces are untouched.
- The protocol was worked out from USB captures only (no vendor code or
  binaries). Notes and captures: (add the research repo link here once it
  is published).
- Firmware: enrollment needs ControlVault2 firmware 00412015 or newer, which
  Dell ships in its ControlVault2 firmware package. On older firmware the
  driver refuses to enroll with an error saying so; verify, identify and
  delete still work. **Testers with older firmware are welcome.**
- Security: templates are matched on the chip and no biometric data crosses
  USB, but replies on this plaintext interface are not authenticated, as
  with other match-on-chip drivers.

Tests: `cv2-proto` unit tests built from captured bytes; umockdev recordings
`cv2` (enroll with a rejected sample, verify match/no-match, identify
match/no-match, delete) and `cv2-cancel` (cancel during an enrollment,
then a full enrollment).

Tested on: Dell Latitude 7490, Fedora 44, fprintd 1.94.5, firmware 00412015
(enroll, verify, identify, delete, sudo, GDM login, reboot, suspend,
cancellation).
```

- [ ] **Step 4: Update HANDOFF.md (research repo) and commit**

Set *Current system state* to the Task 11 setup (cv2 build in `/usr/local`, fprintd drop-in `cv2-dev.conf`, revert with `devtools/uninstall-dev.sh`), and *Where to resume* to "Driver implemented on `~/projects/libfprint` branch `cv2-driver`; next: publish the research repo if wanted, fork libfprint on GitLab, push the branch and open the MR using `docs/upstream-mr.md`."

```bash
cd ~/projects/cv2-research
git add HANDOFF.md docs/upstream-mr.md
git commit -m "Driver implemented; MR draft and handoff update

Co-Authored-By: Claude <noreply@anthropic.com>"
```

Pushing, forking and opening the merge request are outside this plan; ask the user before doing any of them.
