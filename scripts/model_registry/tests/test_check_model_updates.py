from __future__ import annotations

import json
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts.model_registry import check_model_updates

_SCRIPT = Path(check_model_updates.__file__)


def write_env_file(tracker: Any, path: Path, *, missing: str | None = None) -> None:
    path.write_text(
        "\n".join(
            f"{source.key_alias}=test-{source.provider}"
            for source in tracker.SOURCES.values()
            if source.key_alias != missing
        )
        + "\n"
    )


def known_models(tracker: Any, source: Any, *, models: dict[str, Any] | None = None) -> list[str]:
    catalog_models = tracker.build_model_catalog().models if models is None else models
    return [
        model_id for model_id, spec in catalog_models.items() if spec.provider == source.provider
    ][:1]


def stub_fetcher(
    tracker: Any,
    overrides: dict[str, list[str]] | None = None,
    *,
    models: dict[str, Any] | None = None,
) -> Callable[[Any, str], list[str]]:
    def fetch(source: Any, api_key: str) -> list[str]:
        assert api_key == f"test-{source.provider}"
        if overrides is not None and source.provider in overrides:
            return overrides[source.provider]
        return known_models(tracker, source, models=models)

    return fetch


def missing_environment_value(_alias: str) -> None:
    return None


class _JSONResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> object:
        return self._payload


def test_tracker_script_exists() -> None:
    assert _SCRIPT.is_file()


def test_fake_ip_range_boundaries(tracker: Any) -> None:

    assert tracker._is_fake_ip("198.18.0.0")
    assert tracker._is_fake_ip("198.19.255.255")
    assert not tracker._is_fake_ip("198.17.255.255")
    assert not tracker._is_fake_ip("198.20.0.0")
    assert not tracker._is_fake_ip("104.18.6.192")
    assert not tracker._is_fake_ip("not-an-ip")


def test_doh_real_ip_falls_back_to_second_endpoint(tracker: Any) -> None:
    host = "api.example.com"
    calls: list[str] = []

    def get(
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, str],
        timeout: int,
    ) -> _JSONResponse:
        assert headers == {"accept": "application/dns-json"}
        assert params == {"name": host, "type": "A"}
        assert timeout == 10
        calls.append(url)
        if url == tracker._DOH_ENDPOINTS[0]:
            raise tracker.requests.exceptions.ConnectionError("primary unavailable")
        return _JSONResponse({"Answer": [{"name": host, "type": 1, "data": "104.18.1.2"}]})

    assert tracker._doh_real_ip(host, http_get=get) == "104.18.1.2"
    assert calls == list(tracker._DOH_ENDPOINTS)


def test_doh_real_ip_skips_fake_ip_answers(tracker: Any) -> None:
    host = "api.example.com"
    calls: list[str] = []
    addresses = iter(["198.18.0.5", "104.18.1.2"])

    def get(
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, str],
        timeout: int,
    ) -> _JSONResponse:
        del headers, params, timeout
        calls.append(url)
        return _JSONResponse({"Answer": [{"name": host, "type": 1, "data": next(addresses)}]})

    assert tracker._doh_real_ip(host, http_get=get) == "104.18.1.2"
    assert calls == list(tracker._DOH_ENDPOINTS)


def test_doh_real_ip_raises_when_all_endpoints_fail(tracker: Any) -> None:
    calls: list[str] = []

    def get(url: str, **_kwargs: object) -> _JSONResponse:
        calls.append(url)
        raise tracker.requests.exceptions.ConnectionError("unavailable")

    with pytest.raises(ConnectionError, match="DoH"):
        tracker._doh_real_ip("api.example.com", http_get=get)
    assert calls == list(tracker._DOH_ENDPOINTS)


def test_doh_real_ip_is_cached(tracker: Any) -> None:
    host = "api.example.com"
    calls: list[str] = []

    def get(url: str, **_kwargs: object) -> _JSONResponse:
        calls.append(url)
        return _JSONResponse({"Answer": [{"name": host, "type": 1, "data": "104.18.1.2"}]})

    assert tracker._doh_real_ip(host, http_get=get) == "104.18.1.2"
    assert tracker._doh_real_ip(host, http_get=get) == "104.18.1.2"
    assert calls == [tracker._DOH_ENDPOINTS[0]]


