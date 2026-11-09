# Panel mode

A socket-less launch of `MeivaPermissionsHelper.app` — no
`MEIVA_PERMISSIONS_HELPER_SOCKET`, no argv[1]; Finder double-click or
`open -n -a` — does not exit: it becomes the user-facing **panel instance**
(design v1 Phase B), launched as `--repo <checkout> [--helper-socket <path>]
[--tier L?]` with argv flags never mistaken for a socket path.

The panel renders the grant matrix from the onboarding tool's `--check`
report, offers a one-click fill-missing run driving
`scripts/host_ops/tcc/onboard-helper-grants.py`, and shows a live run log. A fill run
carries the same guard pair the CLI enforces (`--fill-pending
--confirm-user-present` — the button click is the user-present attestation),
and no automatic path fills: the launch-time run and the refresh button are
`--check` runs.

Panel copy is localized from `helper/locales/*.lproj` (en base, zh-Hans
alongside). `Info.plist` drops `LSBackgroundOnly` and keeps `LSUIElement` so
this instance may show UI; the launchd daemon path is unchanged.
