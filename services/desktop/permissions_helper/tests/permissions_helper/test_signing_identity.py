from __future__ import annotations

import stat
from pathlib import Path

import pytest

from services.desktop.permissions_helper.tests.test_permissions_helper import (
    certificate_dr,
    fake_tools,
    recorded_argvs,
    sign_command,
    stage_bundle,
    stage_signing_identity,
    write_current_build_state,
)


# meiva:note
# These tests pin the two halves of the fix: a build always signs with the fixed
# certificate, and a build whose identity is missing stops instead of falling back
# to an ad-hoc signature, which would silently invalidate every desktop grant.
def test_first_build_provisions_the_identity_without_asking_anyone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle, signing_identity

    app = stage_bundle(monkeypatch, tmp_path, exe_present=False, identity=False)
    recorded = fake_tools(monkeypatch)

    assert lifecycle.build_and_sign() == (app, True)

    argv = sign_command(recorded)
    assert argv[argv.index("--sign") + 1] == signing_identity.IDENTITY_NAME
    assert signing_identity.keychain_path().is_file()
    assert stat.S_IMODE(signing_identity.password_path().stat().st_mode) == 0o600
    assert signing_identity.password_path().read_text().strip()
    readme = signing_identity.readme_path().read_text()
    assert str(signing_identity.password_path()) in readme
    assert "What breaks if it is lost" in readme

    argvs = recorded_argvs(recorded)
    assert any(argv[0] == "/usr/bin/openssl" and "req" in argv for argv in argvs)
    assert any(argv[:2] == ["/usr/bin/security", "create-keychain"] for argv in argvs)
    assert any(argv[:2] == ["/usr/bin/security", "set-key-partition-list"] for argv in argvs)


def test_build_never_falls_back_to_an_ad_hoc_signature(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    stage_bundle(monkeypatch, tmp_path, exe_present=False)
    recorded = fake_tools(monkeypatch)

    lifecycle.build_and_sign()

    for argv in recorded_argvs(recorded):
        if "--sign" in argv:
            assert argv[argv.index("--sign") + 1] != "-", argv


def test_a_deleted_identity_stops_the_build_instead_of_minting_a_new_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app = stage_bundle(monkeypatch, tmp_path, exe_present=True, identity=False)
    recorded = fake_tools(monkeypatch)
    write_current_build_state(app, "previous", dr=certificate_dr())

    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="is gone"):
        lifecycle.build_and_sign()
    assert not any(argv[0] == "/usr/bin/openssl" for argv in recorded_argvs(recorded))
    assert not any("--force" in argv for argv in recorded_argvs(recorded))


def test_a_keychain_without_the_certificate_is_reported_not_replaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle, signing_identity

    stage_bundle(monkeypatch, tmp_path, exe_present=False, identity=False)
    signing_identity.keychain_path().parent.mkdir(parents=True, exist_ok=True)
    signing_identity.keychain_path().write_bytes(b"keychain")
    signing_identity.password_path().write_text("test-password\n")
    recorded = fake_tools(monkeypatch, identity_name="Someone Else")

    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="no 'Meiva Local Signing'"):
        lifecycle.build_and_sign()
    assert not any(argv[0] == "/usr/bin/openssl" for argv in recorded_argvs(recorded))


def test_a_keychain_outside_the_search_list_is_appended_not_replaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle, signing_identity

    stage_bundle(monkeypatch, tmp_path, exe_present=False)
    other = str(tmp_path / "other.keychain-db")
    recorded = fake_tools(monkeypatch, search_list=[other])

    lifecycle.build_and_sign()

    repair = next(
        argv
        for argv in recorded_argvs(recorded)
        if argv[:4] == ["/usr/bin/security", "list-keychains", "-d", "user"] and "-s" in argv
    )
    assert repair[repair.index("-s") + 1 :] == [other, str(signing_identity.keychain_path())]


def test_a_keychain_listed_through_a_symlink_is_not_added_twice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    stage_bundle(monkeypatch, tmp_path, exe_present=False, identity=False)
    stage_signing_identity()
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    recorded = fake_tools(
        monkeypatch, search_list=(str(alias / "installed-helper" / "signing.keychain-db"),)
    )

    lifecycle.build_and_sign()

    assert not any("-s" in argv for argv in recorded_argvs(recorded) if "list-keychains" in argv)


def test_a_second_keychain_with_the_same_name_stops_the_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    stage_bundle(monkeypatch, tmp_path, exe_present=False)
    stale = tmp_path / "stale.keychain-db"
    stale.write_bytes(b"keychain")
    recorded = fake_tools(monkeypatch, search_list=(str(stale),), rival=True)

    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="ambiguous"):
        lifecycle.build_and_sign()
    assert not any("--force" in argv for argv in recorded_argvs(recorded))
