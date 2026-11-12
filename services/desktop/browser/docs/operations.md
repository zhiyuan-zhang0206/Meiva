# Shared browser operations

The [shared browser owner](../../../docs/agent_runner_side/browser/browser/browser.md)
describes Chrome custody, the shared MCP upstream, native probes and teardown.
The [in-daemon MCP client](../../../../meiva/mcps/docs/mcps.md) owns client dispatch;
the operating choices below do not redefine either component.

- **Profile source — fresh vs. seeded from your daily Chrome**: the dedicated
  profile is normally created empty, so the agent signs in to every site itself.
  On the **first** `meiva start` on a browser-capable host, when the profile is
  still absent **and** a human is at the TTY, converge's `_ensure_browser`
  (`services/desktop/browser/profile.py:ensure_browser_profile`) offers to seed it by
  **copying your daily Chrome profile** (macOS `~/Library/Application Support/Google/Chrome`,
  Linux `~/.config/google-chrome`) into `$MEIVA_HOME/chrome-profile/` instead. Copying
  hands the agent your full logged-in identity (cookies, sessions, saved
  passwords, signed-in accounts) so it acts as you without a re-login — a security
  trade-off, so it is opt-in behind an explicit confirmation and the default is a
  fresh profile. The copy excludes lock/socket files (`Singleton*`) and
  regenerable caches, reports its size first, and refuses while Chrome is still
  running (copying live SQLite risks a corrupt import — quit Chrome and retry).
  **Guardrails**: any existing profile directory is never touched, including an
  empty or partial first copy (idempotent across restarts; prod's multi-GB logged-in
  profile survives every start); non-interactive
  paths (root revival, boot autostart, a `cli.fleet_update` start) never prompt and
  always take the fresh default; a host with no daily Chrome degrades silently to
  fresh.
  Converge selects the daily-profile candidate from its captured user session home and OS
  system. Provisioning checks that directory only after preserving any existing destination
  and skipping non-interactive starts; it does not resolve the user's home again.
- **Capability and readiness:** consult the [browser gating owner](../../../docs/agent_runner_side/browser/browser/gating.md).
  `meiva status` surfaces skipped capability reasons. On macOS, a waiting session
  needs the service account's active GUI login and readable login Keychain;
  resolve the reported prerequisite instead of unlocking or editing the profile
  through an agent. Set `MEIVA_BROWSER_ENABLED=false` when choosing to opt out.
