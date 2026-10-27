from __future__ import annotations

import plistlib
import subprocess
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from base.clock.tests.manual_timers import ManualTimers
from base.sessions.tests.refuse_helper import refuse_helper
from base.sessions.tests.scripted_helper import ScriptedHelper, serve
from services.desktop.permissions_helper import launchd_job
from services.desktop.permissions_helper.launchd_job import helper_job_loaded
from services.desktop.permissions_helper.tests.support import launchd_facts
from services.desktop.permissions_helper.tests.test_permissions_helper import (
    fake_helper as fake_helper,
)
from services.desktop.permissions_helper.tests.test_permissions_helper import (
    fake_tools,
    install_env,
    recorded_argvs,
    sample_dr,
    sign_command,
    stage_bundle,
    write_current_build_state,
)


def test_source_content_change_forces_rebuild(
    tmp_path: Path,
) -> None:
    from services.desktop.permissions_helper import lifecycle

    bundle = stage_bundle(tmp_path, exe_present=True)
    recorded, env = fake_tools(bundle)
    write_current_build_state(bundle.app, bundle.source_hash())
    bundle.sources.source.write_text("// changed swift")

    before = (bundle.app / "Contents/MacOS/MeivaPermissionsHelper").read_bytes()
    with pytest.raises(RuntimeError, match="still in use"):
        lifecycle.build_and_sign(destination=None, env=env)
    assert (bundle.app / "Contents/MacOS/MeivaPermissionsHelper").read_bytes() == before
    assert not any(c[0] == "swiftc" for c in recorded_argvs(recorded))


def test_locale_content_change_forces_rebuild(
    tmp_path: Path,
) -> None:
    from services.desktop.permissions_helper import lifecycle

    bundle = stage_bundle(tmp_path, exe_present=True)
    recorded, env = fake_tools(bundle)
    write_current_build_state(bundle.app, bundle.source_hash())
    locale_file = bundle.sources.locales / "en.lproj" / "Localizable.strings"
    locale_file.write_text('"panel.title" = "Changed";')

    before = (bundle.app / "Contents/MacOS/MeivaPermissionsHelper").read_bytes()
    with pytest.raises(RuntimeError, match="still in use"):
        lifecycle.build_and_sign(destination=None, env=env)
    assert (bundle.app / "Contents/MacOS/MeivaPermissionsHelper").read_bytes() == before
    assert not any(c[0] == "swiftc" for c in recorded_argvs(recorded))


def test_entitlements_content_change_forces_rebuild(
    tmp_path: Path,
) -> None:
    from services.desktop.permissions_helper import lifecycle

    bundle = stage_bundle(tmp_path, exe_present=True)
    recorded, env = fake_tools(bundle)
    write_current_build_state(bundle.app, bundle.source_hash())
    bundle.sources.entitlements.write_bytes(b"<plist><dict/></plist>")

    before = (bundle.app / "Contents/MacOS/MeivaPermissionsHelper").read_bytes()
    with pytest.raises(RuntimeError, match="still in use"):
        lifecycle.build_and_sign(destination=None, env=env)
    assert (bundle.app / "Contents/MacOS/MeivaPermissionsHelper").read_bytes() == before
    assert not any(c[0] == "swiftc" for c in recorded_argvs(recorded))


def test_build_copies_locale_resources_into_the_bundle(
    tmp_path: Path,
) -> None:
    from services.desktop.permissions_helper import lifecycle

    bundle = stage_bundle(tmp_path, exe_present=False)
    _, env = fake_tools(bundle)

    assert lifecycle.build_and_sign(destination=None, env=env) == (bundle.app, True)
    for language in ("en", "zh-Hans"):
        source = bundle.sources.locales / (language + ".lproj") / "Localizable.strings"
        copied = (
            bundle.app / "Contents" / "Resources" / (language + ".lproj") / "Localizable.strings"
        )
        assert copied.is_file()
        assert copied.read_text() == source.read_text()


