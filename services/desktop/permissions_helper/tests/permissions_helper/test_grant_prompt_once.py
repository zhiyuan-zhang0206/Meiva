# meiva:note
# The desktop grant dialogs are raised at most once per helper process: a dialog steals
# focus, so a daemon that re-asks on a timer turns one missing grant into a stream of
# interruptions. These tests pin that discipline at the source level (every prompt call
# site lives inside a gate) and by running the sliced gate against stubbed probes.
# meiva:link services/docs/agent_runner_side/permissions-helper/authorization.meiva.okf.md
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from base.host.proc import run_bounded

_SWIFT = (Path(__file__).parents[2] / "helper" / "main.swift").read_text()
_START = "// MARK: - Desktop grant prompts\n"
_END = "// MARK: - Desktop grant prompts (end)"


def _gate_source() -> str:
    assert _START in _SWIFT and _END in _SWIFT
    return _START + _SWIFT.split(_START, 1)[1].split(_END, 1)[0]


def _body(name: str) -> str:
    head = f"func {name}("
    assert head in _SWIFT
    return _SWIFT.split(head, 1)[1].split("\n}\n", 1)[0]


def test_each_prompt_call_site_is_reached_only_through_a_gate() -> None:
    gate = _gate_source()
    assert _SWIFT.count("AXIsProcessTrustedWithOptions(") == 1
    assert _SWIFT.count("CGRequestScreenCaptureAccess(") == 1
    assert "AXIsProcessTrustedWithOptions(" in gate
    assert "CGRequestScreenCaptureAccess(" in gate


def test_no_timer_or_cooldown_rearms_the_prompt() -> None:
    assert "lastAXPromptAt" not in _SWIFT
    assert "axPromptMinInterval" not in _SWIFT


def test_registration_and_grant_requests_reuse_the_same_gates() -> None:
    for name, gate_call in (
        ("registerPermissions", "_ = screenGrantOrPrompt()"),
        ("registerPermissions", "_ = axTrustedOrPrompt()"),
        ("requestGrants", "_ = screenGrantOrPrompt()"),
        ("requestGrants", "_ = axTrustedOrPrompt()"),
    ):
        assert gate_call in _body(name), (name, gate_call)


_HARNESS = r"""
import CoreFoundation
import Foundation

var axPromptCalls = 0
var screenPromptCalls = 0
var axTrusted = false
var screenGranted = false

let kAXTrustedCheckOptionPrompt = Unmanaged.passUnretained(
    "AXTrustedCheckOptionPrompt" as CFString
)

func AXIsProcessTrusted() -> Bool { axTrusted }

func AXIsProcessTrustedWithOptions(_ options: CFDictionary) -> Bool {
    axPromptCalls += 1
    return axTrusted
}

func CGPreflightScreenCaptureAccess() -> Bool { screenGranted }

func CGRequestScreenCaptureAccess() -> Bool {
    screenPromptCalls += 1
    return screenGranted
}

func bits(_ values: [Bool]) -> String {
    values.map { $0 ? "1" : "0" }.joined()
}

let scenario = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : ""
switch scenario {
case "ax-untrusted":
    var allowed: [Bool] = []
    for _ in 0 ..< 5 { allowed.append(axTrustedOrPrompt()) }
    print("\(axPromptCalls) \(bits(allowed))")
case "ax-trusted":
    axTrusted = true
    var allowed: [Bool] = []
    for _ in 0 ..< 5 { allowed.append(axTrustedOrPrompt()) }
    print("\(axPromptCalls) \(bits(allowed))")
case "ax-then-granted":
    var allowed: [Bool] = []
    for _ in 0 ..< 3 { allowed.append(axTrustedOrPrompt()) }
    axTrusted = true
    for _ in 0 ..< 2 { allowed.append(axTrustedOrPrompt()) }
    print("\(axPromptCalls) \(bits(allowed))")
case "screen-untrusted":
    _ = screenGrantOrPrompt()
    _ = screenGrantOrPrompt()
    _ = screenGrantOrPrompt()
    print("\(screenPromptCalls) \(screenGranted ? 1 : 0)")
case "screen-granted":
    screenGranted = true
    _ = screenGrantOrPrompt()
    _ = screenGrantOrPrompt()
    print("\(screenPromptCalls) 1")
default:
    print("unknown scenario")
}
"""


@pytest.fixture(scope="module")
def gate_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    swiftc = shutil.which("swiftc")
    if swiftc is None:
        pytest.skip("swiftc is unavailable")
    work = tmp_path_factory.mktemp("grant-prompt-once")
    (work / "gate.swift").write_text("import CoreFoundation\nimport Foundation\n" + _gate_source())
    (work / "main.swift").write_text(_HARNESS)
    binary = work / "gate"
    result = run_bounded(
        [swiftc, "-O", str(work / "gate.swift"), str(work / "main.swift"), "-o", str(binary)],
        timeout=300,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"the sliced grant gate does not compile:\n{result.stderr}")
    return binary


def _run(binary: Path, scenario: str) -> tuple[int, str]:
    result = run_bounded([str(binary), scenario], timeout=60, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    prompts, flags = result.stdout.split()
    return int(prompts), flags


@pytest.mark.skipif(sys.platform != "darwin", reason="the helper only builds on macOS")
def test_an_untrusted_helper_asks_exactly_once_per_process(gate_binary: Path) -> None:
    assert _run(gate_binary, "ax-untrusted") == (1, "00000")


@pytest.mark.skipif(sys.platform != "darwin", reason="the helper only builds on macOS")
def test_a_granted_helper_never_asks(gate_binary: Path) -> None:
    assert _run(gate_binary, "ax-trusted") == (0, "11111")


@pytest.mark.skipif(sys.platform != "darwin", reason="the helper only builds on macOS")
def test_a_consumed_prompt_is_not_reissued_after_the_grant_arrives(gate_binary: Path) -> None:
    assert _run(gate_binary, "ax-then-granted") == (1, "00011")


@pytest.mark.skipif(sys.platform != "darwin", reason="the helper only builds on macOS")
def test_screen_recording_follows_the_same_rule(gate_binary: Path) -> None:
    assert _run(gate_binary, "screen-untrusted") == (1, "0")
    assert _run(gate_binary, "screen-granted") == (0, "1")
