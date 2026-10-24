# meiva:note
# services.desktop.browser.profile: the first-start choice between a fresh dedicated Chrome
# profile and one seeded from the operator's daily Chrome.
#
# The interactive orchestration (`ensure_browser_profile`) talks over a `Console` whose streams
# are `io.StringIO` here, and is wired through its module-bound seams (`profile_dir`,
# `default_chrome_user_data_dir`, `running_chrome_pid`, `copy_default_profile`). The pure
# machinery (exclusions, size accounting, running-Chrome detection, the copy itself) is tested
# against a real on-disk mini profile.

import io
import os
from pathlib import Path

import pytest

import services.desktop.browser.profile as bp
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import fake_host


# meiva:note
# A console whose input holds exactly these answers, one per line; asking for one more is an
# `EOFError`, so a path that must not prompt fails when it does.
def _console(*answers: str) -> bp.Console:
    return bp.Console(
        stdin=io.StringIO("".join(f"{answer}\n" for answer in answers)), stdout=io.StringIO()
    )


def _make_profile(root: Path) -> Path:
    """A minimal Chrome user-data dir: real state + junk that must be excluded."""
    (root / "Default").mkdir(parents=True)
    (root / "Default" / "Cookies").write_bytes(b"cookie-jar")
    (root / "Local State").write_text("{}")
    cache = root / "Default" / "Cache"
    cache.mkdir()
    (cache / "big").write_bytes(b"x" * 4096)
    (root / "SingletonLock").symlink_to("myhost-4242")
    return root


# --- pure helpers ----------------------------------------------------------