def test_missing_build_state_forces_rebuild(
    tmp_path: Path,
) -> None:
    from services.desktop.permissions_helper import lifecycle

    bundle = stage_bundle(tmp_path, exe_present=True)
    recorded, env = fake_tools(bundle)

    before = (bundle.app / "Contents/MacOS/MeivaPermissionsHelper").read_bytes()
    with pytest.raises(RuntimeError, match="still in use"):
        lifecycle.build_and_sign(destination=None, env=env)
    assert (bundle.app / "Contents/MacOS/MeivaPermissionsHelper").read_bytes() == before
    assert not any(c[0] == "swiftc" for c in recorded_argvs(recorded))


def test_current_build_is_neither_rebuilt_nor_re_signed(
    tmp_path: Path,
) -> None:
    from services.desktop.permissions_helper import lifecycle

    bundle = stage_bundle(tmp_path, exe_present=True)
    recorded, env = fake_tools(bundle)
    write_current_build_state(bundle.app, bundle.source_hash())

    assert lifecycle.build_and_sign(destination=None, env=env) == (bundle.app, False)
    assert not any(
        c[0] == "swiftc" or (Path(c[0]).name == "codesign" and "--force" in c)
        for c in recorded_argvs(recorded)
    )


def test_bundle_that_fails_codesign_verification_is_rebuilt(
    tmp_path: Path,
) -> None:
    from services.desktop.permissions_helper import lifecycle

    bundle = stage_bundle(tmp_path, exe_present=True)
    _, env = fake_tools(bundle, verify_rc=1)
    write_current_build_state(bundle.app, bundle.source_hash())

    with pytest.raises(RuntimeError, match="still in use"):
        lifecycle.build_and_sign(destination=None, env=env)


def test_content_hash_needs_no_signing_tool_or_identity(
    tmp_path: Path,
) -> None:
    bundle = stage_bundle(tmp_path, exe_present=False)
    recorded, _ = fake_tools(bundle)

    assert len(bundle.source_hash()) == 64
    assert recorded == []


def test_build_signs_with_the_fixed_identity_and_the_declared_entitlements(
    tmp_path: Path,
) -> None:
    from services.desktop.permissions_helper import lifecycle, signing_identity

    bundle = stage_bundle(tmp_path, exe_present=False)
    recorded, env = fake_tools(bundle)

    assert lifecycle.build_and_sign(destination=None, env=env) == (bundle.app, True)
    assert sign_command(recorded) == [
        "/usr/bin/codesign",
        "--force",
        "--sign",
        signing_identity.IDENTITY_NAME,
        "--keychain",
        str(signing_identity.keychain_path(bundle.app_dir)),
        "--options",
        "runtime",
        "--timestamp=none",
        "--entitlements",
        str(bundle.sources.entitlements),
        "--identifier",
        lifecycle.HELPER_BUNDLE_ID,
        str(bundle.app),
    ]
    assert not any("--deep" in argv for argv in recorded_argvs(recorded))
    argvs = recorded_argvs(recorded)
    assert argvs.index(sign_command(recorded)) > argvs.index(
        next(c for c in argvs if c[0] == "swiftc")
    )


def test_build_state_records_the_signed_code_identity(
    tmp_path: Path,
) -> None:
    import json

    from services.desktop.permissions_helper import lifecycle

    bundle = stage_bundle(tmp_path, exe_present=False)
    _, env = fake_tools(bundle)

    lifecycle.build_and_sign(destination=None, env=env)
    state = json.loads((bundle.app.parent / "build-state.json").read_text())
    assert state["source_hash"] == bundle.source_hash()
    assert state["dr"] == sample_dr()
    assert state["signed_at"] == "2026-01-01T00:00:00+00:00"


def test_first_build_does_not_warn_about_regranting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from services.desktop.permissions_helper import lifecycle

    bundle = stage_bundle(tmp_path, exe_present=False)
    _, env = fake_tools(bundle)

    lifecycle.build_and_sign(destination=None, env=env)
    assert "need granting again" not in capsys.readouterr().err


