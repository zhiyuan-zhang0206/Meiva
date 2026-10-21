type: Added

- The shared browser's tabs now survive a deliberate `meiva stop`/`meiva start`:
  stop records the open tabs (URL, title, pinned, tab-group title, order) to
  `<home>/run/browser-session.json`, start relaunches Chrome with
  `--restore-last-session` and re-applies the recorded pins and groups, then
  reports `browser session: restored N/M tabs; pin/group reapplied K/J`
  ([browser session restore](services/desktop/browser/docs/browser-session-restore.meiva.okf.md)).
  A crash, a watchdog respawn and `--keep-browser` all leave the browser alone.
