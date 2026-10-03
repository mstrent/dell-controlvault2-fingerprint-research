# Dell ControlVault2 fingerprint reader: protocol research

Research behind an open-source [libfprint](https://gitlab.freedesktop.org/libfprint/libfprint)
driver for the fingerprint function of the **Dell ControlVault2** reader
(Broadcom BCM5880, USB `0a5c:5834`), as found in the Dell Latitude 7490 (the
only machine tested).

**The driver itself is under review upstream:**
https://gitlab.freedesktop.org/libfprint/libfprint/-/merge_requests/672

This repository holds the protocol notes, the USB captures they are based on,
the probe and development tools, and the design and implementation notes.

## Start here

| File | What |
|---|---|
| [`docs/upstream-protocol.md`](docs/upstream-protocol.md) | Clean protocol specification (the version in the merge request) |
| [`protocol/PROTOCOL.md`](protocol/PROTOCOL.md) | Working protocol notes, with dates and how each fact was found |
| `protocol/captures/*.pcap` | `usbmon` captures of the vendor's Linux driver: enroll, verify, identify, delete |
| `enroll-full.pcap`, `enroll-usbmon*.txt` | Earlier enrollment captures (factory firmware) |
| [`cvtool.py`](cvtool.py) + [`run-cvtool.sh`](run-cvtool.sh) | Probe tool that sends single protocol commands (allow-listed); `cvtool.log` is its log |
| [`cvdump.py`](cvdump.py), [`devtools/cvraw.py`](devtools/cvraw.py) | Decoders for the captures |
| [`devtools/`](devtools) | Scripts to run the libfprint driver from a build tree, record its umockdev tests, and install it for fprintd |
| [`docs/superpowers/`](docs/superpowers) | Design spec, implementation plan and execution log (every decision and finding) |
| [`HANDOFF.md`](HANDOFF.md), [`CLAUDE.md`](CLAUDE.md) | Project status and working context |

## Things worth knowing

- **Firmware:** enrollment needs ControlVault2 firmware `00412015` or newer.
  Factory firmware (`00412001`) rejects every enrollment commit with status
  `0x24`. Dell's ControlVault2 driver/firmware package (for example
  [this one](https://www.dell.com/support/home/en-us/drivers/driversdetails?driverid=nx3hh))
  updates it from Windows. A Windows VM with the reader passed through worked.
- **After a suspend** the reader can enter a state where enrollment never
  finishes and nothing matches, with any driver. Command `0x82`, which Dell's
  Windows driver sends, restores it. The open driver sends it at every open.
- **No biometric data crosses USB.** Matching happens on the chip; the largest
  message in the captures is the 848-byte firmware version text.

## Provenance

Everything here comes from USB captures of the author's own machine, of the
vendor's Linux driver and Dell's Windows driver, and from single-command
probes. No vendor code, headers or binaries were used. Windows captures are
not included, because they contain Dell's firmware.

Much of this work, including the protocol analysis, the driver, its tests and
these notes, was done with the help of Claude (Anthropic), with all hardware
testing on the author's reader.

Not affiliated with Dell or Broadcom. Use at your own risk.

## License

[CC0 1.0](LICENSE): public domain dedication. Use anything here for any purpose.