def test_rebuild_with_a_new_code_identity_warns_that_grants_are_needed_again(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from services.desktop.permissions_helper import lifecycle

    bundle = stage_bundle(tmp_path, exe_present=False)
    write_current_build_state(
        bundle.app, "hash-of-the-previous-build", dr='identifier "old" and cdhash H"00"'
    )
    _, env = fake_tools(bundle)

    assert lifecycle.build_and_sign(destination=None, env=env) == (bundle.app, True)
    assert "need granting again" in capsys.readouterr().err


def test_codesign_failure_aborts_the_build(tmp_path: Path) -> None:
    from services.desktop.permissions_helper import lifecycle

    bundle = stage_bundle(tmp_path, exe_present=False)
    _, env = fake_tools(bundle, sign_rc=1)

    with pytest.raises(lifecycle.PermissionsHelperBuildError, match=r"codesign failed \(1\)"):
        lifecycle.build_and_sign(destination=None, env=env)
    assert not (bundle.app.parent / "build-state.json").exists()


def test_unreadable_designated_requirement_aborts_the_build(
    tmp_path: Path,
) -> None:
    from services.desktop.permissions_helper import lifecycle

    bundle = stage_bundle(tmp_path, exe_present=False)
    _, env = fake_tools(bundle, dr_streams=(b"", b"Executable=/tmp/helper\n"))

    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="did not report a designated"):
        lifecycle.build_and_sign(destination=None, env=env)
    assert not (bundle.app.parent / "build-state.json").exists()


def test_every_call_carries_its_declared_bound(
    tmp_path: Path,
) -> None:
    from services.desktop.permissions_helper import lifecycle

    bundle = stage_bundle(tmp_path, exe_present=False)
    recorded, env = fake_tools(bundle)

    lifecycle.build_and_sign(destination=None, env=env)
    assert recorded
    for call in recorded:
        assert call.timeout == lifecycle.TIMEOUTS_S[Path(call.argv[0]).name], call.argv


def test_swiftc_hang_fails_the_build_instead_of_blocking(
    tmp_path: Path,
) -> None:
    from services.desktop.permissions_helper import lifecycle

    bundle = stage_bundle(tmp_path, exe_present=False)
    recorded, env = fake_tools(bundle, hang=("swiftc",))

    with pytest.raises(lifecycle.PermissionsHelperTimeoutError) as err:
        lifecycle.build_and_sign(destination=None, env=env)
    assert f"{lifecycle.TIMEOUTS_S['swiftc']:.0f}s" in str(err.value)
    assert not any(
        Path(c[0]).name == "codesign" and "--force" in c for c in recorded_argvs(recorded)
    )


def test_codesign_hang_fails_the_build_instead_of_blocking(
    tmp_path: Path,
) -> None:
    from services.desktop.permissions_helper import lifecycle

    bundle = stage_bundle(tmp_path, exe_present=False)
    _, env = fake_tools(bundle, hang=("/usr/bin/codesign",))

    with pytest.raises(lifecycle.PermissionsHelperTimeoutError) as err:
        lifecycle.build_and_sign(destination=None, env=env)
    assert f"{lifecycle.TIMEOUTS_S['codesign']:.0f}s" in str(err.value)
    assert not (bundle.app.parent / "build-state.json").exists()


