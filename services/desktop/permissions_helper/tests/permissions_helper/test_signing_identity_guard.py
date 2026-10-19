from __future__ import annotations

from pathlib import Path

import pytest

from services.desktop.permissions_helper.tests.test_permissions_helper import (
    TEST_CERT_SHA1,
    fake_tools,
    recorded_argvs,
)


# meiva:note
# A second self-signed certificate would change the helper's designated requirement and
# drop its Screen Recording and Accessibility grants -- the "I granted it and it asks
# again" failure this guard exists to prevent.
# meiva:link services/docs/agent_runner_side/permissions-helper/authorization.meiva.okf.md
def test_an_identity_hidden_from_the_search_list_is_never_replaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    keychain = tmp_path / "signing.keychain-db"
    keychain.write_bytes(b"\x00")
    recorded = fake_tools(
        monkeypatch,
        authority=None,
        identity_output="     0 valid identities found\n",
        keychain_identity_output=f'  1) {TEST_CERT_SHA1} "{lifecycle.CERT_CN}"\n',
    )

    with pytest.raises(lifecycle.PermissionsHelperBuildError) as excinfo:
        lifecycle.ensure_signing_cert(keychain=keychain)

    message = str(excinfo.value)
    assert "keychain search list" in message
    assert str(keychain) in message
    assert not any(argv[0] == "openssl" for argv in recorded_argvs(recorded))
    assert not any(argv[:2] == ["security", "import"] for argv in recorded_argvs(recorded))


def test_an_identity_absent_everywhere_is_still_provisioned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    keychain = tmp_path / "signing.keychain-db"
    keychain.write_bytes(b"\x00")
    recorded = fake_tools(
        monkeypatch, authority=None, identity_output="     0 valid identities found\n"
    )

    lifecycle.ensure_signing_cert(keychain=keychain)

    assert any(argv[0] == "openssl" for argv in recorded_argvs(recorded))
    assert any(argv[:2] == ["security", "import"] for argv in recorded_argvs(recorded))
