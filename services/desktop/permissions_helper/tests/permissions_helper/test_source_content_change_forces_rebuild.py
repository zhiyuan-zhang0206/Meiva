from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

from base import paths
from base.clock.tests.manual_timers import ManualTimers
from base.sessions.tests.refuse_helper import refuse_helper
from base.sessions.tests.scripted_helper import ScriptedHelper, serve
from services.desktop.permissions_helper.tests.test_permissions_helper import (
    fake_helper as fake_helper,
)
from services.desktop.permissions_helper.tests.test_permissions_helper import (
    fake_tools,
    install_env,
    recorded_argvs,
    sign_command,
    stage_bundle,
    test_dr,
    write_current_build_state,
)


def test_source_content_change_forces_rebuild(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app = stage_bundle(monkeypatch, tmp_path, exe_present=True)
    recorded = fake_tools(monkeypatch)
    write_current_build_state(app, lifecycle.source_content_hash())
    lifecycle.SOURCE.write_text("// changed swift")

    before = (app / "Contents/MacOS/MeivaPermissionsHelper").read_bytes()
    with pytest.raises(RuntimeError, match="still in use"):
        lifecycle.build_and_sign()
    assert (app / "Contents/MacOS/MeivaPermissionsHelper").read_bytes() == before
    assert not any(c[0] == "swiftc" for c in recorded_argvs(recorded))


def test_locale_content_change_forces_rebuild(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app = stage_bundle(monkeypatch, tmp_path, exe_present=True)
    recorded = fake_tools(monkeypatch)
    write_current_build_state(app, lifecycle.source_content_hash())
    locale_file = lifecycle.LOCALES / "en.lproj" / "Localizable.strings"
    locale_file.write_text('"panel.title" = "Changed";')

    before = (app / "Contents/MacOS/MeivaPermissionsHelper").read_bytes()
    with pytest.raises(RuntimeError, match="still in use"):
        lifecycle.build_and_sign()
    assert (app / "Contents/MacOS/MeivaPermissionsHelper").read_bytes() == before
    assert not any(c[0] == "swiftc" for c in recorded_argvs(recorded))


def test_entitlements_content_change_forces_rebuild(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app = stage_bundle(monkeypatch, tmp_path, exe_present=True)
    recorded = fake_tools(monkeypatch)
    write_current_build_state(app, lifecycle.source_content_hash())
    lifecycle.ENTITLEMENTS.write_bytes(b"<plist><dict/></plist>")

    before = (app / "Contents/MacOS/MeivaPermissionsHelper").read_bytes()
    with pytest.raises(RuntimeError, match="still in use"):
        lifecycle.build_and_sign()
    assert (app / "Contents/MacOS/MeivaPermissionsHelper").read_bytes() == before
    assert not any(c[0] == "swiftc" for c in recorded_argvs(recorded))


def test_build_copies_locale_resources_into_the_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app = stage_bundle(monkeypatch, tmp_path, exe_present=False)
    fake_tools(monkeypatch)

    assert lifecycle.build_and_sign() == (app, True)
    for language in ("en", "zh-Hans"):
        source = lifecycle.LOCALES / (language + ".lproj") / "Localizable.strings"
        copied = app / "Contents" / "Resources" / (language + ".lproj") / "Localizable.strings"
        assert copied.is_file()
        assert copied.read_text() == source.read_text()


def test_missing_build_state_forces_rebuild(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app = stage_bundle(monkeypatch, tmp_path, exe_present=True)
    recorded = fake_tools(monkeypatch)

    before = (app / "Contents/MacOS/MeivaPermissionsHelper").read_bytes()
    with pytest.raises(RuntimeError, match="still in use"):
        lifecycle.build_and_sign()
    assert (app / "Contents/MacOS/MeivaPermissionsHelper").read_bytes() == before
    assert not any(c[0] == "swiftc" for c in recorded_argvs(recorded))


def test_current_build_is_neither_rebuilt_nor_re_signed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app = stage_bundle(monkeypatch, tmp_path, exe_present=True)
    recorded = fake_tools(monkeypatch)
    write_current_build_state(app, lifecycle.source_content_hash())

    assert lifecycle.build_and_sign() == (app, False)
    assert not any(
        c[0] == "swiftc" or (Path(c[0]).name == "codesign" and "--force" in c)
        for c in recorded_argvs(recorded)
    )


def test_bundle_that_fails_codesign_verification_is_rebuilt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app = stage_bundle(monkeypatch, tmp_path, exe_present=True)
    fake_tools(monkeypatch, verify_rc=1)
    write_current_build_state(app, lifecycle.source_content_hash())

    with pytest.raises(RuntimeError, match="still in use"):
        lifecycle.build_and_sign()


def test_content_hash_needs_no_signing_tool_or_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    stage_bundle(monkeypatch, tmp_path, exe_present=False)
    recorded = fake_tools(monkeypatch)

    assert len(lifecycle.source_content_hash()) == 64
    assert recorded == []


def test_build_signs_with_the_fixed_identity_and_the_declared_entitlements(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle, signing_identity

    app = stage_bundle(monkeypatch, tmp_path, exe_present=False)
    recorded = fake_tools(monkeypatch)

    assert lifecycle.build_and_sign() == (app, True)
    assert sign_command(recorded) == [
        "/usr/bin/codesign",
        "--force",
        "--sign",
        signing_identity.IDENTITY_NAME,
        "--keychain",
        str(signing_identity.keychain_path()),
        "--options",
        "runtime",
        "--timestamp=none",
        "--entitlements",
        str(lifecycle.ENTITLEMENTS),
        "--identifier",
        lifecycle.HELPER_BUNDLE_ID,
        str(app),
    ]
    assert not any("--deep" in argv for argv in recorded_argvs(recorded))
    argvs = recorded_argvs(recorded)
    assert argvs.index(sign_command(recorded)) > argvs.index(
        next(c for c in argvs if c[0] == "swiftc")
    )


def test_build_state_records_the_signed_code_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import json

    from services.desktop.permissions_helper import lifecycle

    app = stage_bundle(monkeypatch, tmp_path, exe_present=False)
    fake_tools(monkeypatch)

    lifecycle.build_and_sign()
    state = json.loads((app.parent / "build-state.json").read_text())
    assert state["source_hash"] == lifecycle.source_content_hash()
    assert state["dr"] == test_dr()


def test_first_build_does_not_warn_about_regranting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from services.desktop.permissions_helper import lifecycle

    stage_bundle(monkeypatch, tmp_path, exe_present=False)
    fake_tools(monkeypatch)

    lifecycle.build_and_sign()
    assert "need granting again" not in capsys.readouterr().err


def test_rebuild_with_a_new_code_identity_warns_that_grants_are_needed_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app = stage_bundle(monkeypatch, tmp_path, exe_present=False)
    write_current_build_state(
        app, "hash-of-the-previous-build", dr='identifier "old" and cdhash H"00"'
    )
    fake_tools(monkeypatch)

    assert lifecycle.build_and_sign() == (app, True)
    assert "need granting again" in capsys.readouterr().err


def test_codesign_failure_aborts_the_build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from services.desktop.permissions_helper import lifecycle

    app = stage_bundle(monkeypatch, tmp_path, exe_present=False)
    fake_tools(monkeypatch, sign_rc=1)

    with pytest.raises(lifecycle.PermissionsHelperBuildError, match=r"codesign failed \(1\)"):
        lifecycle.build_and_sign()
    assert not (app.parent / "build-state.json").exists()


def test_unreadable_designated_requirement_aborts_the_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app = stage_bundle(monkeypatch, tmp_path, exe_present=False)
    fake_tools(monkeypatch, dr_streams=(b"", b"Executable=/tmp/helper\n"))

    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="did not report a designated"):
        lifecycle.build_and_sign()
    assert not (app.parent / "build-state.json").exists()


def test_every_call_carries_its_declared_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    stage_bundle(monkeypatch, tmp_path, exe_present=False)
    recorded = fake_tools(monkeypatch)

    lifecycle.build_and_sign()
    assert recorded
    for call in recorded:
        assert call.timeout == lifecycle.TIMEOUTS_S[Path(call.argv[0]).name], call.argv


def test_swiftc_hang_fails_the_build_instead_of_blocking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    stage_bundle(monkeypatch, tmp_path, exe_present=False)
    recorded = fake_tools(monkeypatch, hang=("swiftc",))

    with pytest.raises(lifecycle.PermissionsHelperTimeoutError) as err:
        lifecycle.build_and_sign()
    assert f"{lifecycle.TIMEOUTS_S['swiftc']:.0f}s" in str(err.value)
    assert not any(Path(c[0]).name == "codesign" and "--force" in c for c in recorded_argvs(recorded))


def test_codesign_hang_fails_the_build_instead_of_blocking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app = stage_bundle(monkeypatch, tmp_path, exe_present=False)
    fake_tools(monkeypatch, hang=("/usr/bin/codesign",))

    with pytest.raises(lifecycle.PermissionsHelperTimeoutError) as err:
        lifecycle.build_and_sign()
    assert f"{lifecycle.TIMEOUTS_S['codesign']:.0f}s" in str(err.value)
    assert not (app.parent / "build-state.json").exists()


def test_hung_helper_job_is_unknown_while_build_probes_report_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import launchd_job, lifecycle

    def hang(cmd: list[str], *, timeout: float, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(lifecycle, "run_bounded", hang)
    monkeypatch.setattr(launchd_job, "run_bounded", hang)
    monkeypatch.setattr(paths, "meiva_home", lambda: Path("/x/.meiva-demo"))
    app = tmp_path / "MeivaPermissionsHelper.app"
    lifecycle.app_executable(app).parent.mkdir(parents=True)
    lifecycle.app_executable(app).write_bytes(b"\x00")

    with pytest.raises(subprocess.TimeoutExpired):
        lifecycle.is_loaded()
    assert lifecycle.is_valid_app(app) is False


def test_helper_ping_settles_until_cold_start_answers() -> None:
    from base.host.permissions_helper import client
    from services.desktop.permissions_helper import lifecycle

    replies: list[object] = [
        client.PermissionsHelperError("socket not ready"),
        {},
        {"pong": False},
        {"pong": True, "root_stop_intent_v1": True, "helper_shutdown_v1": True},
    ]
    timers = ManualTimers()

    def ping() -> dict[str, object]:
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        assert isinstance(reply, dict)
        return reply

    assert lifecycle.helper_answers_ping(serve(ScriptedHelper(ping=ping)), timers)
    assert timers.sleeps == [0.5, 0.5, 0.5]


@pytest.mark.parametrize("changed", [False, True])
def test_loaded_helper_changes_refuse_before_any_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, changed: bool
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app, run_calls, probe_calls = install_env(
        monkeypatch, tmp_path, loaded=True, matching_plist=not changed
    )
    path = lifecycle.plist_path()
    before = path.read_bytes() if path.exists() else None
    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="stop and unregister"):
        lifecycle.install_and_load(
            app, rebuilt=not changed, helper=refuse_helper, timers=ManualTimers()
        )
    assert (path.read_bytes() if path.exists() else None) == before
    assert run_calls == probe_calls == []


def test_unresponsive_loaded_helper_keeps_unknown_custody(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app, run_calls, probe_calls = install_env(monkeypatch, tmp_path, loaded=True)
    silent = ScriptedHelper(ping={"pong": False})
    timers = ManualTimers()
    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="custody is unknown"):
        lifecycle.install_and_load(app, rebuilt=False, helper=serve(silent), timers=timers)
    assert run_calls == probe_calls == []
    assert len(silent.calls) > 1 and set(silent.calls) == {"ping"}
    assert timers.sleeps == [0.5] * (len(silent.calls) - 1)


def test_unloaded_helper_bootstraps_and_must_answer_ping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app, run_calls, _ = install_env(monkeypatch, tmp_path, loaded=False, matching_plist=False)
    helper = ScriptedHelper(
        ping={
            "pong": True,
            "preflight_screen": True,
            "ax_trusted": True,
            "root_stop_intent_v1": True,
            "helper_shutdown_v1": True,
        }
    )

    lifecycle.install_and_load(app, rebuilt=False, helper=serve(helper), timers=ManualTimers())

    assert ["launchctl", "bootstrap", "gui/501", str(lifecycle.plist_path())] in run_calls
    plist = plistlib.loads(lifecycle.plist_path().read_bytes())
    assert plist["ProgramArguments"] == [str(app / "Contents" / "MacOS" / "MeivaPermissionsHelper")]
    assert plist["KeepAlive"] == {"SuccessfulExit": False}


@pytest.mark.parametrize("loaded", [False, True])
def test_helper_start_clears_shutdown_intent_only_after_native_job_absence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, loaded: bool
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app, commands, _ = install_env(monkeypatch, tmp_path, loaded=loaded)
    helper = serve(
        ScriptedHelper(ping={"pong": True, "root_stop_intent_v1": True, "helper_shutdown_v1": True})
    )
    marker = tmp_path / "run" / "meiva-root" / "helper-stopped"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_bytes(b"stopped\n")
    marker.chmod(0o600)
    if loaded:
        with pytest.raises(lifecycle.PermissionsHelperBuildError, match="retirement is incomplete"):
            lifecycle.install_and_load(app, rebuilt=False, helper=helper, timers=ManualTimers())
        assert marker.read_bytes() == b"stopped\n" and commands == []
    else:
        lifecycle.install_and_load(app, rebuilt=False, helper=helper, timers=ManualTimers())
        assert not marker.exists()
        assert [cmd[1] for cmd in commands] == ["bootstrap"]
