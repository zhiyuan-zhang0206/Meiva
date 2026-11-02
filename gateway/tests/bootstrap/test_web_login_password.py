# meiva:note
# The web login password over HTTP, against the real gateway app: set through `PUT /api/auth/password`
# with a human credential, the only thing a browser logs in with besides a one-time ticket, kept as a
# salted hash, and bound to every session it mints. Each test serves its own home (`unit_home`), so no
# record outlives it.

import hashlib
import stat
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

from base import config
from base.host.net.auth import bearer_header, cookie_name
from gateway.http.auth.login_password import MIN_PASSWORD_LENGTH, parse_record, verify_password
from gateway.http.auth.login_password_store import login_password_path
from gateway.tests.auth.gateway_auth import LOGIN_PASSWORD, login_with_ticket
from gateway.tests.auth.gateway_config import anonymous_gateway_client

# meiva:note
# A fixture value, not a credential.
_SECRET = "test-cluster-secret"  # noqa: S105
# meiva:note
# A fixture value, not a credential.
_NEXT_PASSWORD = "the-next-login-password"  # noqa: S105
_PATH = "/api/auth/password"


def _set(client: TestClient, password: str = LOGIN_PASSWORD) -> httpx2.Response:
    return client.put(_PATH, json={"password": password}, headers=bearer_header(_SECRET))


def _login(client: TestClient, password: str) -> httpx2.Response:
    return client.post("/api/auth/login", json={"password": password})


def _cookie_of(client: TestClient, password: str) -> str:
    response = _login(client, password)
    assert response.status_code == 200, response.text
    cookie = response.cookies[cookie_name()]
    client.cookies.clear()
    return cookie


# meiva:note
# (`GET /api/agents` status, `/api/auth/check` verdict) presenting only `cookie`.
def _presenting(client: TestClient, cookie: str) -> tuple[int, bool]:
    client.cookies.clear()
    headers = {"Cookie": f"{cookie_name()}={cookie}"}
    agents = client.get("/api/agents", headers=headers).status_code
    return agents, client.get("/api/auth/check", headers=headers).json()["authenticated"]


@pytest.fixture(autouse=True)
def served_home(unit_home: Path) -> Path:
    """Every test serves its own home, where the password record lands."""
    return unit_home


# meiva:note
# ── setting the password ──────────────────────────────────────────────────


def test_a_password_set_by_the_cluster_secret_logs_a_browser_in(served_home: Path) -> None:
    with anonymous_gateway_client(cluster_secret=_SECRET) as client:
        response = _set(client)
        assert response.status_code == 200 and response.json() == {"ok": True}
        cookie = _cookie_of(client, LOGIN_PASSWORD)
        assert _presenting(client, cookie) == (200, True)
        sessions = client.get("/api/auth/sessions", headers=bearer_header(_SECRET)).json()
    assert [row["id"] for row in sessions] == [cookie[-8:]]
    # meiva:note
    # A password session is its own credential: neither the secret's mint nor the runner's.
    assert cookie.startswith("password-")
    assert login_password_path(served_home).exists()


def test_the_password_is_kept_only_as_a_salted_hash_in_an_owner_only_file(
    served_home: Path,
) -> None:
    with anonymous_gateway_client(cluster_secret=_SECRET) as client:
        assert _set(client).status_code == 200

    path = login_password_path(served_home)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    raw = path.read_bytes()
    assert LOGIN_PASSWORD.encode() not in raw
    assert _SECRET.encode() not in raw
    assert verify_password(parse_record(raw), LOGIN_PASSWORD)
    # meiva:note
    # The password never reaches `.env`.
    assert not (served_home / ".env").exists()


