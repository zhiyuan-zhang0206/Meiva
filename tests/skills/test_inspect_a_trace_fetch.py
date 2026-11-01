"""Hermetic unit tests for the inspect-a-trace toolchain
(.agents/skills/inspect-a-trace/scripts/{fetch,read}_trace.py).

Locks the PR #637 follow-up nits: a malformed sibling span must not abort
the whole mirror scan, --trace-id validation must match its error text, and
unpadded base64 ids (legacy OTLP JSON) must decode. Pure logic only — no
network, no Tempo; the mirror walk uses tmp_path files.

`_mirror_dir` / `_cluster_secret` / `_gateway_get` resolve `$MEIVA_HOME` via
`base.paths.home.resolve_meiva_home` (the variable, else `~/.meiva`), the
same resolution every other Meiva process uses.

MEIVA_CLUSTER_SECRET is also a `base.config.Settings` field alias, but
`read_trace.py` reads it straight from `os.environ` by design (the same
Settings-free stance as the rest of this skill script) — `monkeypatch.
setitem(os.environ, ...)` is used below instead of `monkeypatch.setenv` /
`delenv` so `no_os_environ.py`'s real Settings-singleton-no-op check
stays meaningful for tests that DO exercise Settings.

`_common.py::source_root` tests: `read_trace.py` / `fetch_trace.py` each need to
locate the `base` package before they can import
`base.paths.home.resolve_meiva_home` — a bootstrap problem
`resolve_meiva_home` itself cannot solve, so `_common.py` mirrors its rule. The
walk-up-from-`__file__` branch (the dev checkout, or a converged
`$MEIVA_HOME/skills/...` copy invoked with an interpreter that already carries
`base` on `sys.path`) needs no fix and is exercised by every `_load()` above.
The tests below lock the *other* branch: when no `base` package is found above
the script, the converged-copy fallback consults `<home>/source` for the home
`MEIVA_HOME` names, else `~/.meiva`. They run `_common.py` in a subprocess with a
from-scratch environment and copy it to an isolated directory with no `base`
package anywhere above it, so the walk-up branch is forced to fail and the
fallback branch actually runs.
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from base.host.proc import run_bounded

_SCRIPTS_DIR = Path(__file__).parents[2] / ".agents" / "skills" / "inspect-a-trace" / "scripts"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS_DIR / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ft = _load("fetch_trace_under_test", "fetch_trace.py")
rt = _load("read_trace_under_test", "read_trace.py")

_TRACE = "00112233445566778899aabbccddeeff"
_OTHER = "ffeeddccbbaa99887766554433221100"
_SPAN_A = "0102030405060708"
_SPAN_B = "1112131415161718"
_SPAN_C = "2122232425262728"
_SPAN_D = "3132333435363738"


def _span(
    span_id: str,
    name: str = "op",
    trace_id: str | None = _TRACE,
    parent: str | None = None,
) -> dict[str, Any]:
    sp: dict[str, Any] = {
        "spanId": span_id,
        "name": name,
        "kind": "SPAN_KIND_INTERNAL",
        "startTimeUnixNano": "1000",
        "endTimeUnixNano": "2000",
        "status": {"code": "STATUS_CODE_OK"},
        "attributes": [],
    }
    if trace_id is not None:
        sp["traceId"] = trace_id
    if parent is not None:
        sp["parentSpanId"] = parent
    return sp


def _envelope(*spans: dict[str, Any]) -> dict[str, Any]:
    return {"resourceSpans": [{"scopeSpans": [{"spans": list(spans)}]}]}


# --- _id_to_hex ---


def test_id_to_hex_normalizes_hex_and_lowercases() -> None:
    assert ft._id_to_hex(_TRACE) == _TRACE
    assert ft._id_to_hex(_TRACE.upper()) == _TRACE
    assert ft._id_to_hex(_SPAN_B.upper()) == _SPAN_B


def test_id_to_hex_decodes_padded_base64() -> None:
    assert ft._id_to_hex("ABEiM0RVZneImaq7zN3u/w==") == _TRACE
    assert ft._id_to_hex("qrvM3e7/ABE=") == "aabbccddeeff0011"


def test_id_to_hex_decodes_unpadded_base64() -> None:
    """Legacy protojson without padding: 22-char trace id, 11-char span id."""
    assert ft._id_to_hex("ABEiM0RVZneImaq7zN3u/w") == _TRACE
    assert ft._id_to_hex("qrvM3e7/ABE") == "aabbccddeeff0011"


@pytest.mark.parametrize(
    "bad",
    ["", "0" * 31, "0" * 33, "g" * 32, "!" * 24, "zzzzzzzzzzzzzzzzzzzzzzzz"],
)
def test_id_to_hex_refuses_malformed_values(bad: str) -> None:
    with pytest.raises(ValueError, match="malformed span id"):
        ft._id_to_hex(bad)


# --- _mirror_day ---


def test_mirror_day_parses_rotated_names() -> None:
    assert ft._mirror_day(Path("spans-2026-08-25T16-14-01.621-size.jsonl")) == date(2026, 8, 25)
    assert ft._mirror_day(Path("spans-2026-08-25T16-14-01.621-time.jsonl")) == date(2026, 8, 25)
    assert ft._mirror_day(Path("spans.jsonl")) is None


def test_mirror_day_returns_none_for_unparsable_dates() -> None:
    assert ft._mirror_day(Path("spans-2026-13-99T00-00-00.000-size.jsonl")) is None
    assert ft._mirror_day(Path("spans-hello.jsonl")) is None


# --- _spans_from_envelope: malformed siblings must not abort the scan ---


def test_malformed_sibling_with_empty_trace_id_is_skipped() -> None:
    data = _envelope(
        _span(_SPAN_A),
        _span("", trace_id=""),  # empty traceId — old code crashed here
        _span(_SPAN_B),
    )

    spans = ft._spans_from_envelope(data, _TRACE)

    assert [s["span_id"] for s in spans] == [_SPAN_A, _SPAN_B]


def test_malformed_sibling_missing_trace_id_is_skipped() -> None:
    bad = _span(_SPAN_A)
    del bad["traceId"]
    data = _envelope(_span(_SPAN_B), bad, _span(_SPAN_C))

    spans = ft._spans_from_envelope(data, _TRACE)

    assert [s["span_id"] for s in spans] == [_SPAN_B, _SPAN_C]


def test_malformed_target_span_is_skipped_but_siblings_kept() -> None:
    """A span with the right traceId but a broken spanId is dropped, not fatal."""
    data = _envelope(_span(_SPAN_A), _span("bad", trace_id=_TRACE), _span(_SPAN_B))

    spans = ft._spans_from_envelope(data, _TRACE)

    assert [s["span_id"] for s in spans] == [_SPAN_A, _SPAN_B]


def test_other_trace_spans_are_filtered_out() -> None:
    data = _envelope(_span(_SPAN_A), _span(_SPAN_B, trace_id=_OTHER))

    spans = ft._spans_from_envelope(data, _TRACE)

    assert [s["span_id"] for s in spans] == [_SPAN_A]


# --- cmd_fetch --trace-id validation ---


def _fetch_args(trace_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        trace_id=trace_id,
        source="mirror",
        out="trace_raw.json",
        days=2,
        mirror_dir=None,
        tempo_url="http://localhost:3200",
    )


def test_cmd_fetch_rejects_wrong_length_ids(capsys: pytest.CaptureFixture[str]) -> None:
    for bad in ("a" * 30, "z" * 32):
        assert ft.cmd_fetch(_fetch_args(bad)) == 2
        assert "--trace-id must be a 31- or 32-char hex id" in capsys.readouterr().out


def test_cmd_fetch_accepts_31_and_32_char_ids_and_zfills(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    zfilled_id = "0" + "a" * 31
    (tmp_path / "spans.jsonl").write_text(
        json.dumps(_envelope(_span(_SPAN_A, trace_id=zfilled_id))) + "\n",
        encoding="utf-8",
    )
    out_file = tmp_path / "out.json"
    hit_args = _fetch_args("a" * 31)
    hit_args.mirror_dir = str(tmp_path)
    hit_args.out = str(out_file)
    miss_args = _fetch_args("b" * 32)
    miss_args.mirror_dir = str(tmp_path)
    miss_args.out = str(tmp_path / "miss.json")

    assert ft.cmd_fetch(hit_args) == 0
    assert ft.cmd_fetch(miss_args) == 1
    capsys.readouterr()
    assert json.loads(out_file.read_text(encoding="utf-8"))["trace_id"] == zfilled_id


# --- fetch_from_mirror: cross-rotation merge + malformed lines ---


def test_fetch_from_mirror_merges_across_rotation_and_skips_bad_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    active = tmp_path / "spans.jsonl"
    # Relative date: the retention window slides with the wall clock — a hard
    # coded date becomes a CI time bomb (2026-09-24 for 2026-08-25 + days=30).
    rotated_day = datetime.now(UTC).date() - timedelta(days=1)
    rotated = tmp_path / f"spans-{rotated_day}T16-14-01.621-size.jsonl"
    legacy_unpadded = ft._trace_id_b64(_TRACE).rstrip("=")
    active.write_text(
        chr(10).join(
            [
                json.dumps(_envelope(_span(_SPAN_A), _span(_SPAN_B, trace_id=_OTHER))),
                json.dumps(_envelope(_span(_SPAN_D, trace_id=legacy_unpadded))),
                "not-json-at-all",
            ]
        )
        + chr(10),
        encoding="utf-8",
    )
    rotated.write_text(
        json.dumps(
            _envelope(
                _span(_SPAN_A),  # duplicate of A: same span_id, deduped
                _span(_SPAN_C),
                _span("", trace_id=""),  # malformed sibling — must not abort
            )
        )
        + chr(10),
        encoding="utf-8",
    )
    args = SimpleNamespace(mirror_dir=str(tmp_path), days=30)

    spans = ft.fetch_from_mirror(args, _TRACE)
    capsys.readouterr()

    assert sorted(s["span_id"] for s in spans) == sorted([_SPAN_A, _SPAN_C, _SPAN_D])


# --- _mirror_dir: the resolved home's trace mirror ---


def test_mirror_dir_respects_explicit_override(tmp_path: Path) -> None:
    override = tmp_path / "custom-traces"
    args = SimpleNamespace(mirror_dir=str(override))

    assert ft._mirror_dir(args) == override


def test_mirror_dir_uses_resolve_meiva_home() -> None:
    assert ft._mirror_dir(SimpleNamespace(mirror_dir=None)) == ft.resolve_meiva_home() / "traces"


# --- _cluster_secret: the machine token, then the explicit env var, then the home's .env ---


def _run_rt_subcommand(args: list[str], env_overrides: dict[str, str]) -> str:
    env = {
        k: v for k, v in os.environ.items() if k not in {"MEIVA_API_TOKEN", "MEIVA_CLUSTER_SECRET"}
    }
    env |= env_overrides
    res = run_bounded(
        [sys.executable, __file__, *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    res.check_returncode()
    return res.stdout


def test_cluster_secret_prefers_explicit_env(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("MEIVA_CLUSTER_SECRET=file-secret\n")

    out = _run_rt_subcommand(
        ["cluster-secret", str(home)],
        {"MEIVA_CLUSTER_SECRET": "explicit-secret"},
    )
    assert json.loads(out) == "explicit-secret"


def test_cluster_secret_reads_the_homes_env_file(tmp_path: Path) -> None:
    home = tmp_path / "dev-cluster-home"
    home.mkdir()
    (home / ".env").write_text("MEIVA_CLUSTER_SECRET=dev-cluster-secret\n")

    out = _run_rt_subcommand(["cluster-secret", str(home)], {})
    assert json.loads(out) == "dev-cluster-secret"


def test_cluster_secret_prefers_the_machine_api_token(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("MEIVA_CLUSTER_SECRET=file-secret\n")

    out = _run_rt_subcommand(
        ["cluster-secret", str(home)],
        {
            "MEIVA_API_TOKEN": "machine-token",
            "MEIVA_CLUSTER_SECRET": "explicit-secret",
        },
    )
    assert json.loads(out) == "machine-token"


# --- _gateway_get: the bearer comes from the resolved home ---


@contextmanager
def _serve_gateway() -> Generator[tuple[str, list[str | None]]]:
    seen_auth: list[str | None] = []
    body = json.dumps({"ok": True}).encode()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen_auth.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen_auth
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_gateway_get_sends_the_explicit_secret() -> None:
    with _serve_gateway() as (base_url, seen_auth):
        out = _run_rt_subcommand(
            ["gateway-get", base_url, "/api/events"],
            {"MEIVA_CLUSTER_SECRET": "explicit-secret"},
        )

    assert json.loads(out) == {"ok": True}
    assert seen_auth == ["Bearer explicit-secret"]


# meiva:note
# This test gets its own home rather than writing the suite's. pytest gives the run one home for
# its whole life (`tests/fixtures/env_bootstrap`), and every fixture that touches the database or
# Redis reads `<home>/.env` before it does anything else, so a test that rewrites or removes that
# file breaks every later test in the same worker -- `--dist=loadfile` schedules this file in the
# middle of a worker's queue, and shard 1/8 of run 41288203853 lost its `.env` there and reported
# 265 setup errors (task #5933/E).
def test_gateway_get_uses_the_homes_env_file_secret(tmp_path: Path) -> None:
    home = tmp_path / "gateway-home"
    home.mkdir()
    (home / ".env").write_text("MEIVA_CLUSTER_SECRET=dev-cluster-secret\n")

    with _serve_gateway() as (base_url, seen_auth):
        out = _run_rt_subcommand(
            ["gateway-get", base_url, "/api/events"], {"MEIVA_HOME": str(home)}
        )

    assert json.loads(out) == {"ok": True}
    assert seen_auth == ["Bearer dev-cluster-secret"]


# --------------------------------------------------------------------------- #
# `_common.py::source_root` — the walk-up failed, so the home's own checkout is consulted
# --------------------------------------------------------------------------- #

# argv: <scripts dir>. Prints the resolved source root on success; on
# RuntimeError (the fail-fast path), prints "RUNTIMEERROR: <message>" and
# exits 1 so the test can tell a refusal apart from a crash.
_SOURCE_ROOT_DRIVER = r"""
import sys

