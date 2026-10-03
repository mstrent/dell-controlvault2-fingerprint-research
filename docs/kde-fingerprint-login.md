# Fingerprint at the KDE login screen: workaround and upstream status

Researched 2026-10-03. Fedora 44 with Plasma 6.7.5, `plasma-login-manager`
6.7.5, `polkit-kde` 6.7.5, fprintd 1.94.5, polkit 127.

## The underlying problem

A PAM stack runs one module at a time. With `pam_fprintd` before `pam_unix`
(authselect's `with-fingerprint` layout of `system-auth`), a typed password
sits unused until `pam_fprintd` gives up: 30 s by default, or 3 failed scans.
`pam_fprintd(8)` lists this under LIMITATIONS and says that applications have
to run separate stacks in parallel to offer both at once.

How each prompt behaves on stock Fedora KDE:

| Prompt | PAM service | Behavior |
|---|---|---|
| Lock screen | `kde` + `kde-fingerprint` | Two stacks in parallel. Password and finger both work at any time |
| sudo, polkit dialog, pkexec | `sudo`, `polkit-1` -> `system-auth` | Fingerprint first. A typed password waits for the fprintd timeout |
| Plasma Login greeter | `plasmalogin` -> `password-auth` | Password only. One sequential stack, no fingerprint |

## Local workaround: password first everywhere

A typed password is accepted at once. Pressing Enter on an empty field makes
`pam_unix` fail and falls through to the reader.

- Greeter: `/etc/pam.d/plasmalogin` overrides `/usr/lib/pam.d/plasmalogin`.
  `auth substack password-auth` becomes:

  ```
  auth        required      pam_env.so
  auth        required      pam_faildelay.so delay=2000000
  auth        [success=1 default=ignore] pam_unix.so nullok
  auth        requisite     pam_fprintd.so
  auth        required      pam_permit.so
  ```

  The final `pam_permit` is required. In `pam_authenticate`, a jump action
  (`success=1`) records no result. The modules after it here are all
  `optional`, so without `pam_permit` the stack falls back to Linux-PAM's
  initial status, and a correct password returns `PAM_PERM_DENIED` (6).
  `pam_permit` is reached only after `pam_unix` succeeded or `pam_fprintd`
  matched, because `requisite` ends the stack on a fingerprint failure. The
  `pam_kwallet` and `postlogin` lines after it still run.
- sudo, polkit, pkexec: authselect profile `custom/pwfirst`, created with
  `authselect create-profile pwfirst -b local --symlink-meta
  --symlink-nsswitch --symlink-dconf`. Its `system-auth` has the
  `pam_fprintd` line moved below `pam_unix`. Both are `sufficient`, so no jump
  is needed. It is selected with
  `with-fingerprint with-silent-lastlog with-mdns4`, and authselect took a
  backup named `before-pwfirst`. Undo:
  `authselect select local with-fingerprint with-silent-lastlog with-mdns4`.
- The KDE polkit dialog accepts an empty password field, so the
  fingerprint path works there too.
- `devtools/pamtest.py SERVICE ask|empty|wrong` checks a stack before logging
  out. It runs `pam_authenticate` as the current user and answers every
  prompt the way a greeter would.

### KWallet after a fingerprint login

`pam_kwallet` derives the wallet key from the password typed at login, using
`~/.local/share/kwalletd/kdewallet.salt`. A fingerprint login provides no
password, so a password-protected wallet prompts after login. Neither KWallet
nor the reader can avoid that: the reader reports only "template X matched"
and never sends key material over USB. Options considered:

1. Blank wallet password. **Chosen.** The wallet opens with no prompt. The
   cost is that, without disk encryption, its contents can be read from a
   pulled disk.
2. Seal the wallet password in the TPM (`systemd-creds`) and release it to
   `pam_kwallet` after `pam_fprintd` succeeds. This is the closest to how
   Windows does it, but it needs custom PAM glue, and any root process can
   unseal it.
3. Secrets stored on the ControlVault chip and released on a match. This is
   unexplored, and the protocol work here doesn't cover it.
4. Live with the prompt after login.

On disk encryption: AES-XTS benchmarks at about 1.5 GB/s on the i7-8650U,
against about 550 MB/s for a SATA SSD, so LUKS2 costs little speed here.
TPM2 auto-unlock (`systemd-cryptenroll`) plus fingerprint at the greeter is
the closest match to Windows (BitLocker plus Windows Hello). It is only as
strong as the boot chain, so it wants Secure Boot on.

## Upstream KDE work (status as of 2026-10-03)

This is part of KDE's Sovereign Tech Fund project
[kde-linux#665](https://invent.kde.org/kde-linux/kde-linux/-/work_items/665),
"Improve Security Infrastructure for Organisational Usage". The goals include
"extend support for non-password-based authentication methods on the login
screen", multi-factor login, Secure Boot and face login. Nearly all of the
work is by Harald Sitter (`sitter`). The plan is to turn kscreenlocker's sign-in
code (its PAM "authenticators") into a shared library that Plasma Login
Manager also uses, so every KDE sign-in prompt behaves the same way. See his
[KDE Discuss comment](https://discuss.kde.org/t/please-improve-plasma-login-manager-for-fingerprint-login/49747)
of 2026-08-27.

Merged, shipping in Plasma 6.8 (due 2026-10-14):

- [kscreenlocker !318](https://invent.kde.org/plasma/kscreenlocker/-/merge_requests/318)
  "pam: flexible authenticator support", with
  [plasma-workspace !6542](https://invent.kde.org/plasma/plasma-workspace/-/merge_requests/6542)
  and [plasma-desktop !3689](https://invent.kde.org/plasma/plasma-desktop/-/merge_requests/3689).
  - The lock screen gets an authentication-type selector (password, smartcard,
    face via howdy, U2F) with fingerprint as a secondary option alongside any
    of them.
  - Switching types cancels the running fingerprint check by sending
    `pam_fprintd` SIGINT.
  - It is enabled through `~/.config/kscreenlockerrc` `[Authenticators]`
    (`Fingerprint=true` and so on) plus `kde-face` and `kde-u2f` PAM services.
- Follow-ups to the sign-in worker processes:
  [kscreenlocker !361](https://invent.kde.org/plasma/kscreenlocker/-/merge_requests/361),
  [!363](https://invent.kde.org/plasma/kscreenlocker/-/merge_requests/363),
  [!364](https://invent.kde.org/plasma/kscreenlocker/-/merge_requests/364),
  [!368](https://invent.kde.org/plasma/kscreenlocker/-/merge_requests/368).

Open, mostly drafts targeted at Plasma 6.9:

| MR | What | State |
|---|---|---|
| [plasma-login-manager !202](https://invent.kde.org/plasma/plasma-login-manager/-/merge_requests/202) | Route the greeter's authentication through kscreenlocker's code. The description is just "seriously drafty draft" | Draft, milestone 6.9, last updated 2026-09-18 |
| [kscreenlocker !369](https://invent.kde.org/plasma/kscreenlocker/-/merge_requests/369) | `plasmauthentication` library: worker startup shared by the lock screen and an Arbiter (the login manager) | Draft, 6.9, 2026-09-11 |
| [kscreenlocker !358](https://invent.kde.org/plasma/kscreenlocker/-/merge_requests/358) | Arbiter interface: the trusted, root part of the login manager starts the worker and receives the result directly, so the unprivileged greeter can't fake a success | Open, 6.9, 2026-09-11 |
| [kscreenlocker !357](https://invent.kde.org/plasma/kscreenlocker/-/merge_requests/357) | Let the PAM worker run as root (the login manager needs this) | Open, tagged 6.8 but unmerged, 2026-09-09 |
| [plasma-login-manager !199](https://invent.kde.org/plasma/plasma-login-manager/-/merge_requests/199) | Start the greeter through `systemd-run`, a first step toward replacing the login manager's helper with systemd plus kscreenlocker's authenticators | Draft, no milestone, 2026-09-11 |
| [kscreenlocker !375](https://invent.kde.org/plasma/kscreenlocker/-/merge_requests/375) | More reliable detection of whether fingerprint will work | Open, 6.8 cherry-pick, 2026-09-22 |
| [develop-kde-org !826](https://invent.kde.org/documentation/develop-kde-org/-/merge_requests/826) | Admin docs page for the new, experimental PAM services | Open, 2026-09-11 |

Sitter on timeouts: `timeout=-1` on `pam_fprintd` is "largely snakeoil",
because fprintd suspends the sensor anyway to avoid overheating. The new UIs
have an idle state during which they suspend the sensor themselves.

Polkit dialog: not part of this work. The only related item is
[polkit-kde-agent-1 !63](https://invent.kde.org/plasma/polkit-kde-agent-1/-/merge_requests/63)
(another contributor, open). It adds a confirmation step when PAM succeeds
without prompting (for example, a passwordless account), and its author has
not tested it with a fingerprint reader. It does not affect the
password-first setup.

### Timing

[Plasma 6.9](https://community.kde.org/Schedules/Plasma_6): soft feature
freeze 2027-01-05, beta 2027-01-19, release 2027-02-23. The login-manager
draft depends on three unmerged kscreenlocker MRs, and its own description is
"seriously drafty draft", so slipping to 6.10 is plausible.

### When it lands

- Delete `/etc/pam.d/plasmalogin` before trying the new greeter. The override
  would otherwise put a second, sequential fingerprint check into the
  password path.
- Recheck whether the `kde-fingerprint`-style services the new code uses
  come from Fedora's `kde-settings` or need authselect changes.
- The `custom/pwfirst` profile for sudo and polkit stays useful until the
  polkit agent gets parallel authentication.

### Re-checking status

The invent.kde.org API is public for reads:

```
curl -s 'https://invent.kde.org/api/v4/merge_requests?author_username=sitter&scope=all&per_page=60&order_by=updated_at' \
  | python3 -c 'import json,sys
for m in json.load(sys.stdin): print(m["state"], m["updated_at"][:10], m["web_url"], "|", m["title"])'
curl -s 'https://invent.kde.org/api/v4/projects/plasma%2Fplasma-login-manager/merge_requests/202' \
  | python3 -c 'import json,sys; m=json.load(sys.stdin); print(m["state"], m["updated_at"], (m.get("milestone") or {}).get("title"))'
```