# meiva:note
# The mint travels in the cookie, so it must give no offline handle on the password or
# on the record: an HMAC under the gateway's private key, never a plain digest.
def test_a_password_session_names_its_credential_only_through_a_keyed_mac(
    served_home: Path,
) -> None:
    with anonymous_gateway_client(cluster_secret=_SECRET) as client:
        _set(client)
        cookie = _cookie_of(client, LOGIN_PASSWORD)
    mac = cookie.partition(".")[0].removeprefix("password-")
    record_digest = hashlib.sha256(login_password_path(served_home).read_bytes()).hexdigest()
    unkeyed = hashlib.sha256(f"password\0{record_digest}".encode()).hexdigest()
    assert len(mac) == 32
    assert mac not in (record_digest[:32], unkeyed[:32])
    assert LOGIN_PASSWORD not in cookie


@pytest.mark.parametrize("password", ["", "short", "x" * (MIN_PASSWORD_LENGTH - 1)])
def test_a_short_or_empty_password_is_a_typed_422_and_writes_no_record(
    served_home: Path, password: str
) -> None:
    with anonymous_gateway_client(cluster_secret=_SECRET) as client:
        refused = _set(client, password)
        missing = client.put(_PATH, json={}, headers=bearer_header(_SECRET))

    assert refused.status_code == 422
    body = refused.json()
    assert body["code"] == "password_too_short" and body["retryable"] is False
    assert str(MIN_PASSWORD_LENGTH) in body["detail"]
    assert missing.status_code == 422 and missing.json()["code"] == "validation_error"
    assert not login_password_path(served_home).exists()


def test_a_refused_password_leaves_the_stored_one_and_its_sessions_alone() -> None:
    with anonymous_gateway_client(cluster_secret=_SECRET) as client:
        _set(client)
        cookie = _cookie_of(client, LOGIN_PASSWORD)
        assert _set(client, "short").status_code == 422
        assert _presenting(client, cookie) == (200, True)
        assert _login(client, LOGIN_PASSWORD).status_code == 200


def test_exactly_the_minimum_length_is_accepted() -> None:
    minimum = "x" * MIN_PASSWORD_LENGTH
    with anonymous_gateway_client(cluster_secret=_SECRET) as client:
        assert _set(client, minimum).status_code == 200
        assert _login(client, minimum).status_code == 200


def test_setting_the_password_needs_a_credential(served_home: Path) -> None:
    with anonymous_gateway_client(cluster_secret=_SECRET) as client:
        anonymous = client.put(_PATH, json={"password": LOGIN_PASSWORD})
        wrong = client.put(
            _PATH, json={"password": LOGIN_PASSWORD}, headers=bearer_header("not-the-secret")
        )
    assert anonymous.status_code == 401
    assert wrong.status_code == 401
    assert not login_password_path(served_home).exists()


# meiva:note
# ── logging in with it ────────────────────────────────────────────────────


def test_the_cluster_secret_is_not_a_login_password() -> None:
    with anonymous_gateway_client(cluster_secret=_SECRET) as client:
        _set(client)
        refused = _login(client, _SECRET)
    assert refused.status_code == 401
    assert refused.json()["code"] == "invalid_password"
    assert cookie_name() not in refused.cookies


def test_a_wrong_password_is_refused_like_the_secret_is() -> None:
    with anonymous_gateway_client(cluster_secret=_SECRET) as client:
        _set(client)
        wrong = _login(client, LOGIN_PASSWORD + "-nearly")
        secret = _login(client, _SECRET)
    assert (wrong.status_code, secret.status_code) == (401, 401)
    assert wrong.json()["code"] == secret.json()["code"] == "invalid_password"
    assert wrong.json()["detail"] == secret.json()["detail"]


def test_with_no_password_set_every_login_is_the_same_401_and_counts_against_the_limiter() -> None:
    attempts = config.settings.gateway.login_max_failures
    with anonymous_gateway_client(cluster_secret=_SECRET) as client:
        refused = [_login(client, secret) for secret in (_SECRET, LOGIN_PASSWORD, "")]
        for _ in range(attempts - len(refused)):
            assert _login(client, _SECRET).status_code == 401
        locked = _login(client, _SECRET)

    assert [response.status_code for response in refused] == [401, 401, 401]
    assert {response.json()["code"] for response in refused} == {"invalid_password"}
    assert locked.status_code == 429
    assert locked.json()["code"] == "login_rate_limited"


