---
type: doc
title: Permissions Helper — Authorization Model
description: "The helper's two macOS TCC grants, the two planes each is read on (system record vs the running process), the helper-owned request path, and why the fixed signing certificate keeps the grants across rebuilds."
tags: []
---

# Authorization Model

The helper has two independent macOS TCC grants:

- **Screen Recording** authorizes `screencapture_region`, `screencapture_window` and window metadata; without it, the helper refuses captures before starting a child or SCK request.
- **Accessibility** authorizes `click`, `drag`, `move`, `focus_app`, `type`, `key`, `scroll`, `ax_window_info`, `ax_tree`, and `ax_act`; without it, macOS silently drops synthetic input and denies accessibility-tree reads.

`ping` reports both facts as `preflight_screen` and `ax_trusted`. The Swift dispatch gate refuses every Accessibility-gated operation with an explicit error when `ax_trusted=false`. Each of the two desktop dialogs is raised at most once per helper process -- the first caller that needs the grant spends the one ask, and every later caller only probes the system and reports the grant as state; a dialog steals focus, so re-asking on a timer turns one missing grant into a stream of interruptions. Requests never wait for human response. Converge preflights both grants with `_ensure_screen_capture` and `_ensure_accessibility`, then agent startup reports either unavailable axis (or one combined notice when both fail).

`request_grants` (advertised as `ping.request_grants_v1`) asks for the two system dialogs this identity can still get -- `CGRequestScreenCaptureAccess()` for Screen Recording and `AXIsProcessTrustedWithOptions` for Accessibility -- through the same once-per-process gates, and returns both grants as they stand afterwards. It runs inside the helper on purpose: a child process inherits its launch chain's Accessibility trust, so a prompt raised there is answered for an identity that is not the one that will use the grant. A request that finds the gate already spent returns the current booleans and shows nothing, so the caller must read the grant back rather than treat the request as consent.

TCC keys grants on the helper's designated requirement, and for a helper signed with the fixed certificate that requirement names the certificate, not the binary. An unchanged helper keeps both grants across restarts, and so does a rebuild; replacing or losing the certificate is what makes the operator re-grant both in System Settings by hand. Accessibility applies to the already running helper immediately. A changed Screen Recording grant may require an externally coordinated stop and helper restart. A descendant must not kickstart its own ancestor.
