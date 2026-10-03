# cv2: Add Broadcom ControlVault2 (BCM5880, 0a5c:5834) driver

Match-on-chip driver for the fingerprint function of the Dell ControlVault2
reader found in the Latitude 7490 and similar machines.

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
- Device open sends a sensor reset (command 0x82, which Dell's Windows driver
  sends before each enrollment). After a suspend the reader was seen to get
  into a state where enrollment needed many more samples and nothing
  matched, with any Linux driver; 0x82 restores it and keeps stored
  templates.
  Cost: about 0.7 s added to every open (each fprintd claim). The command's
  exact meaning is unknown; it is used as observed from the Windows driver.
- There is no suspend/resume handler: an operation running at suspend is
  ended by libfprint, and the sensor reset at the next open recovers the
  chip (tested: suspend during verify, then verify).
- An enrollment that has not finished after 30 accepted samples fails with
  an error asking to clean the sensor, instead of prompting forever.
- Robustness: a brief touch (finger event with nonzero length) asks for
  another press; a print that is no longer on the chip doesn't block identify
  for the user's other prints (the chip fails the whole list, so the driver
  then matches the prints one at a time on the same capture).
- Security: templates are matched on the chip and no biometric data crosses
  USB, but replies on this plaintext interface are not authenticated, as
  with other match-on-chip drivers.

Tests: `cv2-proto` unit tests built from captured bytes; umockdev recordings
`cv2` (enroll, verify match/no-match, identify match/no-match, identify and
verify with a print deleted from the chip, delete) and `cv2-cancel` (cancel
during an enrollment, then a full enrollment). `cv2-cancel` runs serially:
it cancels a pending interrupt read, and the replay can stall under parallel
load.

Housekeeping: `0a5c:5834` is removed from the unsupported-device list in
`fprint-list-udev-hwdb.c`; the entry on the wiki's Unsupported-Devices page
also needs removing, or the next `sync-udev-hwdb` run will re-add it.

Tested on: Dell Latitude 7490, Fedora 44, fprintd 1.94.5, firmware 00412015
(enroll, verify, identify, delete, sudo, lock screen, GDM login, reboot,
suspend during verify, cancellation).