def test_hung_helper_job_is_unknown_while_build_probes_report_failure(
    tmp_path: Path,
) -> None:
    from services.desktop.permissions_helper import lifecycle

    def hang(
        cmd: Sequence[str], *, timeout: float, **_kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(list(cmd), timeout)

    app = tmp_path / "MeivaPermissionsHelper.app"
    lifecycle.app_executable(app).parent.mkdir(parents=True)
    lifecycle.app_executable(app).write_bytes(b"\x00")
    home = Path("/x/.meiva-demo")

    with pytest.raises(subprocess.TimeoutExpired):
        helper_job_loaded(home, facts=launchd_facts(home), run=hang, timers=ManualTimers())
    assert lifecycle.is_valid_app(app, run=hang) is False


def test_helper_ping_settles_until_cold_start_answers() -> None:
    from base.host.permissions_helper import client
    from services.desktop.permissions_helper import lifecycle

    replies: list[object] = [
        client.PermissionsHelperError("socket not ready"),
        {},
        {"pong": False},
        {"pong": True, "root_stop_intent_v1": True, "helper_shutdown_v1": True},
    ]

    def ping() -> dict[str, object]:
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        assert isinstance(reply, dict)
        return reply

    helper = ScriptedHelper(ping=ping)

    assert lifecycle.helper_answers_ping(serve(helper), timers=ManualTimers(), settle_seconds=0.0)
    assert helper.calls == ["ping"] * 4
    assert replies == []


@pytest.mark.parametrize("changed", [False, True])
def test_loaded_helper_changes_refuse_before_any_mutation(tmp_path: Path, *, changed: bool) -> None:
    from services.desktop.permissions_helper import lifecycle

    app, run_calls, probe_calls, env = install_env(
        tmp_path, loaded=True, matching_plist=not changed
    )
    path = launchd_job.helper_job_plist_path(env.build.home, facts=env.build.facts)
    before = path.read_bytes() if path.exists() else None
    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="stop and unregister"):
        lifecycle.install_and_load(app, rebuilt=not changed, helper=refuse_helper, env=env)
    assert (path.read_bytes() if path.exists() else None) == before
    assert run_calls == []
    assert probe_calls and probe_calls[0][:2] == ["launchctl", "print"]


def test_unresponsive_loaded_helper_keeps_unknown_custody(
    tmp_path: Path,
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app, run_calls, probe_calls, env = install_env(tmp_path, loaded=True)
    silent = ScriptedHelper(ping={"pong": False})
    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="custody is unknown"):
        lifecycle.install_and_load(
            app, rebuilt=False, helper=serve(silent), settle_seconds=0.0, env=env
        )
    assert run_calls == []
    assert probe_calls and probe_calls[0][:2] == ["launchctl", "print"]
    assert len(silent.calls) > 1 and set(silent.calls) == {"ping"}
    assert silent.calls == ["ping"] * 10


def test_unloaded_helper_bootstraps_and_must_answer_ping(
    tmp_path: Path,
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app, run_calls, _, env = install_env(tmp_path, loaded=False, matching_plist=False)
    env = replace(env, build=replace(env.build, facts=replace(env.build.facts, uid=777)))
    plist_path = launchd_job.helper_job_plist_path(env.build.home, facts=env.build.facts)
    helper = ScriptedHelper(
        ping={
            "pong": True,
            "preflight_screen": True,
            "ax_trusted": True,
            "root_stop_intent_v1": True,
            "helper_shutdown_v1": True,
        }
    )

    lifecycle.install_and_load(app, rebuilt=False, helper=serve(helper), env=env)

    assert ["launchctl", "bootstrap", "gui/777", str(plist_path)] in run_calls
    plist = plistlib.loads(plist_path.read_bytes())
    assert plist["ProgramArguments"] == [str(app / "Contents" / "MacOS" / "MeivaPermissionsHelper")]
    assert plist["KeepAlive"] == {"SuccessfulExit": False}


@pytest.mark.parametrize("loaded", [False, True])
def test_helper_start_clears_shutdown_intent_only_after_native_job_absence(
    tmp_path: Path, loaded: bool
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app, commands, _, env = install_env(tmp_path, loaded=loaded)
    helper = serve(
        ScriptedHelper(ping={"pong": True, "root_stop_intent_v1": True, "helper_shutdown_v1": True})
    )
    marker = env.build.home / "run" / "meiva-root" / "helper-stopped"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_bytes(b"stopped\n")
    marker.chmod(0o600)
    if loaded:
        with pytest.raises(lifecycle.PermissionsHelperBuildError, match="retirement is incomplete"):
            lifecycle.install_and_load(app, rebuilt=False, helper=helper, env=env)
        assert marker.read_bytes() == b"stopped\n" and commands == []
    else:
        lifecycle.install_and_load(app, rebuilt=False, helper=helper, env=env)
        assert not marker.exists()
        assert [cmd[1] for cmd in commands] == ["bootstrap"]
