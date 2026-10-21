---
type: doc
title: "Permissions Helper — launchd ownership and a fixed signing identity"
description: "Why the macOS permissions helper must be launchd-owned and how one self-signed certificate keeps its TCC grants: the designated requirement names the certificate, so a rebuild is not a new identity and the operator grants once per host."
tags:
- services
- permissions-helper
- lifecycle
---

# Permissions Helper — launchd ownership and a fixed signing identity

## Why launchd + a fixed identity
- **launchd launch**: the helper must be its own responsible process to independently hold permission grants, rather than borrowing the grant from the terminal that launched it.
- **One fixed certificate**: the helper is signed with `Meiva Local Signing`, a self-signed certificate in `$MEIVA_HOME/helper/signing.keychain-db` (`services/desktop/permissions_helper/signing_identity.py`). Its keychain password is generated at provisioning time and kept beside it (`signing.keychain-pass`, owner-only), and a `set-key-partition-list` entry lets `codesign` use the key without a prompt, so signing works headless and over SSH from a GUI login session.
- **Grants follow the certificate**: the designated requirement is `identifier "com.meiva.permissions-helper" and certificate root = H"..."`. TCC keys a grant on that requirement, so a rebuild with the same certificate keeps every grant.
- **No ad-hoc fallback**: the build either signs with that identity or fails. An identity that exists but cannot sign is an error, and on a host whose installed helper was already certificate-signed a *missing* keychain is a deletion the build refuses to paper over -- minting a replacement would silently invalidate every grant.
- **Provisioned once per host**: with no recorded certificate requirement (a first build) the build provisions the identity itself with `security` and `openssl`, and writes `$MEIVA_HOME/helper/SIGNING-README.md` recording the exact commands and the password path. `scripts/host_ops/tcc/helper-signing.py` reports the identity (`status`), recreates it (`provision`), and moves a copy between hosts (`export` / `import`).
- **Rebuild only on a content change**: the helper is rebuilt only when the content hash of its inputs (source, `Info.plist`, entitlements, locales, signing options and the signing policy) differs from the hash recorded in the build state, or the installed bundle no longer verifies with `codesign --verify`. Checkout mtimes never trigger a rebuild.
- **A new certificate means granting again**: replacing the certificate, or losing the keychain, is a new code identity: Screen Recording, Accessibility and every other helper grant must be given again by hand in System Settings (the OS forces a human click). When the designated requirement differs from the recorded one, converge says so on stderr.
- **Hardened runtime**: the helper is signed with `--options runtime` and no dyld or library-validation exception, so dyld ignores `DYLD_*` from the launchd session (for example `launchctl setenv`) and maps only platform libraries; without it an inserted library runs before `main` and the running image still satisfies `codesign -R`. Release admission requires the flag on the file and in the running image's kernel status (`csops`). The signing options are part of the build input hash, so a policy change is a new artifact and a new code identity.
- **AppleEvents entitlement**: the helper is signed with `com.apple.security.automation.apple-events` (`helper/helper.entitlements`), the entitlement that lets tccd build an attribution chain for helper-spawned `osascript` / AppleEvents children; without it such a request dies as `-1712` (exec) / `-609` (pty) before any dialog can appear. The entitlements file is part of the build input hash, so changing it is a new artifact and a new code identity.
- The first-time authorization in System Settings is a manual step (OS forces human click) once per host per certificate; later rebuilds keep it.


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