def test_session_for_host_healthy_resolution_returns_none(tracker: Any) -> None:
    host = "api.example.com"

    def getaddrinfo(
        _host: str, _port: int, *, type: socket.SocketKind
    ) -> list[tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[str, int]]]:
        del type
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("104.18.1.2", 443))]

    assert tracker._session_for_host(host, addrinfo=getaddrinfo) is None


def test_session_for_host_fake_ip_returns_pinned_session(tracker: Any) -> None:
    host = "api.example.com"
    pinned_ip = "140.82.113.3"

    def getaddrinfo(
        _host: str, _port: int, *, type: socket.SocketKind
    ) -> list[tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[str, int]]]:
        del type
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.2.25", 443))]

    def doh_real_ip(_host: str) -> str:
        return pinned_ip

    session = tracker._session_for_host(host, addrinfo=getaddrinfo, doh_resolver=doh_real_ip)

    assert isinstance(session, tracker.requests.Session)
    adapter = session.get_adapter(f"https://{host}/")
    assert isinstance(adapter, tracker.PinnedIPHTTPSAdapter)
    assert adapter._pinned_ip == pinned_ip
    prepared = tracker.requests.Request("GET", f"https://{host}/models").prepare()
    pool = adapter.get_connection_with_tls_context(prepared, verify=True)
    connection = pool._new_conn()
    try:
        assert connection._dns_host == pinned_ip
        assert connection.host == host
    finally:
        connection.close()


def test_fetch_json_uses_pinned_session_when_fake_ip(tracker: Any) -> None:
    url = "https://api.example.com/models"
    headers = {"Authorization": "Bearer test-key"}
    params = {"limit": 100}
    payload: dict[str, object] = {"data": []}
    session_calls: list[tuple[str, dict[str, object]]] = []
    plain_calls: list[tuple[str, dict[str, object]]] = []

    class Session:
        def get(self, request_url: str, **kwargs: object) -> _JSONResponse:
            session_calls.append((request_url, kwargs))
            return _JSONResponse(payload)

    session = Session()

    def session_for_host(_host: str) -> Session:
        return session

    assert (
        tracker.fetch_json(url, headers=headers, params=params, session_for_host=session_for_host)
        == payload
    )
    assert session_calls == [
        (
            url,
            {
                "headers": {"User-Agent": tracker._USER_AGENT, **headers},
                "params": params,
                "timeout": tracker._TIMEOUT_SECONDS,
            },
        )
    ]

    def plain_get(request_url: str, **kwargs: object) -> _JSONResponse:
        plain_calls.append((request_url, kwargs))
        return _JSONResponse(payload)

    def no_session_for_host(_host: str) -> None:
        return None

    assert (
        tracker.fetch_json(
            url,
            headers=headers,
            params=params,
            session_for_host=no_session_for_host,
            http_get=plain_get,
        )
        == payload
    )
    assert plain_calls == session_calls


def test_candidate_is_reported_once_then_recorded_in_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    tracker: Any,
) -> None:
    env_file = tmp_path / ".env"
    write_env_file(tracker, env_file)
    fetcher = stub_fetcher(tracker, {"glm": ["glm-5.4"]}, models={})

    args = ["--env-file", str(env_file), "--state-dir", str(tmp_path / "state")]
    assert (
        tracker.main(
            args,
            env_lookup=missing_environment_value,
            model_fetcher=fetcher,
            models={},
        )
        == 2
    )
    assert "glm-5.4" in capsys.readouterr().out

    assert (
        tracker.main(
            args,
            env_lookup=missing_environment_value,
            model_fetcher=fetcher,
            models={},
        )
        == 0
    )
    assert "glm-5.4" not in capsys.readouterr().out.split("## Actionable candidates", 1)[-1]
    state = json.loads((tmp_path / "state" / "state.json").read_text())
    assert state["providers"]["glm"]["reported"] == ["glm-5.4"]