def test_with_no_password_set_a_ticket_still_opens_a_session() -> None:
    with anonymous_gateway_client(cluster_secret=_SECRET) as client:
        assert _login(client, _SECRET).status_code == 401
        redeemed = login_with_ticket(client, _SECRET)
        cookie = redeemed.cookies[cookie_name()]
        verdict = _presenting(client, cookie)

    assert redeemed.status_code == 200
    assert cookie.startswith("human-")
    assert verdict == (200, True)


# meiva:note
# ── setting it again ──────────────────────────────────────────────────────


def test_setting_a_new_password_ends_the_sessions_the_old_one_minted() -> None:
    with anonymous_gateway_client(cluster_secret=_SECRET) as client:
        _set(client)
        old = _cookie_of(client, LOGIN_PASSWORD)
        assert _presenting(client, old) == (200, True)

        assert _set(client, _NEXT_PASSWORD).status_code == 200

        assert _presenting(client, old) == (401, False)
        assert _login(client, LOGIN_PASSWORD).status_code == 401
        new = _cookie_of(client, _NEXT_PASSWORD)
        assert _presenting(client, new) == (200, True)
        listed = client.get("/api/auth/sessions", headers=bearer_header(_SECRET)).json()
    assert [row["id"] for row in listed] == [new[-8:]]


# meiva:note
# The record is written under a fresh salt, so its bytes and the digest the sessions bind
# to change even though the password does not.
def test_setting_the_same_password_again_still_ends_its_sessions() -> None:
    with anonymous_gateway_client(cluster_secret=_SECRET) as client:
        _set(client)
        old = _cookie_of(client, LOGIN_PASSWORD)
        _set(client)
        assert _presenting(client, old) == (401, False)
        assert _presenting(client, _cookie_of(client, LOGIN_PASSWORD)) == (200, True)


def test_a_password_session_can_set_the_password_and_ends_itself_doing_so() -> None:
    with anonymous_gateway_client(cluster_secret=_SECRET) as client:
        _set(client)
        cookie = _cookie_of(client, LOGIN_PASSWORD)
        client.cookies.clear()
        changed = client.put(
            _PATH,
            json={"password": _NEXT_PASSWORD},
            headers={"Cookie": f"{cookie_name()}={cookie}"},
        )
        assert changed.status_code == 200
        assert _presenting(client, cookie) == (401, False)
        assert _login(client, _NEXT_PASSWORD).status_code == 200


def test_a_ticket_session_and_the_secret_outlive_a_new_password() -> None:
    with anonymous_gateway_client(cluster_secret=_SECRET) as client:
        ticket_cookie = login_with_ticket(client, _SECRET).cookies[cookie_name()]
        client.cookies.clear()
        _set(client)
        _set(client, _NEXT_PASSWORD)

        assert _presenting(client, ticket_cookie) == (200, True)
        assert client.get("/api/agents", headers=bearer_header(_SECRET)).status_code == 200


# meiva:note
# ── a damaged record ──────────────────────────────────────────────────────


def test_a_damaged_record_fails_loudly_instead_of_reading_as_no_password(
    served_home: Path,
) -> None:
    with anonymous_gateway_client(cluster_secret=_SECRET, raise_server_exceptions=False) as client:
        _set(client)
        cookie = _cookie_of(client, LOGIN_PASSWORD)
        path = login_password_path(served_home)
        path.write_bytes(b"{not a record")
        path.chmod(0o600)

        login = _login(client, LOGIN_PASSWORD)
        checked = client.get("/api/auth/check", headers={"Cookie": f"{cookie_name()}={cookie}"})
        # meiva:note
        # The bearer needs no session mint: the secret keeps working while the file is repaired.
        bearer = client.get("/api/agents", headers=bearer_header(_SECRET))

    assert login.status_code == 500 and login.json()["code"] == "internal_error"
    assert checked.status_code == 500
    assert bearer.status_code == 200
