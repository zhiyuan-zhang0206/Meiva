from __future__ import annotations

import os
import random
import subprocess
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from base.clock import Clock, ClockConfig, Timers
from base.clock.tests.manual_timers import ManualTimers
from services.desktop.permissions_helper.launchd_job import BoundedRunner, LaunchdFacts
from services.desktop.permissions_helper.lifecycle import BuildEnv, HelperSources, InstallEnv

# meiva:note
# The account these tests act as for launchd's purposes: macOS, this process's real user id (the
# plists a test writes are its own, which retirement checks for ownership) and an account home under
# the test's own directory, so the LaunchAgents directory is one the test can read back.
USER_ID = os.getuid()


def launchd_facts(home: Path) -> LaunchdFacts:
    return LaunchdFacts(uid=USER_ID, platform="darwin", user_home=home / "user")


# meiva:note
# What `launchctl print` answers for this home's helper job: a running job, or launchd's own
# "could not find service" (status 113). None for any other command, so a fake runner can chain it.
def launchctl_reply(
    cmd: Sequence[str], *, loaded: bool
) -> subprocess.CompletedProcess[bytes] | None:
    if cmd[:2] != ["launchctl", "print"]:
        return None
    if loaded:
        return subprocess.CompletedProcess(list(cmd), 0, b"pid = 321\n", b"")
    return subprocess.CompletedProcess(list(cmd), 113, b"", b"Could not find service")


# meiva:note
# The world a helper build reads, over the test's own `home`: this account's launchd facts, the
# runner the test hands in, timers the test moves by hand and the operating system's generator for
# the keychain password. The sources are the helper's own unless the test stages others.
def helper_build_env(
    home: Path,
    run: BoundedRunner,
    *,
    sources: HelperSources | None = None,
    timers: Timers | None = None,
) -> BuildEnv:
    config = ClockConfig("UTC", None, message_timestamp_weekday=False)
    if timers is None:
        manual = ManualTimers()
        clock_timers, clock = manual, Clock(config, timers=manual, now=manual.now)
    else:
        clock_timers, clock = timers, Clock(config, timers=timers)
    env = BuildEnv(
        home=home,
        facts=launchd_facts(home),
        run=run,
        timers=clock_timers,
        clock=clock,
        rng=random.SystemRandom(),
    )
    return env if sources is None else replace(env, sources=sources)


# meiva:note
# A runner for a test whose path runs no tool: a call is a failure.
def refuse_tools(
    cmd: Sequence[str], /, *, timeout: float, capture_output: bool = True
) -> subprocess.CompletedProcess[bytes]:
    del timeout, capture_output
    raise AssertionError(f"this test must not run {list(cmd)}")


def helper_install_env(build: BuildEnv, *, helper_port: int = 9223) -> InstallEnv:
    return InstallEnv(build=build, helper_port=helper_port, artifact_dir=None)


def stopped_helper_plist(
    home: Path, path: Path, *, executable: str = "/private/test/MeivaPermissionsHelper"
) -> bytes:
    import plistlib

    from services.desktop.permissions_helper import launchd_job as jobs

    body = plistlib.dumps(
        {
            "Label": jobs.helper_job_label(home),
            "ProgramArguments": [executable],
            "KeepAlive": {"SuccessfulExit": False},
            "EnvironmentVariables": {
                "MEIVA_PERMISSIONS_HELPER_SOCKET": str(home / "run/permissions-helper.23456.sock"),
                "MEIVA_PERMISSIONS_HELPER_ROOT_SEED": str(home / "run/meiva-root/seed.json"),
            },
        }
    )
    path.write_bytes(body)
    return body