def test_check_sources_compares_against_the_provider_catalog(tmp_path: Path, tracker: Any) -> None:
    registered = type("Spec", (), {"provider": "gemini"})()
    models = {"gemini-3.5-flash-lite": registered}
    env_file = tmp_path / ".env"
    write_env_file(tracker, env_file)

    reports = tracker.check_sources(
        tracker._read_env_file(env_file),
        {"providers": {}},
        models=models,
        env_lookup=missing_environment_value,
        model_fetcher=stub_fetcher(
            tracker,
            {"gemini": ["gemini-3.5-flash-lite", "gemini-3.6-flash-lite"]},
            models=models,
        ),
    )

    assert reports["gemini"].candidates == ["gemini-3.6-flash-lite"]


def test_gpt6_family_ids_are_classified_not_dropped_as_other(tracker: Any) -> None:
    registry = {
        "gpt-6-astra": type("Spec", (), {"provider": "gpt"})(),
        "gpt-5.6-sol": type("Spec", (), {"provider": "gpt"})(),
    }

    comparison = tracker.compare_models(
        tracker.SOURCES["gpt"],
        ["gpt-6-astra", "gpt-6-sol"],
        registry,
    )

    assert comparison.candidates == ["gpt-6-sol"]
    assert comparison.suppressed == []
    assert comparison.other_ids == []
    assert comparison.series_models["gpt-6-sol"] == ["gpt-5.6-sol"]


def test_newer_same_series_is_actionable_while_older_upstream_member_is_suppressed(
    tracker: Any,
) -> None:
    registry = {
        "gemini-3.7-flash": type("Spec", (), {"provider": "gemini"})(),
    }

    comparison = tracker.compare_models(
        tracker.SOURCES["gemini"],
        ["gemini-3.6-flash", "gemini-3.8-flash"],
        registry,
    )

    assert comparison.candidates == ["gemini-3.8-flash"]
    assert comparison.suppressed == ["gemini-3.6-flash"]
    assert comparison.series_models["gemini-3.8-flash"] == ["gemini-3.7-flash"]


def test_gemini_flash_lite_ids_are_classified_not_dropped_as_other(tracker: Any) -> None:
    registry = {
        "gemini-3.5-flash-lite": type("Spec", (), {"provider": "gemini"})(),
        "gemini-flash-lite-latest": type("Spec", (), {"provider": "gemini"})(),
    }

    comparison = tracker.compare_models(
        tracker.SOURCES["gemini"],
        [
            "gemini-3.5-flash-lite",
            "gemini-3.6-flash-lite",
            "gemini-flash-lite-latest",
            "gemini-3.1-flash-lite-image",
        ],
        registry,
    )

    assert comparison.candidates == ["gemini-3.6-flash-lite"]
    assert comparison.suppressed == []
    assert comparison.series_models["gemini-3.6-flash-lite"] == ["gemini-3.5-flash-lite"]
    assert comparison.other_ids == ["gemini-flash-lite-latest", "gemini-3.1-flash-lite-image"]


def test_older_flash_lite_member_is_suppressed_while_preview_series_is_separate(
    tracker: Any,
) -> None:
    registry = {
        "gemini-3.5-flash-lite": type("Spec", (), {"provider": "gemini"})(),
    }

    comparison = tracker.compare_models(
        tracker.SOURCES["gemini"],
        ["gemini-3.1-flash-lite", "gemini-3.1-flash-lite-preview", "gemini-3.5-flash-lite"],
        registry,
    )

    assert comparison.candidates == ["gemini-3.1-flash-lite-preview"]
    assert comparison.suppressed == ["gemini-3.1-flash-lite"]
    assert comparison.series_models["gemini-3.1-flash-lite-preview"] == []


def test_qwen_major_only_variant_is_suppressed_but_same_version_variant_is_actionable(
    tracker: Any,
) -> None:
    registry = {
        "qwen3.8-max": type("Spec", (), {"provider": "qwen"})(),
    }

    comparison = tracker.compare_models(
        tracker.SOURCES["qwen"],
        ["qwen3-max-2025-09-23", "qwen3.8-2.4t-a95b"],
        registry,
    )

    assert comparison.candidates == ["qwen3.8-2.4t-a95b"]
    assert comparison.suppressed == ["qwen3-max-2025-09-23"]


