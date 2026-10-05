# cv2: Add Broadcom ControlVault2 (BCM5880, 0a5c:5834) driver

Match-on-chip driver for the fingerprint function of the Dell ControlVault2
reader (Broadcom BCM5880, USB `0a5c:5834`), as found in the Dell Latitude
7490 and Latitude 7480 (both tested; other Dell models with this USB ID
should behave the same). Until now this reader only worked through a proprietary
libfprint-tod module.

**Features:** enroll, verify, identify, delete. Prints store the chip's
32-bit template handle (`"(u)"`); the chip can't list its templates, so there
is no list/clear and fprintd's storage is the source of truth. Scan type
press, 4 enroll stages.

**Scope:** USB interface 0 only; the smart-card interfaces are untouched and
the device is never reset.

**Protocol:** worked out from USB captures only (no vendor code, headers or
binaries; see Provenance for where the commit and delete arguments come
from). The full write-up is below; the unit-test fixtures are bytes copied
from those captures, and `tests/cv2*/custom.pcapng` are complete captures of
the driver talking to the reader.
Research notes, all the raw captures, the probe tool and the design notes
are public at <this repository's GitHub URL>
(CC0).

### Behavior worth knowing

- **Firmware:** no firmware requirement is known. With empty commit
  arguments (as Dell's Linux driver sends them), factory firmware rejected
  every enrollment commit with `0x24` (seen: `00412001` on a 7490,
  `00047026` on a 7480). The driver sends the commit's attribute and
  authorization blocks the way Dell's Windows driver does, and with them
  `00412001` enrolls, matches and deletes with no update (tested on a
  Precision 3520 and a Latitude 7490, below); `00412015` works either way; `00047026` is
  untested with the blocks. The driver logs the firmware version at open
  (`0x39`). If a commit still fails with `0x24`, the error names the
  firmware and suggests the update: 4.12.015 ships only in Dell's
  *ControlVault2 Driver and Firmware* package **4.12.11.15**; older
  packages, including the one Dell's catalog lists as newest for some models
  (4.12.5.8), still contain 4.12.001. The package installs from Windows (a
  VM works; pass the reader through by USB port, since it re-enumerates as
  `0a5c:5831` while flashing). Download details are in the comments below.
- **Sensor reset at open:** after a suspend the reader was seen to enter a
  state where enrollment never finished and nothing matched, with any Linux
  driver. Command `0x82`, which Dell's Windows driver sends before each
  enrollment, restores it and keeps stored templates, so the driver sends it at
  every open. Cost: about 0.7 s per open. Its exact meaning is unknown.
- **Templates missing from the chip:** if any handle in a match list is not
  on the chip, the chip fails the whole list (`0x1b`). The driver then matches
  the prints one at a time on the same capture, skipping missing ones, so one
  stale print doesn't block the user's other fingers. A single missing print
  is reported as `DATA_NOT_FOUND`.
- **Brief touches** (a finger event with a nonzero length) get "please try
  again"; an enrollment that hasn't finished after 30 accepted samples fails
  with a message to clean the sensor.
- **No suspend handler:** libfprint ends an operation running at suspend; the
  sensor reset at the next open recovers the chip (tested: suspend during
  verify, then verify).
- **Security:** templates are matched on the chip and no biometric data
  crosses USB, but replies on this plaintext interface are not authenticated,
  as with other match-on-chip drivers.

### Tests

- `cv2-proto`: unit tests for the request builders, reply parser, status
  mapping, firmware error and helpers, using captured bytes.
- `cv2` (umockdev): rejecting a print without template data, enroll, verify
  match/no-match, identify match/no-match, identify with a print deleted from
  the chip, verify against a deleted print, delete.
- `cv2-cancel` (umockdev): cancel during an enrollment, then a full
  enrollment. Runs serially: it cancels a pending interrupt read, and
  umockdev's replay can stall on that under load (see the comments).
- `cv2-retries` (umockdev): a too-brief touch and a chip-rejected sample
  during enrollment, a too-brief touch during verify, identify with a
  missing print placed first in the gallery, and identify with every print
  missing (`DATA_NOT_FOUND`).

Tested on hardware:
- Dell Latitude 7490, Fedora 44, fprintd 1.94.5, firmware `00412015`:
  enroll, verify, identify, delete, sudo, lock screen, reboot, suspend during
  verify, Ctrl-C during enroll and verify. With the commit and delete
  arguments: enroll, verify, identify and delete, and deleting a template
  committed without them.
- Dell Latitude 7480, Zorin OS 18.1 (Ubuntu 24.04), firmware `00412015`
  (flashed from `00047026`), tested independently by a user of the reader
  ([report](https://github.com/grosa787/dell-controlvault2-fingerprint-linux/issues/3#issuecomment-5971060113)):
  open with `0x82` reset, enroll, verify match/no-match, too-brief touch
  retry, identify, delete, verify against a deleted print
  (`DATA_NOT_FOUND`), and coexistence with a template enrolled earlier by
  the vendor driver. This was an earlier revision, before the commit and
  delete arguments and the per-capture `0x8a`.
- Dell Precision 3520, firmware `00412001` (factory firmware that rejected
  commits without the blocks), tested independently by a user of the reader
  with the commit and delete arguments
  ([report](https://github.com/grosa787/dell-controlvault2-fingerprint-linux/issues/3#issuecomment-5984364510)):
  enroll (including the `0x85` and too-brief-touch retries), verify 3 of 3,
  rejecting a different finger, and delete with the authorization, with no
  firmware update.
- Dell Latitude 7490, Ubuntu 22.04, firmware `00412001` (the factory
  firmware the author's 7490 had before its update), tested independently
  by another user of the reader
  ([report](https://github.com/grosa787/dell-controlvault2-fingerprint-linux/issues/3#issuecomment-5984891551)):
  enroll (including the `0x85` retry), verify 2 of 3 (one miss, as is usual
  for this sensor), rejecting a different finger, and delete with the
  authorization, with no firmware update.

### Housekeeping

`0a5c:5834` is removed from the unsupported-device list in
`libfprint/fprint-list-udev-hwdb.c` and `data/autosuspend.hwdb` is
regenerated. The entry on the wiki's
[Unsupported-Devices](https://gitlab.freedesktop.org/libfprint/wiki/-/wikis/Unsupported-Devices)
page also needs removing, or the next `sync-udev-hwdb` will add it back.

### AI assistance

This driver was written with the help of Claude (Anthropic), working with me:
protocol analysis of my captures, the driver code and tests. Every protocol
claim comes from captures of my own hardware, and every behavior above was
checked on the reader. The commits carry a `Co-Authored-By` trailer. Happy to
answer questions or rework anything.

<details>
<summary><b>Protocol specification</b> (from USB captures)</summary>


This describes the plaintext host interface that the `cv2` driver uses for
the reader's fingerprint function.

#### Provenance

Everything here comes from **USB traffic captures** (Linux `usbmon`) of the
reader in a Dell Latitude 7490 owned by the author:

- the vendor's Linux driver (Broadcom TOD module) driven by fprintd: enroll,
  verify, identify, delete;
- Dell's Windows driver in a VM with the reader passed through: version query,
  enrollment, Windows Hello login. Windows uses a protected (encrypted)
  session; only its plaintext commands are used here;
- the vendor's Linux driver as patched by
  [grosa787/dell-controlvault2-fingerprint-linux#13](https://github.com/grosa787/dell-controlvault2-fingerprint-linux/pull/13)
  to send the commit and delete values Dell's Windows driver uses (Windows
  sends them inside its protected session): enroll, verify, delete;
- a small probe tool (Python, pyusb) that sends the commands seen in those
  captures, to test single behaviors (identify with several handles,
  deleted handles, brief touches, the version query, `0x82`).

No vendor code, headers or binaries were used. Command names describe the
**observed behavior**. Facts not yet observed are listed under
[Open questions](#open-questions) rather than guessed.

The merge request carries the evidence with it: the request and reply
fixtures in `tests/test-cv2-proto.c` are bytes copied from these captures, and
`tests/cv2*/custom.pcapng` are complete captures of the driver talking to the
reader.

Out of scope: the smart-card (CCID) interfaces, firmware update, and the
Windows driver's protected session.

#### Device

| | |
|---|---|
| USB ID | `0a5c:5834` "Broadcom Corp / 5880", USB 1.1 (full speed) |
| Interfaces | 0: class `0xFE` (fingerprint / ControlVault channel), 1–2: CCID smart card, 3: vendor `0xFF` |
| Interface 0 endpoints | bulk OUT `0x01`, bulk IN `0x81` (64-byte packets), interrupt IN `0x85` |
| Kernel driver | none; interface 0 is claimed from userspace |

##### Firmware

Command `0x39` returns the firmware version as newline-separated `KEY:VALUE`
lines in a kind-1 string parameter (796 bytes on `00412015`; the reply is 848
bytes, several USB packets). Relevant keys:

| Key | Observed values |
|---|---|
| `USH_CHIPID` | `05810211` |
| `USH_REL_UPGRADE_VER` | `00047026` (4.7.26, 2017; factory on a Latitude 7480) and `00412001` (4.12.001; factory on a 7490, also in Dell package 4.12.5.8): enrollment commit with empty arguments fails with `0x24` · `00412015` (4.12.015, Dell package 4.12.11.15): enrollment works either way |

With the vendor's commit arguments (see [Commands](#commands)), `00412001`
enrolls, matches and deletes (tested on a Precision 3520 and a Latitude
7490); `00047026` is
untested with them. The driver logs the version at open and does not gate
anything on it. Status `0x24` at commit gives an error that names the
firmware and suggests Dell's ControlVault2 firmware package.

#### Transport

Each command is a request/response exchange on interface 0:

1. The host writes the whole request to **bulk OUT `0x01`**.
2. The chip sends an 8-byte message on **interrupt IN `0x85`**: `u32 type`,
   `u32 length`.
   - `type = 0`: a reply of `length` bytes is ready.
   - `type = 3`: finger event, see [Capture](#capture-and-finger-events).
3. The host reads the reply from **bulk IN `0x81`**. The vendor driver reads
   with a 4196-byte buffer. The interrupt length can exceed the message
   (commit: interrupt 65, header 64, 65 bytes read), so the header's total
   length is authoritative.

One command is outstanding at a time.

#### Message format

All integers little-endian.

##### Header (0x2c bytes)

| Offset | Size | Request | Reply |
|---|---|---|---|
| 0x00 | 4 | `1` | `1` |
| 0x04 | 4 | total message length (header + parameters) | same |
| 0x08 | 2 | command ID | same command ID |
| 0x0a | 2 | flags (below) | request flags with bit 8 set (`0x0440` → `0x0540`) |
| 0x0c | 4 | `2` | `2` |
| 0x10 | 4 | session handle, or 0 (per command) | session handle |
| 0x14 | 4 | 0 | **status** (0 = success) |
| 0x18 | 16 | 0 | 0 |
| 0x28 | 4 | length of the parameter area | same |

Flags: `0x0440` for ordinary commands, `0x0442` for `0x66`, `0x2f` and `0x0a`,
`0x0040` for `0x82` (as the Windows driver sends it). Replies to failed
commands are header-only (0x2c bytes).

##### Parameters

The parameter area is a sequence of:

```
u32 kind | u32 length | data[length], zero-padded to a 4-byte boundary
```

| Kind | Observed use |
|---|---|
| 0 | fixed-size value or blob (handles, IDs) |
| 1 | string |
| 2 | variable-length block: the `0x2f` handle list, the `0x6e`/`0x0a` attribute and authorization blocks; otherwise empty |
| 3 | buffer, sent with length 0 in commit requests |

The open request declares `1:7` for `"myAppID"` but sends the terminating NUL
too (8 bytes). A request whose declared length doesn't match the bytes sent
is rejected with status `0x0d`.

#### Commands

| ID | Name (behavioral) | Header handle | Request parameters | Reply parameters (on success) |
|---|---|---|---|---|
| `0x02` | open session | 0 | `0:4 = 0x44`, `1:7 "myAppID\0"`, `1:8 "myUserID"`, `1:0` | `0:4` session handle (also in header +0x10) |
| `0x04` | close session | 0 | `0:4` session handle | — |
| `0x39` | get version | 0 | `0:4 = 0` | `1:n` version text |
| `0x82` | sensor reset | 0 | `0:4 = 0` (flags `0x0040`) | — (status 0 after ~0.7 s); see [Degraded state](#degraded-state-after-suspend) |
| `0x8a` | begin enrollment | 0 | `0:4 = 0` | — (sent before every enrollment capture; keeps the samples already accepted) |
| `0x66` | capture start | session | `0:4` session, `0:4 = 2`, `0:4` mode `0x23` | `0:20` capture ID |
| `0x68` | capture cancel | 0 | `0:4 = 0` | — (status 0 also with nothing pending) |
| `0x6c` | enrollment sample | session | `0:4` session, `0:20` capture ID, `2:0` | `0:4` done flag (low byte; upper bytes unrelated), `0:20` ID, `0:4` (0) |
| `0x6e` | commit enrollment | session | `0:4` session, `0:20` final ID, `2:8` attributes, `2:21` authorization, `3:0` | `3:0`, `0:4` **template handle** |
| `0x6d` | discard enrollment | 0 | `0:4 = 0` | — |
| `0x2f` | match | session | `0:4` session, `0:4 = 0x48`, `0:4 = 0x53e2`, `0:4 = 0`, `2:4n` template handles | `0:4` match (1/0), `0:4` matched template handle (0 if none) |
| `0x0a` | delete template | session | `0:4` session, `0:4` template handle, `2:21` authorization | — (~1.4 s) |

Attributes are `00 00 04 00 04 00 00 00`. Authorization is
`01 01 ff 00 00 00 0d 00 0c` followed by `"BroadcomWBF\0"`, 21 bytes. The
vendor's Linux driver sends both blocks empty; with those, firmware
`00412015` commits and deletes normally, older firmware rejects the commit
(`0x24`). With the authorization, delete also removes templates committed
without it. The meaning of the individual bytes is unknown.

Template handles are 32-bit values assigned by the chip at commit. They
persist across sessions, reboots and power loss, and must be stored by the
host with each print; the chip has no observed way to list them. The meaning
of the `0x2f` constants `0x48` and `0x53e2` is unknown; they were identical in
every capture.

##### Capture and finger events

After `0x66` returns a capture ID, the chip sends an interrupt message with
`type = 3` when a finger touches the sensor. The host then sends the sample
(`0x6c`) or match (`0x2f`) command.

- `length = 0`: a usable capture.
- `length = 7`: no usable capture. Seen on brief taps, slides and short
  presses; a press held for about a second gives `length = 0`. A `0x6c` or
  `0x2f` sent after it fails with `0x89`. The driver asks for another press
  and re-arms the capture (`0x68`, `0x66`).

There is no finger-removed event. The vendor driver starts the next capture
immediately after each sample; the chip raises one event per touch. An idle
capture does not time out (no event in 100 s without a touch).

#### Sequences

##### Enrollment

```
0x02 open                        → session
repeat:
    0x8a begin enrollment
    0x66 capture start (0x23)    → capture ID     (0x85: send 0x68, then 0x8a, retry once)
    wait for interrupt type 3
    0x6c sample(capture ID)      → status 0: done flag, ID
                                 → status 0x59: sample rejected, repeat
until done flag == 1
0x6e commit(final ID, attributes, authorization) → template handle
0x04 close
```

- Dell's Windows driver sends `0x8a` before every capture (it also opens a
  protected session per sample, which is out of scope); the vendor's Linux
  driver sends it once. The driver follows Windows. Both complete in 4
  samples on `00412015`.

- On the final sample, the ID returned by `0x6c` differs from the capture ID
  sent; the commit carries it. Earlier replies echo the capture ID.
- Firmware `00412015`: usually 4 accepted samples; occasionally a few more.
- Abort: `0x68` cancel, `0x6d` discard, `0x04` close (the vendor driver's
  sequence; the driver's cleanup uses the same).

##### Verify and identify

```
0x02 open                        → session
0x66 capture start (0x23)        → capture ID
wait for interrupt type 3
0x2f match(template handles)     → status 0: match flag, matched handle
0x68 capture cancel
0x04 close
```

- Match and no-match both return status 0. The first reply parameter is `1`
  (match) or `0`; the second is the matched handle, or 0.
- One `0x2f` with several handles performs identification: it returns the
  handle of the finger that matched. Tested with two enrolled fingers and an
  unenrolled one.
- The match takes about 0.2–0.35 s.
- The vendor driver also sends `0x66` with mode `0x48` after each match, which
  always fails with `0x47`; the driver skips it with no observed effect.
- **Deleted handles:** if any handle in the list is not on the chip, `0x2f`
  fails with `0x1b` for the whole list. The capture stays usable: sending
  `0x2f` again on the same capture (no new `0x66`) with only valid handles
  matches normally, and can be repeated. The driver uses this to match the
  prints one at a time, skipping missing ones, so one stale print doesn't
  block the user's other fingers. `0x2f` with no capture pending returns
  `0x89` for valid and deleted handles alike, so a handle can't be checked
  without a capture.
- With an empty handle list, `0x2f` returns `0x47`.

##### Delete

```
0x02 open
0x0a delete(template handle)     → status 0 (~1.4 s); 0x1b if not on the chip
0x04 close
```

#### Degraded state after suspend

After a suspend/resume, the reader was seen to enter a state where enrollment
needed many more accepted samples (10 to 38+, often never finishing, with a
repeating "3 accepted, 1 rejected" pattern), half-covered presses were
accepted, and verify never matched. This happened with the vendor's Linux
driver too. A reboot, a power-button drain with AC unplugged, `0x68`/`0x6d`
and cleaning the sensor did not help; Windows Hello still worked.

Sending `0x82` once, as the Windows driver does before each enrollment,
restored normal behavior (4-sample enrollment, matching). A second `0x82`
kept stored templates. The driver sends `0x82` at every open (about 0.7 s).

#### Status codes

| Code | Seen on | Observed meaning | Driver handling |
|---|---|---|---|
| `0x00` | all | success | — |
| `0x0d` | `0x6e` | malformed request; pending enrollment kept | protocol error |
| `0x1b` | `0x2f`, `0x0a` | template handle not on the chip | `DATA_NOT_FOUND`; identify falls back to one handle at a time |
| `0x24` | `0x6e` | firmware `00412001`, commit with empty arguments: rejected, enrollment consumed | `NOT_SUPPORTED` (firmware update suggested) |
| `0x47` | `0x2f`, `0x66` | `0x2f` with an empty handle list; `0x66` mode `0x48` after a match | no match |
| `0x59` | `0x6c` | sample rejected (poor or partial press) | retry |
| `0x85` | `0x66` | capture already pending (first `0x66` after another client exited) | `0x68` cancel, retry once |
| `0x89` | `0x6c`, `0x2f` | no usable capture (after a brief touch, or with no capture pending) | retry |
| `0x8d` | `0x6e` | no completed enrollment to commit | protocol error |

#### Security model

Match-on-chip: templates are stored and matched inside the chip, and no
fingerprint images or templates cross USB. Replies on this plaintext interface
are not authenticated, so the host trusts the reader's match result, as with
other match-on-chip drivers in libfprint. The Windows driver uses an encrypted
session instead, which is out of scope.

#### Open questions

1. Listing templates stored on the chip; slot limit; behavior when full.
2. Exact meaning of `0x82`, and whether it is needed at every open or only
   after power events (it is cheap enough to send every time).
3. Meaning of the `0x2f` constants `0x48`/`0x53e2`, of the `0x02` open
   parameters (`0x44`, app/user strings), of `0x66` parameter 2 (`2`), and of
   mode `0x48`.
4. Other finger-event lengths besides 0 and 7.
5. Behavior on firmware other than `00412001` and `00412015` (for example
   `00047026` with the commit's attribute and authorization blocks).
6. Meaning of the attribute and authorization bytes.

</details>
