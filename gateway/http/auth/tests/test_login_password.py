# meiva:note
# The web login password as a stored record: the pure derivation, parsing and verification, and the
# file that keeps the record owner-only and is read once per revision.

import base64
import hashlib
import json
import random
import stat
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from gateway.http.auth.login_password import (
    MIN_PASSWORD_LENGTH,
    SALT_BYTES,
    PasswordRecordError,
    parse_record,
    password_problem,
    record_digest,
    render_record,
    verify_password,
)
from gateway.http.auth.login_password_store import LoginPasswords, login_password_path

# meiva:note
# A fixture value, not a credential.
_PASSWORD = "correct horse battery"  # noqa: S105
_SALT = bytes(range(SALT_BYTES))


# meiva:note
# A seeded generator gives the draw a test expects a name.
def _seeded(seed: int) -> random.Random:
    return random.Random(seed)  # noqa: S311


_FIELDS = TypeAdapter(dict[str, object])


def _fields(record: bytes) -> dict[str, object]:
    return _FIELDS.validate_json(record)


def _encoded(fields: dict[str, object]) -> bytes:
    return json.dumps(fields).encode()


# meiva:note
# ── the record ────────────────────────────────────────────────────────────


def test_a_record_verifies_its_password_and_no_other() -> None:
    record = parse_record(render_record(_PASSWORD, _SALT))
    assert verify_password(record, _PASSWORD)
    assert not verify_password(record, _PASSWORD + "x")
    assert not verify_password(record, _PASSWORD.upper())
    assert not verify_password(record, "")


def test_a_record_holds_no_plaintext_and_names_its_kdf() -> None:
    raw = render_record(_PASSWORD, _SALT)
    fields = _fields(raw)
    assert _PASSWORD.encode() not in raw
    assert fields["algorithm"] == "scrypt"
    assert (fields["n"], fields["r"], fields["p"]) == (32768, 8, 1)
    assert base64.b64decode(str(fields["salt"])) == _SALT
    assert _PASSWORD.encode() not in base64.b64decode(str(fields["hash"]))


def test_the_same_password_under_another_salt_is_another_record_with_another_digest() -> None:
    first = render_record(_PASSWORD, _SALT)
    second = render_record(_PASSWORD, bytes(reversed(_SALT)))
    assert first != second
    assert record_digest(first) != record_digest(second)
    assert verify_password(parse_record(second), _PASSWORD)


def test_a_digest_is_the_sha256_of_the_record_bytes() -> None:
    raw = render_record(_PASSWORD, _SALT)
    assert record_digest(raw) == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize(
    "password",
    [
        "utf8-\u043f\u0430\u0440\u043e\u043b\u044c-\u5bc6\u7801-\U0001f510",
        "\ud800" * MIN_PASSWORD_LENGTH,
    ],
)
def test_any_text_is_a_password_and_a_lone_surrogate_is_not_an_encoding_error(
    password: str,
) -> None:
    record = parse_record(render_record(password, _SALT))
    assert verify_password(record, password)
    assert not verify_password(record, password + "x")


@pytest.mark.parametrize(
    ("password", "refused"),
    [
        ("", True),
        ("x" * (MIN_PASSWORD_LENGTH - 1), True),
        ("x" * MIN_PASSWORD_LENGTH, False),
        ("\u5bc6" * (MIN_PASSWORD_LENGTH - 1), True),
        ("\u5bc6" * MIN_PASSWORD_LENGTH, False),
    ],
)
def test_a_password_needs_the_minimum_length_in_characters(password: str, refused: bool) -> None:
    assert (password_problem(password) is not None) is refused


def test_a_record_is_never_rendered_from_a_short_password_or_a_wrong_length_salt() -> None:
    with pytest.raises(ValueError, match="at least"):
        render_record("short", _SALT)
    with pytest.raises(ValueError, match="salt"):
        render_record(_PASSWORD, _SALT[:-1])


