# meiva:note
# Which tests declare a host, and which CI lane runs tests on that host.
#
# `@pytest.mark.requires_host("darwin")` names the one system whose behavior a test proves,
# and `tests/fixtures/host_platform.py` turns it into a skip everywhere else. A skip says the
# test does not run HERE; only a lane says it runs ANYWHERE. The two facts live in different
# files, so a test can declare macOS, skip on every runner, and stay green because the job
# that would have caught it never existed.
#
# Run: `.venv/bin/python -m scripts.structure.platform_lane_coverage` (report on stderr,
# exit 1) or `--print-debt` for the literal below. Its test runs in the static lane.
#
# The lanes are declared in `scripts/structure/platform_lanes.toml`; the declarations no lane
# covers yet are listed in `UNRUN_ALLOWED`, which may only shrink.

from __future__ import annotations

import ast
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml

from scripts.structure import skip_sites

_REPO_ROOT = Path(__file__).resolve().parents[2]

# meiva:note
# The two files this gate reads: where the lanes are declared, and the workflow whose jobs
# they name. A lane pointing at a job in another workflow would need its own reader, which is
# the point of keeping the registry next to the file it is checked against.
REGISTRY = "scripts/structure/platform_lanes.toml"
WORKFLOW = ".github/workflows/ci.yml"

# meiva:note
# `Platform`'s whole vocabulary (`base/native_process/os_platform.py` refuses anything else),
# so a declaration naming one of these is a cell this gate can judge. A name outside the
# vocabulary is refused by the marker plugin at collection time, not silently ignored.
SYSTEMS = ("darwin", "linux")

# meiva:note
# A skip whose condition names one host in the "not on the others" polarity: the two
# predicate spellings the tree used before the marker existed, and the comparison it replaced.
# `sys.platform == "darwin"` is the opposite polarity (skip ON darwin) and is not a host
# requirement; `os.name == "nt"` names a system `Platform` does not support.
_PREDICATE_SYSTEM = {"is_macos": "darwin", "is_linux": "linux"}

# meiva:note
# The declarations no lane covers yet, as `<path>::<test>`, sorted: only shrink, never grow.
# Every entry is a name a reader can open, and the tuple is what makes a lane's arrival
# visible - the run that covers them says the record lists what the tree no longer leaves
# unrun. Adding a line is a decision review can see.
UNRUN_ALLOWED: tuple[str, ...] = (
    "base/native_process/tests/test_os_platform.py::TestPtyMax::test_macos_reads_positive_ceiling",
    "base/native_process/tests/test_proc_tree.py::test_capture_and_verify_span_clock_epochs",
    "base/native_process/tests/test_proc_tree.py::test_stable_create_time_ignores_a_simulated_clock_step",
    "base/native_process/tests/test_pytest_env_locale.py::<module>",
    "base/sessions/tests/test_posixproc.py::test_session_record_spans_import_epochs",
    "services/desktop/permissions_helper/tests/permissions_helper/test_grant_prompt_once.py::test_a_consumed_prompt_is_not_reissued_after_the_grant_arrives",
    "services/desktop/permissions_helper/tests/permissions_helper/test_grant_prompt_once.py::test_a_granted_helper_never_asks",
    "services/desktop/permissions_helper/tests/permissions_helper/test_grant_prompt_once.py::test_an_untrusted_helper_asks_exactly_once_per_process",
    "services/desktop/permissions_helper/tests/permissions_helper/test_grant_prompt_once.py::test_screen_recording_follows_the_same_rule",
)


# meiva:note
# One test's statement that it needs one host. `declared` is `marker` for
# `@pytest.mark.requires_host("…")` and `skip` for the host-polarity skipif it replaced: both
# are facts about the tree, but only the marker is one a `-m requires_host` lane can select.
@dataclass(frozen=True)
class Site:
    system: str
    path: str
    test: str
    declared: str

    @property
    def key(self) -> str:
        return f"{self.path}::{self.test}"


# meiva:note
# One CI job that runs tests on one host. `selects` is `suite` (the whole tree on that host)
# or `marked` (`-m requires_host` only).
@dataclass(frozen=True)
class Lane:
    id: str
    host: str
    job: str
    selects: str


# meiva:note
# The workflow job a lane names: the runner it lands on (`runs-on`) is the host it actually
# tests on, and `continue-on-error` decides whether its verdict blocks. A lane whose job is
# informational runs the tests and judges none of them, which is the decoration this gate
# refuses to count as coverage.
@dataclass(frozen=True)
class Job:
    id: str
    runs_on: str
    system: str | None
    blocking: bool


# meiva:note
# `a.b.c` for an attribute chain, so a mark can be named by what it calls.
def _dotted(node: ast.expr) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _requires_host(mark: ast.AST) -> str | None:
    if not isinstance(mark, ast.Call):
        return None
    name = _dotted(mark.func) or ""
    if not name.endswith("mark.requires_host") or len(mark.args) != 1 or mark.keywords:
        return None
    argument = mark.args[0]
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return argument.value
    return None