INPUT_HARNESS = r"""
import Foundation
import CoreGraphics
import CoreFoundation
enum OpError: Error { case bad(String) }
enum Thread {
    static var waits: [Double] = []
    static func sleep(forTimeInterval value: Double) { waits.append(value) }
}
var posted: [CGEvent] = []
var created = 0
var failAt = 0
var reads = 0
var cursorFailAt = 0
var cursorReadFails = false
var cursorScript: [CGPoint] = []
var cursorDefault = CGPoint(x: -8, y: 9)
var mainOrigin = CGPoint.zero
func CGMainDisplayID() -> CGDirectDisplayID { 1 }
func CGDisplayBounds(_ id: CGDirectDisplayID) -> CGRect {
    CGRect(origin: mainOrigin, size: CGSize(width: 1000, height: 800))
}
class CGEvent {
    var type: CGEventType = .null
    var flags: CGEventFlags = []
    var timestamp: UInt64 = 0
    var location: CGPoint = CGPoint(x: -8, y: 9)
    var button: CGMouseButton = .left
    var code: CGKeyCode = 0
    var clickState: Int64 = 0
    var wheels: [Int32] = []
    init?() {
        created += 1
        if created == failAt { return nil }
    }
    /// A cursor read: scripted, never counted as an allocation.
    init?(cursorRead: Void) {
        reads += 1
        if cursorReadFails || reads == cursorFailAt { return nil }
        location = cursorScript.isEmpty ? cursorDefault : cursorScript.removeFirst()
    }
    convenience init?(mouseEventSource: Any?, mouseType: CGEventType,
                     mouseCursorPosition: CGPoint, mouseButton: CGMouseButton) {
        self.init(); type = mouseType; location = mouseCursorPosition; button = mouseButton
    }
    convenience init?(keyboardEventSource: Any?, virtualKey: CGKeyCode, keyDown: Bool) {
        self.init(); type = keyDown ? .keyDown : .keyUp; code = virtualKey
    }
    convenience init?(scrollWheelEvent2Source: Any?, units: CGScrollEventUnit, wheelCount: UInt32,
                      wheel1: Int32, wheel2: Int32, wheel3: Int32) {
        self.init(); type = .scrollWheel; wheels = [wheel1, wheel2, wheel3]
        precondition(wheelCount == 2 && units == .pixel)
    }
    convenience init?(source: Any?) { self.init(cursorRead: ()) }
    func setIntegerValueField(_ field: CGEventField, value: Int64) { clickState = value }
    func post(tap: CGEventTapLocation) { posted.append(self) }
}
func reset() {
    posted = []; created = 0; failAt = 0; Thread.waits = []
    reads = 0; cursorFailAt = 0; cursorReadFails = false; cursorScript = []
    cursorDefault = CGPoint(x: -8, y: 9); stubCalls = 0
}

var stubCalls = 0
var monitorStarts = 0
var monitorStops = 0
func startHeldMouseMonitor() { monitorStarts += 1 }
func stopHeldMouseMonitor() { monitorStops += 1 }
var abortStops = 0
@discardableResult func stopCaptureIndicator() -> Bool { abortStops += 1; return true }
"""


def helper_source() -> str:
    from services.desktop.permissions_helper import lifecycle

    return lifecycle.SOURCE.read_text()


def between(source: str, start: str, end: str) -> str:
    return start + source.split(start, 1)[1].split(end, 1)[0]


def input_bodies(source: str) -> str:
    return between(source, "func rejectUnknownFields(", "func logIndicator(") + between(
        source, "func inputNumber(", "/// Post a synthetic mouse click"
    )


def dispatch_stubs(stubbed: list[str]) -> str:
    stubs = """
var trusted = true
func axTrustedOrPrompt() -> Bool { trusted }
func AXIsProcessTrusted() -> Bool { trusted }
func CGPreflightScreenCaptureAccess() -> Bool { false }
let axGrantError = "Accessibility grant missing"
struct RootSeed { static func from(_ raw: [String: Any]) throws -> RootSeed { RootSeed() } }
class Keeper {
    func configure(_ seed: RootSeed) throws { }
    func status() -> [String: Any] { [:] }
    func requestStop() throws -> [String: Any] { [:] }
}
let rootKeeper = Keeper()
/// Each badge request: the method, and how many events had been posted by then.
var holdCalls: [(method: String, postedBefore: Int)] = []
func holdIndicatorForInput(_ method: String, _ req: [String: Any]) {
    holdCalls.append((method, posted.count))
}
func requestHelperShutdown(runDir: String) throws -> [String: Any] { [:] }
"""
    for name in stubbed:
        stubs += (
            f"func {name}(_ req: [String: Any]) throws -> [String: Any] "
            "{ stubCalls += 1; return [:] }\n"
        )
    for name in ["listApps", "sessionInfo", "frontmostApp"]:
        stubs += f"func {name}() -> [String: Any] {{ stubCalls += 1; return [:] }}\n"
    return stubs


def _ignore_sigterm(_signum: int, _frame: object) -> None:
    pass


def _run_sleeper() -> None:
    import signal
    import sys
    import time

    signal.signal(signal.SIGTERM, _ignore_sigterm)
    if len(sys.argv) > 1:
        Path(sys.argv[1]).touch()
    time.sleep(60)


if __name__ == "__main__":
    _run_sleeper()
