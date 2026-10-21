type: Added

- `@pytest.mark.requires_host("linux" | "darwin")` declares a test whose subject only
  exists on one host (launchd, TCC, codesign, swiftc); `tests/fixtures/host_platform.py`
  turns it into a skip on every other host with one consistent reason and still lists
  the test under `--collect-only`, so a CI lane can be held to running it. Everything
  else injects a fake `Host` (`base/host/system/tests/fake_commands.py:fake_host`) and
  asserts the same result on every machine
  ([platform-dependent tests](docs/conventions/testing.md#platform-dependent-tests)).