def _skipif_condition(mark: ast.AST) -> ast.expr | None:
    if not isinstance(mark, ast.Call):
        return None
    name = _dotted(mark.func) or ""
    if not name.endswith("mark.skipif") or not mark.args:
        return None
    return mark.args[0]


# meiva:note
# `sys.platform != "darwin"` and `not is_macos()` both mean "this test is about darwin".
# The named predicate is matched by its last element so `plat.is_macos()` and a bare
# `is_macos()` read the same; anything else - a conjunction, a comparison in the other
# direction, a system outside `SYSTEMS` - is not a declaration this gate can read.
def _host_of_condition(condition: ast.expr) -> str | None:
    if isinstance(condition, ast.Compare) and len(condition.ops) == 1:
        left = condition.left
        right = condition.comparators[0]
        if (
            isinstance(condition.ops[0], ast.NotEq)
            and _dotted(left) == "sys.platform"
            and isinstance(right, ast.Constant)
            and right.value in SYSTEMS
        ):
            return str(right.value)
        return None
    if isinstance(condition, ast.UnaryOp) and isinstance(condition.op, ast.Not):
        operand = condition.operand
        if isinstance(operand, ast.Call) and not operand.args and not operand.keywords:
            name = (_dotted(operand.func) or "").rsplit(".", 1)[-1]
            return _PREDICATE_SYSTEM.get(name)
    return None


# meiva:note
# The two spellings reach the same fact; `judge` accepts only the first one as coverable,
# because a `-m requires_host` selection cannot see the second.
def _declaration(mark: ast.AST) -> tuple[str, str] | None:
    system = _requires_host(mark)
    if system is not None:
        return system, "marker"
    condition = _skipif_condition(mark)
    if condition is None:
        return None
    system = _host_of_condition(condition)
    if system is None:
        return None
    return system, "skip"


def _pytestmark_values(tree: ast.Module) -> list[ast.expr]:
    values: list[ast.expr] = []
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in statement.targets
        ):
            continue
        if isinstance(statement.value, ast.List | ast.Tuple):
            values.extend(statement.value.elts)
        else:
            values.append(statement.value)
    return values


