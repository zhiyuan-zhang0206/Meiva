---
type: doc
title: "Permissions Helper — launchd ownership and ad-hoc signing"
description: "Why the macOS permissions helper must be launchd-owned and what its ad-hoc signature means for TCC: grants follow the code hash, an unchanged helper keeps them, and any rebuild is a new identity the operator grants again by hand."
tags:
- services
- permissions-helper
- lifecycle
---

# Permissions Helper — launchd ownership and ad-hoc signing

## Why launchd + ad-hoc signing
- **launchd launch**: the helper must be its own responsible process to independently hold permission grants, rather than borrowing the grant from the terminal that launched it.
- **Ad-hoc signature, no certificate**: the helper is signed with `codesign --sign -` and the fixed bundle id `com.meiva.permissions-helper`. No certificate or keychain is involved, so signing needs no unlock and no ACL decision and works the same over SSH, headless, and on a fresh host.
- **Grants follow the code hash**: TCC identifies ad-hoc code by its code hash (the designated requirement is `cdhash H"..."`), so a grant belongs to one exact helper build. An unchanged binary keeps its grants across restarts, converges and updates.
- **Rebuild only on a content change**: the helper is rebuilt only when the content hash of its inputs (source, `Info.plist`, entitlements, locales and signing options; no certificate is part of it) differs from the hash recorded in the build state, or the installed bundle no longer verifies with `codesign --verify`. Checkout mtimes never trigger a rebuild.
- **A rebuild means granting again**: a rebuilt helper is a new code identity, so Screen Recording, Accessibility and every other helper grant must be given again by hand in System Settings (the OS forces a human click). When the designated requirement differs from the recorded one, converge says so on stderr.
- **Hardened runtime**: the helper is signed with `--options runtime` and no dyld or library-validation exception, so dyld ignores `DYLD_*` from the launchd session (for example `launchctl setenv`) and maps only platform libraries; without it an inserted library runs before `main` and the running image still satisfies `codesign -R`. Release admission requires the flag on the file and in the running image's kernel status (`csops`). The signing options are part of the build input hash, so a policy change is a new artifact and a new code identity.
- **AppleEvents entitlement**: the helper is signed with `com.apple.security.automation.apple-events` (`helper/helper.entitlements`), the entitlement that lets tccd build an attribution chain for helper-spawned `osascript` / AppleEvents children; without it such a request dies as `-1712` (exec) / `-609` (pty) before any dialog can appear. The entitlements file is part of the build input hash, so changing it is a new artifact and a new code identity.
- The first-time authorization in System Settings is a manual step (OS forces human click), repeated for each new code identity.


## Artifact and activation boundary

A loaded helper is never upgraded in place: a differing loaded job, or a stale
installed artifact whose home job is still loaded, whose plist is still
registered, or whose executable a live process still runs (resolved-path match),
causes refusal (`launchd_job.require_retired_helper`, checked before compilation
and again right before removal). After `meiva stop` retired the exact-home
job, the next start rebuilds the stale `$MEIVA_HOME/helper` artifact in place;
the rebuilt helper is a new code identity and its desktop grants are given
again. Compilation finishes before the old artifact is removed, so a host that
cannot compile keeps it. Descendants cannot replace their permission
ancestor. An artifact in the optional explicit artifact directory is
immutable: a reviewed replacement goes into a fresh directory. That directory is
host-local configuration, including for isolated previews; it may not overlap
the ordinary home or production helper directories.