@pytest.mark.parametrize(
    ("changed", "dropped"),
    [
        pytest.param({"algorithm": "argon2"}, "", id="other-algorithm"),
        pytest.param({}, "salt", id="missing-salt"),
        pytest.param({"extra": 1}, "", id="extra-field"),
        pytest.param({"n": True}, "", id="boolean-cost"),
        pytest.param({"n": "32768"}, "", id="string-cost"),
        pytest.param({"p": 0}, "", id="zero-cost"),
        pytest.param({"n": 32767}, "", id="n-not-a-power-of-two"),
        pytest.param({"n": 1}, "", id="n-too-small"),
        pytest.param({"n": 2**30}, "", id="memory-beyond-the-bound"),
        pytest.param({"salt": "!!not-base64!!"}, "", id="salt-not-base64"),
        pytest.param({"salt": base64.b64encode(b"short").decode()}, "", id="short-salt"),
        pytest.param({"hash": base64.b64encode(b"short").decode()}, "", id="short-hash"),
        pytest.param({"hash": 7}, "", id="hash-not-a-string"),
    ],
)
def test_a_record_that_is_not_exactly_what_is_written_is_refused(
    changed: dict[str, object], dropped: str
) -> None:
    fields = {k: v for k, v in _fields(render_record(_PASSWORD, _SALT)).items() if k != dropped}
    with pytest.raises(PasswordRecordError):
        parse_record(_encoded(fields | changed))


@pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b"null", b'"scrypt"', b"{}"])
def test_bytes_that_are_not_a_record_object_are_refused(raw: bytes) -> None:
    with pytest.raises(PasswordRecordError):
        parse_record(raw)


def test_a_refusal_names_the_fault_and_none_of_the_stored_values() -> None:
    fields = _fields(render_record(_PASSWORD, _SALT))
    damaged = _encoded(fields | {"algorithm": "argon2"})
    with pytest.raises(PasswordRecordError) as refused:
        parse_record(damaged)
    assert "algorithm" in str(refused.value)
    assert str(fields["hash"]) not in str(refused.value)
    assert str(fields["salt"]) not in str(refused.value)


# meiva:note
# ── the file ──────────────────────────────────────────────────────────────


def test_setting_a_password_writes_an_owner_only_record_that_reads_back(tmp_path: Path) -> None:
    store = LoginPasswords(_seeded(3))
    assert store.current(tmp_path) is None
    store.set(tmp_path, _PASSWORD)

    path = login_password_path(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.name == "web-login-password.json"
    stored = store.current(tmp_path)
    assert stored is not None
    assert verify_password(stored.record, _PASSWORD)
    assert stored.digest == record_digest(path.read_bytes())


def test_the_salt_is_drawn_from_the_generator_the_store_was_given(tmp_path: Path) -> None:
    LoginPasswords(_seeded(3)).set(tmp_path, _PASSWORD)
    stored = LoginPasswords(_seeded(9)).current(tmp_path)
    assert stored is not None
    assert stored.record.salt == _seeded(3).randbytes(SALT_BYTES)


def test_a_revision_is_read_once_and_a_rewrite_is_read_again(tmp_path: Path) -> None:
    store = LoginPasswords(_seeded(3))
    store.set(tmp_path, _PASSWORD)
    first = store.current(tmp_path)
    # meiva:note
    # The same revision is not read again.
    assert store.current(tmp_path) is first

    # meiva:note
    # The same password under the next salt.
    store.set(tmp_path, _PASSWORD)
    second = store.current(tmp_path)
    assert second is not None and first is not None
    assert second.digest != first.digest
    assert verify_password(second.record, _PASSWORD)

    login_password_path(tmp_path).unlink()
    assert store.current(tmp_path) is None


def test_a_short_password_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least"):
        LoginPasswords(_seeded(3)).set(tmp_path, "short")
    assert not login_password_path(tmp_path).exists()


def test_a_damaged_record_raises_naming_its_file_and_is_not_read_as_unset(tmp_path: Path) -> None:
    path = login_password_path(tmp_path)
    path.write_bytes(b'{"algorithm": "scrypt"}\n')
    path.chmod(0o600)
    with pytest.raises(PasswordRecordError, match=r"web-login-password\.json"):
        LoginPasswords(_seeded(3)).current(tmp_path)


def test_a_record_others_can_read_is_refused(tmp_path: Path) -> None:
    store = LoginPasswords(_seeded(3))
    store.set(tmp_path, _PASSWORD)
    login_password_path(tmp_path).chmod(0o644)
    with pytest.raises(RuntimeError, match="not owner-only"):
        LoginPasswords(_seeded(3)).current(tmp_path)


def test_a_record_that_is_a_symlink_is_refused(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_bytes(render_record(_PASSWORD, _SALT))
    elsewhere.chmod(0o600)
    login_password_path(tmp_path).symlink_to(elsewhere)
    with pytest.raises(RuntimeError, match="symlink"):
        LoginPasswords(_seeded(3)).current(tmp_path)