def test_profile_is_populated(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    assert bp.profile_is_populated(missing) is False
    empty = tmp_path / "empty"
    empty.mkdir()
    assert bp.profile_is_populated(empty) is False
    empty.joinpath("x").write_text("1")
    assert bp.profile_is_populated(empty) is True


def test_ignore_junk_drops_singletons_and_caches() -> None:
    names = ["Cookies", "Local State", "SingletonLock", "SingletonSocket", "Cache", "GPUCache"]
    assert bp.ignore_junk("/anywhere", names) == {
        "SingletonLock",
        "SingletonSocket",
        "Cache",
        "GPUCache",
    }


def test_dir_size_bytes_excludes_junk(tmp_path: Path) -> None:
    src = _make_profile(tmp_path / "chrome")
    # Only "Cookies" (10) + "Local State" (2) count; the 4096-byte Cache blob and
    # the SingletonLock symlink are excluded.
    assert bp.dir_size_bytes(src) == len(b"cookie-jar") + len("{}")


def test_running_chrome_pid_none_when_no_lock(tmp_path: Path) -> None:
    src = tmp_path / "chrome"
    src.mkdir()
    assert bp.running_chrome_pid(src) is None


def test_running_chrome_pid_none_when_lock_is_regular_file(tmp_path: Path) -> None:
    src = tmp_path / "chrome"
    src.mkdir()
    (src / "SingletonLock").write_text("not-a-symlink")  # readlink -> OSError
    assert bp.running_chrome_pid(src) is None


def test_running_chrome_pid_none_on_stale_lock(tmp_path: Path) -> None:
    src = tmp_path / "chrome"
    src.mkdir()
    (src / "SingletonLock").symlink_to("myhost-999999")  # pid not alive
    assert bp.running_chrome_pid(src) is None


def test_running_chrome_pid_none_on_unparseable_pid(tmp_path: Path) -> None:
    src = tmp_path / "chrome"
    src.mkdir()
    (src / "SingletonLock").symlink_to("myhost-notanumber")
    assert bp.running_chrome_pid(src) is None


def test_running_chrome_pid_live(tmp_path: Path) -> None:
    src = tmp_path / "chrome"
    src.mkdir()
    (src / "SingletonLock").symlink_to(f"myhost-{os.getpid()}")  # this process is alive
    assert bp.running_chrome_pid(src) == os.getpid()


def test_copy_default_profile_excludes_and_copies(tmp_path: Path) -> None:
    src = _make_profile(tmp_path / "chrome")
    dst = tmp_path / "agent-profile"
    bp.copy_default_profile(src, dst)
    assert (dst / "Default" / "Cookies").read_bytes() == b"cookie-jar"
    assert (dst / "Local State").exists()
    assert not (dst / "Default" / "Cache").exists()
    assert not (dst / "SingletonLock").exists()


def test_copy_default_profile_refuses_existing_dst_without_touching_it(tmp_path: Path) -> None:
    src = _make_profile(tmp_path / "chrome")
    dst = tmp_path / "agent-profile"
    dst.mkdir()
    cookies = dst / "Cookies"
    cookies.write_bytes(b"existing-login")
    with pytest.raises(FileExistsError):
        bp.copy_default_profile(src, dst)
    assert cookies.read_bytes() == b"existing-login"


def test_copy_default_profile_preserves_partial_dst_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _make_profile(tmp_path / "chrome")
    dst = tmp_path / "agent-profile"

    def _explode(*_a: object, **_k: object) -> None:
        dst.mkdir(exist_ok=True)  # simulate a partial copy having started
        raise OSError("disk full mid-copy")

    monkeypatch.setattr(bp.shutil, "copytree", _explode)
    with pytest.raises(OSError, match="disk full"):
        bp.copy_default_profile(src, dst)
    assert dst.exists()  # preserve every existing directory; never clear a profile automatically


def test_validate_local_state_warns_without_mutating_profile(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    before = sorted(profile.iterdir())
    warning = bp.validate_local_state(profile)
    assert warning is not None
    assert "missing" in warning
    assert sorted(profile.iterdir()) == before


def test_validate_local_state_accepts_readable_regular_file(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    state = profile / "Local State"
    state.write_text("{}")
    assert bp.validate_local_state(profile) is None


def test_validate_local_state_warns_when_mtime_is_unexpectedly_future_dated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    state = profile / "Local State"
    state.write_text("{}")
    monkeypatch.setattr(bp.time, "time", lambda: 0.0)
    os.utime(state, (301, 301))
    warning = bp.validate_local_state(profile)
    assert warning is not None
    assert "mtime" in warning


def test_human_size() -> None:
    assert bp.human_size(2 * 1024**3) == "2.0 GB"
    assert bp.human_size(512 * 1024**2) == "512 MB"


# --- ensure_browser_profile orchestration ----------------------------------


def test_skips_when_profile_path_already_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = tmp_path / "prof"
    existing.mkdir()  # An empty profile from a prior launch is also immutable.
    monkeypatch.setattr(bp, "profile_dir", lambda: existing)
    bp.ensure_browser_profile(
        interactive=True, host=fake_host(), console=_console()
    )  # returns without prompting


def test_skips_when_not_interactive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bp, "profile_dir", lambda: tmp_path / "absent")
    bp.ensure_browser_profile(interactive=False, host=fake_host(), console=_console())


def test_skips_when_no_daily_chrome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bp, "profile_dir", lambda: tmp_path / "absent")

    def fake_default_chrome_user_data_dir(*, host: Host) -> Path | None:
        return None

    monkeypatch.setattr(bp, "default_chrome_user_data_dir", fake_default_chrome_user_data_dir)
    bp.ensure_browser_profile(interactive=True, host=fake_host(), console=_console())


def _wire_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, Path]]:
    """Route a fresh dst + a discoverable daily Chrome, and record copy calls."""
    dst = tmp_path / "agent-profile"
    src = tmp_path / "daily-chrome"
    src.mkdir()
    monkeypatch.setattr(bp, "profile_dir", lambda: dst)

    def fake_default_chrome_user_data_dir(*, host: Host) -> Path | None:
        return src

    monkeypatch.setattr(bp, "default_chrome_user_data_dir", fake_default_chrome_user_data_dir)
    monkeypatch.setattr(bp, "running_chrome_pid", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(bp, "dir_size_bytes", lambda _s: 1234)  # pyright: ignore[reportUnknownArgumentType]
    calls: list[tuple[Path, Path]] = []
    monkeypatch.setattr(bp, "copy_default_profile", lambda s, d: calls.append((s, d)))  # pyright: ignore[reportUnknownArgumentType]
    return calls


def test_choice_new_does_not_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _wire_copy(tmp_path, monkeypatch)
    bp.ensure_browser_profile(interactive=True, host=fake_host(), console=_console("new"))
    assert calls == []


def test_choice_copy_confirmed_copies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _wire_copy(tmp_path, monkeypatch)
    answers = ["copy", "yes"]
    bp.ensure_browser_profile(interactive=True, host=fake_host(), console=_console(*answers))
    assert len(calls) == 1


def test_choice_copy_declined_at_confirm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _wire_copy(tmp_path, monkeypatch)
    answers = ["copy", "no"]
    bp.ensure_browser_profile(interactive=True, host=fake_host(), console=_console(*answers))
    assert calls == []


def test_copy_aborts_when_chrome_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _wire_copy(tmp_path, monkeypatch)
    monkeypatch.setattr(
        bp,
        "running_chrome_pid",
        lambda _s: 4242,  # pyright: ignore[reportUnknownArgumentType]
    )  # never quits
    answers = ["copy", "new"]  # choose copy, then bail out of the retry loop
    bp.ensure_browser_profile(interactive=True, host=fake_host(), console=_console(*answers))
    assert calls == []


def test_copy_proceeds_after_chrome_quits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _wire_copy(tmp_path, monkeypatch)
    pids = iter([4242, None])  # running on first probe, quit by the second
    monkeypatch.setattr(bp, "running_chrome_pid", lambda _s: next(pids))  # pyright: ignore[reportUnknownArgumentType]
    answers = ["copy", "", "yes"]  # copy; Enter to retry; confirm
    bp.ensure_browser_profile(interactive=True, host=fake_host(), console=_console(*answers))
    assert len(calls) == 1