def test_qwen_dated_snapshots_of_registered_aliases_are_suppressed(tracker: Any) -> None:
    registry = {
        "qwen3.8-max": type("Spec", (), {"provider": "qwen"})(),
    }

    comparison = tracker.compare_models(
        tracker.SOURCES["qwen"],
        [
            "qwen3.8-max-0902",
            "qwen3.8-max-2026-09-02",
            "qwen3.8-max-20260902",
            "qwen3.8-2.4t-a95b",
        ],
        registry,
    )

    assert comparison.candidates == ["qwen3.8-2.4t-a95b"]
    assert comparison.suppressed == [
        "qwen3.8-max-0902",
        "qwen3.8-max-2026-09-02",
        "qwen3.8-max-20260902",
    ]


def test_qwen_date_suffixes_with_unregistered_remainders_fall_through(tracker: Any) -> None:
    registry = {
        "qwen3.8-max": type("Spec", (), {"provider": "qwen"})(),
    }

    comparison = tracker.compare_models(
        tracker.SOURCES["qwen"],
        ["qwen3-max-2025-09-23", "qwen3.8-other-0902"],
        registry,
    )

    assert comparison.candidates == ["qwen3.8-other-0902"]
    assert comparison.suppressed == ["qwen3-max-2025-09-23"]


def test_openai_response_shape_drift_raises_instead_of_reading_no_models(
    tracker: Any,
) -> None:

    def malformed_response(
        url: str, *, headers: dict[str, str], params: dict[str, str | int]
    ) -> dict[str, str]:
        del url, headers, params
        return {"data": "not-a-list"}

    with pytest.raises(TypeError, match="must be a list"):
        tracker.fetch_provider_models(
            tracker.SOURCES["gpt"], "test-key", fetcher=malformed_response
        )


def test_missing_key_is_a_provider_error(tmp_path: Path, tracker: Any) -> None:
    env_file = tmp_path / ".env"
    write_env_file(tracker, env_file, missing="GLM_API_KEY")

    assert (
        tracker.main(
            ["--env-file", str(env_file), "--state-dir", str(tmp_path / "state")],
            env_lookup=missing_environment_value,
            model_fetcher=stub_fetcher(tracker, models={}),
            models={},
        )
        == 0
    )
    state = json.loads((tmp_path / "state" / "state.json").read_text())
    assert state["providers"]["glm"]["status"].startswith("error:")
    assert state["providers"]["glm"]["pending"]["rounds"] == 1


@pytest.mark.usefixtures("retry_waits")
def test_fetch_json_retries_a_transient_connection_error(
    tracker: Any,
) -> None:
    url = "https://api.example.com/models"
    payload: dict[str, object] = {"data": []}
    calls: list[str] = []

    def get(request_url: str, **kwargs: object) -> _JSONResponse:
        calls.append(request_url)
        if len(calls) == 1:
            raise tracker.requests.exceptions.SSLError("EOF occurred in violation of protocol")
        return _JSONResponse(payload)

    def no_session_for_host(_host: str) -> None:
        return None

    assert (
        tracker.fetch_json(
            url,
            headers={},
            params={},
            session_for_host=no_session_for_host,
            http_get=get,
        )
        == payload
    )
    assert calls == [url, url]


@pytest.mark.usefixtures("retry_waits")
def test_fetch_json_raises_after_the_retry_budget_is_exhausted(
    tracker: Any,
) -> None:
    calls: list[str] = []

    def get(request_url: str, **kwargs: object) -> _JSONResponse:
        calls.append(request_url)
        raise tracker.requests.exceptions.SSLError("EOF occurred in violation of protocol")

    def no_session_for_host(_host: str) -> None:
        return None

    with pytest.raises(tracker.requests.exceptions.SSLError):
        tracker.fetch_json(
            "https://api.example.com/models",
            headers={},
            params={},
            session_for_host=no_session_for_host,
            http_get=get,
        )
    assert len(calls) == 3


