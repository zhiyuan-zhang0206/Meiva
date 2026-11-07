from __future__ import annotations

from pathlib import Path

from meizel.tests.synthetic import RepoFactory, analysis_of, diagnostics, failures

ROOT = '[roles]\napplication = ["app"]\n'
CLOCK_CALL = "import time\n\ndef now() -> float:\n    return time.time()\n"


def repo(make_repo: RepoFactory, files: dict[str, str], manifest: str = "") -> Path:
    return make_repo(
        {"meizel.toml": ROOT, "app/__init__.py": "", "app/meizel.toml": manifest, **files}
    )


# meiva:note
# The same read recorded twice, second face: `[roots]` licenses the clock in this file and
# `[debt.impure-call]` charges the same call again. The purity rule leaves the call unrecorded, and
# the conflict is reported against the entry so `debt prune` drops it -- a counting correction, not a
# repayment: the code still reads the clock.
def test_a_root_that_also_pays_for_its_call_is_a_conflict(make_repo: RepoFactory) -> None:
    manifest = (
        '[purity]\n"mod.py" = "strict-to-be"\n'
        '[roots]\n"mod.py" = { kinds = ["clock"], why = "the clock door" }\n'
        '\n[debt.impure-call]\n"mod.py#time.time" = 1\n'
    )
    root = repo(make_repo, {"app/mod.py": CLOCK_CALL}, manifest)
    assert [d.code.value for d in diagnostics(root, "impure-call")] == []
    assert ("legal-read-recorded", "app/meizel.toml") in failures(root)


# meiva:note
# Only the kind the root declares is licensed: a root that pays for another kind's call is exactly
# what `[debt.impure-call]` is for, and the exemption must not swallow it.
def test_a_root_paying_for_a_call_of_another_kind_is_no_conflict(make_repo: RepoFactory) -> None:
    manifest = (
        '[purity]\n"mod.py" = "strict-to-be"\n'
        '[roots]\n"mod.py" = { kinds = ["environ"], why = "reads the environment" }\n'
        '\n[debt.impure-call]\n"mod.py#time.time" = 1\n'
    )
    source = "import os\nimport time\n\nenv = os.environ\n\ndef now() -> float:\n    return time.time()\n"
    root = repo(make_repo, {"app/mod.py": source}, manifest)
    assert [d.code.value for d in diagnostics(root, "impure-call")] == ["impure-call"]
    assert "legal-read-recorded" not in {code for code, _ in failures(root)}


# meiva:note
# The other face of the same rule: no entry, no violation. A strict root that reads the kind it
# declares is clean, so the licensing is a property of the rule and not of the debt table.
def test_a_licensed_call_in_a_strict_root_is_no_violation(make_repo: RepoFactory) -> None:
    manifest = (
        '[purity]\n"mod.py" = "strict"\n'
        '[roots]\n"mod.py" = { kinds = ["clock"], why = "the clock door" }\n'
    )
    root = repo(make_repo, {"app/mod.py": CLOCK_CALL}, manifest)
    assert analysis_of(root).verdict == []


# meiva:note
# The boundary this rule keeps: sleeping is an effect, not a read of any ambient kind
# (`meizel/graph/ambient_process_table.py`, the `time` table), so a clock root that waits still owes
# the record. Suppressing it would turn `[roots]` into a waiver of every effect in that file.
def test_a_wait_in_a_clock_root_keeps_its_record(make_repo: RepoFactory) -> None:
    manifest = (
        '[purity]\n"mod.py" = "strict"\n'
        '[roots]\n"mod.py" = { kinds = ["clock"], why = "the clock door" }\n'
    )
    source = "import time\n\ndef pause(seconds: float) -> None:\n    time.sleep(seconds)\n"
    root = repo(make_repo, {"app/mod.py": source}, manifest)
    assert [d.message for d in diagnostics(root, "impure-call")] == [
        "impure call `time.sleep` in strict module"
    ]


# meiva:note
# A call the ambient table names only by its method name (`Path.expanduser`) is licensed on the line
# the scan records the acquisition on: the two rules see one call from two sides, and the table
# carries no dotted name for it to join on.
def test_a_call_with_no_judged_name_is_licensed_on_its_line(make_repo: RepoFactory) -> None:
    manifest = (
        '[purity]\n"mod.py" = "strict"\n'
        '[roots]\n"mod.py" = { kinds = ["home"], why = "resolves the home anchor" }\n'
    )
    source = (
        "from pathlib import Path\n\ndef home() -> str:\n    return str(Path('~/x').expanduser())\n"
    )
    root = repo(make_repo, {"app/mod.py": source}, manifest)
    assert analysis_of(root).verdict == []
    scan = analysis_of(root).scans["app/mod.py"]
    assert [(a.kind, a.target) for a in scan.ambient] == [("home", "pathlib.Path.expanduser")]