- **Service-owned — don't start Chrome by hand**: the `meiva-browser`
  session is the single owner of the CDP port and `$MEIVA_HOME/chrome-profile/`. A
  manually-launched Chrome on that profile takes the singleton profile lock, so
  the daemon's Chrome forwards-then-exits and the session dies — `meiva
  status` cannot infer service ownership from a successful `/json/version`
  alone. The browser probe binds the listener to this home's profile and process
  identity; see the shared browser owner above for orphan reconciliation. The daemon guards the collision: `main()` probes the CDP
  port first and refuses with a clear message rather than exec'ing a second
  Chrome into the lock. To (re)take service ownership, stop the squatter, then
  `meiva start` (or let the next root health round revive the session once the port is
  free) — and the refusal message now names that remedy itself. When the squatter
  is one of *ours* (a Chrome left outside the session by a `SingletonLock`
  handoff), `meiva stop --mode force` sweeps it, so there is no pid hunt: it kills
  every Chrome running on this cluster's `--user-data-dir`. A Chrome on some other
  profile is deliberately left alone — it cannot be positively identified as ours,
  and the operator's own browser is the thing that must never be killed — so that
  one is still quit by hand.
- **Browser lifetime**: restart and `--keep-browser` stops preserve the detached
  Chrome process while stopping the `meiva-browser` keeper unit so `meiva-root`
  itself can stop and be replaced across updates; a
  cluster update (`cli.fleet_update down`) keeps or stops it as the operator
  chose with `--keep-browser|--stop-browser`, forwarded unchanged to each host's
  `meiva stop`; the fleet never infers it from the diff. `meiva stop` takes the
  same explicit `--keep-browser|--stop-browser` choice. The profile and its logins survive either choice. `meiva start`
  adopts a running cluster Chrome and relaunches a stopped one. Explicit force stop
  and cluster destroy additionally sweep owned Chrome processes outside the
  recorded session tree (`services/desktop/browser/orphan.py`).
- **Tabs survive a deliberate stop**: `meiva stop --stop-browser` records the
  open tabs to `<home>/run/browser-session.json` before the services phase
  signals the browser, and the next `meiva start` relaunches Chrome with
  `--restore-last-session`, re-applies the recorded pins and tab groups, and
  prints `browser session: restored N/M tabs; pin/group reapplied K/J`. `K/J`
  counts the recorded pins and grouped tabs that are **in place**, not the ones
  this pass changed — Chrome restores some of them itself, and a run that
  repaired nothing would otherwise report `0/J` for a correct layout and read
  as a failure. A crash
  never restores, and `--keep-browser` records nothing. The ledger holds URLs,
  titles, pins and group titles only — never cookies, tokens or headers. Do not
  reach for Chrome's "continue where you left off" preference instead: measured
  on Chrome 156 it restores an incomplete set and drops grouped tabs
  ([browser session restore](browser-session-restore.md)).
- **Page TTL survives restarts**: every page the agents' browser calls create has a
  record (owner, deadline, 24h default `MEIVA_CHROME_PAGE_DEFAULT_TTL_SECONDS`) in
  `<home>/run/browser-pages.json`, keyed by CDP target id and written at creation
  (isolated-context pages too), so a daemon restart (every cluster update, every
  upstream `chrome-devtools-mcp` death) loses none. The record is the basis for
  closing; the `Meiva #<id>` tab group only checks that the user has not taken the
  page (a page that was in its owner's group and no longer is stays open and loses its
  record). The first sweep runs 30 s after the daemon starts and closes nothing; later
  sweeps run every 10 minutes and close a page only when two consecutive sweeps found it
  due, re-check it on a fresh read right before the close, and count the close only when
  the target is gone. A page in an agent group with **no** record is neither adopted
  nor closed; the exceptions are a one-time migration of the pages in agent groups when the
  daemon starts with no ledger file (first run; deleting the file asks for it again;
  migrated pages are closed only by the sweep, never by an agent's release), and the
  inheritance of a record by the single restored page with the same owner and URL after
  a Chrome restart (only when no recorded page survived). If Chrome's tabs cannot be
  re-read before a close, the whole sweep ends. Read-only
  checks in the `browser-mcp` log: `page ledger migration adopted page ...` (once, first
  run), `page ledger passed the record of gone page ...` (after a Chrome restart), `N
  page(s) sit in an agent tab group with no ledger record` (the leak size; they are left
  alone), `TTL sweep closed page <target> (<origin>) for agent <id>` (the sweep working),
  `could not read Chrome's tabs and tab groups` (WARNING: nothing was closed), `could not
  close page ... (failure N)` (WARNING, rate-limited).
- **Upstream health and restarts**: the daemon exits (the root restarts it, every agent's
  browser connection reconnects) only for a stop or update, or when the upstream
  `chrome-devtools-mcp` is judged unusable (`UpstreamGuard`, `mcp_upstream.py`):
  the upstream process or connection is gone (at once); 3 watchdog pings in a row unanswered
  (ping every 30 s, 10 s timeout, retries 10 s apart: a wedge is found within at most 80 s
  of the last answered ping); a call that hit the 180 s read timeout while the probe that
  follows it goes unanswered (at once); or 2 read timeouts in a row with no call succeeding
  between them (at most 2 x (180 + 10) = 380 s after the first affected call). A single
  slow call (a slow navigation, lighthouse, a trace) whose upstream still answers fails
  alone with `UpstreamCallTimeoutError` and nobody else notices. Measured against 1.6.0
  (2026-11-12): pings are answered within 0.12 s during a 40 s navigation, a 40 s
  lighthouse audit and a 20 s `wait_for`. Every end logs `daemon exiting: <reason>` and
  emits the `browser_mcp_exit` event (`reason`: `stopped`, `upstream_exited`,
  `probes_unanswered`, `call_timeout_unanswered`, `call_timeouts_repeated`); count restarts
  by reason there, `stopped` being the expected update/stop. The upstream is started with
  `npx -y chrome-devtools-mcp@<pin>`, which resolves the package over the network when the
  npm cache lacks it; a start that hangs on the registry ends by the connect timeout.
- **Selected pages survive restarts**: each agent's selected page is saved as a CDP target id
  in `<home>/run/browser-selected-pages.json` whenever it changes (a page the agent
  created, or a tab it merely selected, matched to its target by URL). The agent's first call
  after a daemon restart finds that target in the new upstream's listing and re-selects it;
  a target that is gone (or a Chrome restart, which renews every target id) or whose URL is
  shared by another tab is dropped and the agent starts page-less, as before. A restored
  selection gives no right to close the page: only the records of the page ledger do. Log
  lines: `restored as <page>` / `selection is dropped`.
- **First login is the user's job**: the headed window opens on the host's
  desktop; **you** sign into the target sites (e.g. Google / Xiaohongshu) once — the
  agent does not (and cannot) log in for you. The dedicated profile persists the
  session across restarts.
- **Profile isolation**: `$MEIVA_HOME/chrome-profile/` is separate from your daily
  Chrome profile (isolated cookie jar; signing it into Google does not evict your
  daily profile's sessions). It holds real logins — any agent on the host acts as
  those identities, so log in only what is needed; use a separate account for
  hardest isolation.
- **Display**: needs a real display (fine on a macOS desktop host); the Chrome
  window is visible on that host's screen.
- **Verification is manual**: live-browser use (driving a real site) is checked by
  hand, like the other real-MCP-server integrations — not in CI.