@pytest.mark.usefixtures("retry_waits")
def test_permanent_http_error_is_not_retried(tracker: Any) -> None:
    url = "https://api.example.com/models"
    calls: list[str] = []

    def get(request_url: str, **kwargs: object) -> Any:
        calls.append(request_url)
        response = tracker.requests.Response()
        response.status_code = 400
        return response

    def no_session_for_host(_host: str) -> None:
        return None

    with pytest.raises(tracker.requests.exceptions.HTTPError):
        tracker.fetch_json(
            url,
            headers={},
            params={},
            session_for_host=no_session_for_host,
            http_get=get,
        )
    assert calls == [url]


@pytest.mark.usefixtures("retry_waits")
def test_transient_ssl_eof_is_retried_without_marking_the_provider_error(
    tmp_path: Path, tracker: Any
) -> None:
    env_file = tmp_path / ".env"
    write_env_file(tracker, env_file)
    gemini_calls: list[str] = []

    def get(request_url: str, **kwargs: object) -> _JSONResponse:
        gemini_calls.append(request_url)
        if len(gemini_calls) == 1:
            raise tracker.requests.exceptions.SSLError("EOF occurred in violation of protocol")
        return _JSONResponse({"models": []})

    def no_session_for_host(_host: str) -> None:
        return None

    def fetch(source: Any, api_key: str) -> list[str]:
        if source.provider == "gemini":
            return tracker.fetch_provider_models(
                source,
                api_key,
                fetcher=lambda url, *, headers, params: tracker.fetch_json(
                    url,
                    headers=headers,
                    params=params,
                    session_for_host=no_session_for_host,
                    http_get=get,
                ),
            )
        return known_models(tracker, source, models={})

    args = ["--env-file", str(env_file), "--state-dir", str(tmp_path / "state")]
    assert (
        tracker.main(
            args,
            env_lookup=missing_environment_value,
            model_fetcher=fetch,
            models={},
        )
        == 0
    )
    assert len(gemini_calls) == 2
    state = json.loads((tmp_path / "state" / "state.json").read_text())
    assert state["providers"]["gemini"]["status"] == "ok"


@pytest.mark.usefixtures("retry_waits")
def test_repeated_provider_failure_announces_once_and_never_repeats(
    tmp_path: Path, tracker: Any
) -> None:
    env_file = tmp_path / ".env"
    write_env_file(tracker, env_file)
    calls: list[str] = []

    def ok_get(request_url: str, **kwargs: object) -> _JSONResponse:
        calls.append(request_url)
        return _JSONResponse({"models": []})

    def failing_get(request_url: str, **kwargs: object) -> _JSONResponse:
        calls.append(request_url)
        raise tracker.requests.exceptions.SSLError("EOF occurred in violation of protocol")

    def no_session_for_host(_host: str) -> None:
        return None

    current_get: Callable[..., _JSONResponse] = ok_get

    def fetch(source: Any, api_key: str) -> list[str]:
        if source.provider == "gemini":
            return tracker.fetch_provider_models(
                source,
                api_key,
                fetcher=lambda url, *, headers, params: tracker.fetch_json(
                    url,
                    headers=headers,
                    params=params,
                    session_for_host=no_session_for_host,
                    http_get=current_get,
                ),
            )
        return known_models(tracker, source, models={})

    args = ["--env-file", str(env_file), "--state-dir", str(tmp_path / "state")]

    def run_tracker() -> int:
        return tracker.main(
            args,
            env_lookup=missing_environment_value,
            model_fetcher=fetch,
            models={},
        )

    assert run_tracker() == 0
    assert len(calls) == 1

    current_get = failing_get
    calls.clear()
    assert run_tracker() == 0
    assert len(calls) == 3

    calls.clear()
    assert run_tracker() == 2
    assert len(calls) == 3

    calls.clear()
    assert run_tracker() == 0
    assert len(calls) == 3
    state = json.loads((tmp_path / "state" / "state.json").read_text())
    assert state["providers"]["gemini"]["status"].startswith("error:")
