type: Added

- `lint-host-platform-marks` refuses a test that reads the process's own platform
  (`sys.platform`, `Host.system()`, `Platform.current()`) or resolves through the
  ambient PATH (`shutil.which(name)` with no `path=`) without saying so — the pattern
  that makes a test green in CI and red on a macOS developer machine. The sites that
  predate the rule are frozen in `scripts/lint/baseline/host_platform_marks.json`
  (49 files, 84 reads); the baseline only shrinks
  ([rule](scripts/lint/docs/host-platform-marks.meiva.okf.md)).