def _collect(node: ast.AST, prefixes: list[str], rel: str, out: list[Site]) -> None:
    for child in ast.iter_child_nodes(node):
        if not isinstance(child, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        label = "::".join([*prefixes, child.name])
        for decorator in child.decorator_list:
            declaration = _declaration(decorator)
            if declaration is not None:
                out.append(Site(declaration[0], rel, label, declaration[1]))
                break
        _collect(child, [*prefixes, child.name], rel, out)


# meiva:note
# Every declaration in one module: a module-level `pytestmark` names a whole file (the test
# column reads `<module>`), a decorator names the class or function it sits on. Counting the
# declaration rather than the collected test keeps this a static read - the census has to work
# on a host that cannot run half of it.
def sites_in_source(rel: str, src: str) -> list[Site]:
    tree = ast.parse(src)
    out: list[Site] = []
    for value in _pytestmark_values(tree):
        declaration = _declaration(value)
        if declaration is not None:
            out.append(Site(declaration[0], rel, "<module>", declaration[1]))
    _collect(tree, [], rel, out)
    return out


def all_sites(root: Path = _REPO_ROOT) -> list[Site]:
    out: list[Site] = []
    for rel in skip_sites.tracked_test_modules(root):
        try:
            source = (root / rel).read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            out.extend(sites_in_source(rel, source))
        except SyntaxError:
            continue
    return sorted(out, key=lambda site: site.key)


def lanes(root: Path = _REPO_ROOT) -> list[Lane]:
    path = root / REGISTRY
    if not path.is_file():
        raise SystemExit(f"{REGISTRY}: missing; the gate cannot judge without the lane table")
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    table: dict[str, object] = cast("dict[str, object]", document.get("lanes") or {})
    out: list[Lane] = []
    for lane_id, entry in sorted(table.items()):
        body: dict[str, object] = cast("dict[str, object]", entry)
        host = str(body.get("host"))
        selects = str(body.get("selects"))
        if host not in SYSTEMS:
            raise SystemExit(
                f"{REGISTRY}: lane {lane_id} names host {host!r}: {' or '.join(SYSTEMS)}"
            )
        if selects not in {"suite", "marked"}:
            raise SystemExit(f"{REGISTRY}: lane {lane_id} selects {selects!r}: suite or marked")
        out.append(Lane(str(lane_id), host, str(body.get("ci_job")), selects))
    return out


def _runner_system(runs_on: object) -> str | None:
    if not isinstance(runs_on, str):
        return None
    if runs_on.startswith("macos"):
        return "darwin"
    if runs_on.startswith("ubuntu"):
        return "linux"
    return None


def jobs(root: Path = _REPO_ROOT) -> dict[str, Job]:
    document: object = yaml.safe_load((root / WORKFLOW).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise SystemExit(f"{WORKFLOW}: expected a workflow document")
    fields = cast("dict[str, object]", document)
    table: dict[str, object] = cast("dict[str, object]", fields.get("jobs") or {})
    out: dict[str, Job] = {}
    for job_id, entry in table.items():
        body: dict[str, object] = cast("dict[str, object]", entry)
        runs_on = body.get("runs-on")
        out[str(job_id)] = Job(
            id=str(job_id),
            runs_on=runs_on if isinstance(runs_on, str) else repr(runs_on),
            system=_runner_system(runs_on),
            blocking=not body.get("continue-on-error"),
        )
    return out


def _usable(lane: Lane, job_table: dict[str, Job]) -> bool:
    job = job_table.get(lane.job)
    return job is not None and job.system == lane.host and job.blocking


def _selects(site: Site, lane: Lane) -> bool:
    if lane.host != site.system:
        return False
    if lane.selects == "marked":
        return site.declared == "marker"
    return True


# meiva:note
# The declarations no usable lane selects. Public because `--print-debt` and `judge` must
# agree on one answer: a second computation would let the printed record and the judged one
# drift.
def unrun_keys(sites: list[Site], lane_list: list[Lane], job_table: dict[str, Job]) -> list[str]:
    covered = {
        site.key
        for site in sites
        if any(_selects(site, lane) and _usable(lane, job_table) for lane in lane_list)
    }
    return sorted({site.key for site in sites} - covered)


def judge(
    sites: list[Site],
    lane_list: list[Lane],
    job_table: dict[str, Job],
    unrun_allowed: tuple[str, ...],
) -> str | None:
    findings: list[str] = []
    if not sites:
        findings.append(
            "  the tree declares no host: a census of zero proves nothing - a tree whose macOS "
            "tests were deleted reads exactly like one whose macOS tests all skip"
        )
    findings.extend(
        f"  undeclared {site.key}  skips on the host without declaring it; write "
        f'@pytest.mark.requires_host("{site.system}") so a lane selection can see it'
        for site in sites
        if site.declared == "skip"
    )
    for lane in lane_list:
        findings.extend(_lane_faults(lane, job_table))
    unrun = unrun_keys(sites, lane_list, job_table)
    recorded = set(unrun_allowed)
    findings.extend(
        f"  +{key}  no lane runs this declaration and {REGISTRY.rsplit('/', 1)[-1]} does not "
        "record the gap"
        for key in unrun
        if key not in recorded
    )
    findings.extend(
        f"  -{key}  UNRUN_ALLOWED lists a declaration that a lane now covers, or that the "
        "tree no longer makes"
        for key in sorted(recorded - set(unrun))
    )
    if not findings:
        return None
    return "\n".join([_headline(sites, lane_list, unrun), *findings, _advice()])


def _lane_faults(lane: Lane, job_table: dict[str, Job]) -> list[str]:
    job = job_table.get(lane.job)
    if job is None:
        return [f"  lane {lane.id} names job {lane.job!r}, which {WORKFLOW} does not define"]
    if job.system is None:
        return [
            f"  lane {lane.id}: job {lane.job!r} runs on {job.runs_on}, so the host it tests on "
            "cannot be read"
        ]
    if job.system != lane.host:
        return [
            f"  lane {lane.id} declares host {lane.host} but job {lane.job!r} runs on "
            f"{job.runs_on}, which is {job.system}"
        ]
    if not job.blocking:
        return [
            f"  lane {lane.id}: job {lane.job!r} carries continue-on-error, so it runs these "
            "tests without judging them"
        ]
    return []


def _headline(sites: list[Site], lane_list: list[Lane], unrun: list[str]) -> str:
    counts = Counter(site.system for site in sites)
    by_system = ", ".join(f"{system} {counts[system]}" for system in SYSTEMS if counts[system])
    declared = ", ".join(f"{lane.host}:{lane.id}" for lane in lane_list) or "none"
    return (
        f"host declarations: {len(sites)} in {len({site.path for site in sites})} file(s) "
        f"({by_system or 'none'}); lanes: {declared}; unrun: {len(unrun)}"
    )


def _advice() -> str:
    return (
        "A host no lane runs is a test that skips everywhere: add the lane to "
        f"{REGISTRY}, or record the gap in UNRUN_ALLOWED - the record may only shrink."
    )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    sites = all_sites()
    lane_list = lanes()
    job_table = jobs()
    if "--print-debt" in arguments:
        sys.stdout.write("UNRUN_ALLOWED: tuple[str, ...] = (\n")
        for key in unrun_keys(sites, lane_list, job_table):
            sys.stdout.write(f'    "{key}",\n')
        sys.stdout.write(")\n")
        return 0
    report = judge(sites, lane_list, job_table, UNRUN_ALLOWED)
    if report is None:
        return 0
    sys.stderr.write(report + "\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
