# Dell ControlVault2 fingerprint reader: protocol research

Research behind an open-source [libfprint](https://gitlab.freedesktop.org/libfprint/libfprint)
driver for the fingerprint function of the **Dell ControlVault2** reader
(Broadcom BCM5880, USB `0a5c:5834`), as found in the Dell Latitude 7490 and
7480 (both tested).

**The driver itself is under review upstream:**
https://gitlab.freedesktop.org/libfprint/libfprint/-/merge_requests/672

This repository holds the protocol notes, the USB captures they are based on,
the probe and development tools, and the design and implementation notes.

## Start here

| File | What |
|---|---|
| [`docs/upstream-protocol.md`](docs/upstream-protocol.md) | Clean protocol specification (the version in the merge request) |
| [`docs/upstream-mr.md`](docs/upstream-mr.md) | The merge request description, which embeds that specification |
| [`protocol/PROTOCOL.md`](protocol/PROTOCOL.md) | Working protocol notes, with dates and how each fact was found |
| `protocol/captures/*.pcap` | `usbmon` captures of the vendor's Linux driver: enroll, verify, identify, delete; `08-tod-pr13-commit-delete.pcap` is the commit and delete with their attribute and authorization blocks (see [Provenance](#provenance)) |
| `enroll-full.pcap`, `enroll-usbmon*.txt` | Earlier enrollment captures (factory firmware) |
| [`cvtool.py`](cvtool.py) + [`run-cvtool.sh`](run-cvtool.sh) | Probe tool that sends single protocol commands (allow-listed); `cvtool.log` is its log |
| [`cvdump.py`](cvdump.py), [`devtools/cvraw.py`](devtools/cvraw.py) | Decoders for the captures |
| [`devtools/`](devtools) | Scripts to run the libfprint driver from a build tree, record its umockdev tests, install it for fprintd, and test PAM stacks (`pamtest.py`) |
| [`docs/kde-fingerprint-login.md`](docs/kde-fingerprint-login.md) | Fingerprint login on KDE Plasma: PAM setup, KWallet, upstream status |
| [`docs/superpowers/`](docs/superpowers) | Design spec, implementation plan and execution log (every decision and finding) |
| [`HANDOFF.md`](HANDOFF.md), [`CLAUDE.md`](CLAUDE.md) | Project status and working context |

## Things worth knowing

- **Firmware:** older factory firmware (seen: `00412001` on a 7490,
  `00047026` on a 7480) rejected every enrollment commit with status `0x24`
  when the commit's attribute and authorization blocks were empty, as Dell's
  Linux driver sends them. The open driver now sends the blocks Dell's
  Windows driver uses, and with them **`00412001` does everything with no
  firmware update**: enroll, verify, reject a wrong finger, delete (a
  Precision 3520, tested by a user,
  [report](https://github.com/grosa787/dell-controlvault2-fingerprint-linux/issues/3)).
  `00412015` works either way; `00047026` is untested with the blocks. If
  enrollment still fails with `0x24`, the firmware update is the fallback:
  - 4.12.015 ships only in Dell's *ControlVault2 Driver and Firmware*
    **4.12.11.15, A25** (driver ID NX3HH,
    [download](https://www.dell.com/support/home/en-us/drivers/driversdetails?driverid=nx3hh)).
    Dell lists it for some Rugged models only, but it flashed fine on a 7490
    and a 7480.
  - Dell's catalog shows 43NG7 (4.12.5.8, A21) as newest for the 7480, but it
    contains 4.12.001.
  - It installs from Windows; a VM works. Pass the reader through by **USB
    port**, not vendor/product ID: during the flash it re-enumerates twice as
    `0a5c:5831`.
- **After a suspend** the reader can enter a state where enrollment never
  finishes and nothing matches, with any driver. Command `0x82`, which Dell's
  Windows driver sends, restores it. The open driver sends it at every open.
- **Enrollment** sends `0x8a` (begin enrollment) before every capture, as
  Dell's Windows driver does; it completes in about 4 presses on 4.12.015.
- **No biometric data crosses USB.** Matching happens on the chip; the largest
  message in the captures is the 848-byte firmware version text.

## Provenance

Everything here comes from USB captures of the author's own machine, of the
vendor's Linux driver and Dell's Windows driver, and from single-command
probes. No vendor code, headers or binaries were used. The commit and delete
blocks were captured from Dell's Linux driver as patched by
[grosa787/dell-controlvault2-fingerprint-linux#13](https://github.com/grosa787/dell-controlvault2-fingerprint-linux/pull/13)
to send the values Dell's Windows driver uses (Windows sends them inside its
protected session). Windows captures are
not included, because they contain Dell's firmware.

Much of this work, including the protocol analysis, the driver, its tests and
these notes, was done with the help of Claude (Anthropic), with all hardware
testing on the author's reader.

Not affiliated with Dell or Broadcom. Use at your own risk.

## License

[CC0 1.0](LICENSE): public domain dedication. Use anything here for any purpose.