sys.path.insert(0, sys.argv[1])
import _common

try:
    print(str(_common.source_root()))
except RuntimeError as exc:
    print(f"RUNTIMEERROR: {exc}")
    sys.exit(1)
"""


def _run_source_root(
    scripts_dir: Path, env_overrides: dict[str, str], home: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEIVA_")}
    if home is not None:
        env["HOME"] = str(home)
    env |= env_overrides
    return subprocess.run(  # noqa: S603 — fixed argv, repository-owned driver script
        [sys.executable, "-c", _SOURCE_ROOT_DRIVER, str(scripts_dir)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )


def test_source_root_walks_up_to_the_real_checkout() -> None:
    """The unmodified dev-checkout invocation needs no `MEIVA_HOME` at all —
    `_common.py` lives under the real repo, whose root has `base/__init__.py`."""
    res = _run_source_root(_SCRIPTS_DIR, {})

    assert res.returncode == 0, res.stdout + res.stderr
    repo_root = Path(__file__).parents[2]
    assert res.stdout.strip() == str(repo_root)


def _isolated_scripts_dir(tmp_path: Path) -> Path:
    """Copy `_common.py` somewhere with no `base` package above it, forcing
    the walk-up branch to fail so the MEIVA_HOME fallback branch actually runs."""
    isolated = tmp_path / "isolated" / "scripts"
    isolated.mkdir(parents=True)
    shutil.copy(_SCRIPTS_DIR / "_common.py", isolated / "_common.py")
    return isolated


def test_source_root_refuses_when_base_is_found_nowhere(tmp_path: Path) -> None:
    """No `base` above the script, MEIVA_HOME unset and nothing at `~/.meiva/source`
    (HOME is a fake directory): there is no checkout to locate."""
    isolated = _isolated_scripts_dir(tmp_path)
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    res = _run_source_root(isolated, {}, home=fake_home)

    assert res.returncode == 1
    assert "RUNTIMEERROR:" in res.stdout
    assert str(fake_home / ".meiva" / "source") in res.stdout


def test_source_root_falls_back_to_the_default_homes_source(tmp_path: Path) -> None:
    """With MEIVA_HOME unset the home is `~/.meiva`, exactly as `resolve_meiva_home`
    decides (here under a fake HOME): its `source` is where `base` is found."""
    isolated = _isolated_scripts_dir(tmp_path)
    fake_home = tmp_path / "home"
    source = fake_home / ".meiva" / "source"
    (source / "base").mkdir(parents=True)
    (source / "base" / "__init__.py").write_text("")

    res = _run_source_root(isolated, {}, home=fake_home)

    assert res.returncode == 0, res.stdout + res.stderr
    assert res.stdout.strip() == str(source)


def test_source_root_uses_the_explicit_meiva_home(tmp_path: Path) -> None:
    isolated = _isolated_scripts_dir(tmp_path)
    home = tmp_path / "dev-cluster-home"
    source = home / "source"
    (source / "base").mkdir(parents=True)
    (source / "base" / "__init__.py").write_text("")

    res = _run_source_root(isolated, {"MEIVA_HOME": str(home)})

    assert res.returncode == 0, res.stdout + res.stderr
    assert res.stdout.strip() == str(source)


def test_source_root_refuses_when_explicit_meiva_home_has_no_source(tmp_path: Path) -> None:
    isolated = _isolated_scripts_dir(tmp_path)
    home = tmp_path / "dev-cluster-home-without-source"
    home.mkdir()

    res = _run_source_root(isolated, {"MEIVA_HOME": str(home)})

    assert res.returncode == 1
    assert "RUNTIMEERROR:" in res.stdout
    assert str(home / "source") in res.stdout


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "cluster-secret":
        sys.stdout.write(json.dumps(rt._cluster_secret(Path(sys.argv[2]))))
    elif mode == "gateway-get":
        sys.stdout.write(json.dumps(rt._gateway_get(sys.argv[2], sys.argv[3])))
    else:
        raise ValueError(f"unknown mode: {mode!r}")
