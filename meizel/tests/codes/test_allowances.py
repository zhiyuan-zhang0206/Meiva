from __future__ import annotations

import ast
from pathlib import Path

import pytest

from meizel import allowances
from meizel.tests.synthetic import RepoFactory

ROOT = Path(__file__).resolve().parents[3]
POLICY = (
    '[ambient-state]\npure-callees = ["re.compile", "typing.TypeVar"]\n\n'
    '[ambient-state.sites]\n"a.py::hidden-cache:f" = "a pure derivation"\n\n'
    '[single-owner.postgres-dial]\n"b.py" = "cannot go through the owner"\n'
)


def test_a_rule_reads_its_sections_from_the_policy(make_repo: RepoFactory) -> None:
    policy = allowances.load(make_repo({"meizel.policy.toml": POLICY}))
    assert policy.names("ambient-state", "pure-callees") == ("re.compile", "typing.TypeVar")
    assert policy.reasons("ambient-state", "sites") == {"a.py::hidden-cache:f": "a pure derivation"}
    assert policy.reasons("single-owner", "postgres-dial") == {
        "b.py": "cannot go through the owner"
    }


def test_a_section_the_file_does_not_have_lists_nothing(make_repo: RepoFactory) -> None:
    policy = allowances.load(make_repo({"meizel.policy.toml": POLICY}))
    assert policy.names("ambient-state", "wiring-callees") == ()
    assert policy.reasons("type-checking", "files") == {}


def test_an_absent_policy_lists_nothing(make_repo: RepoFactory) -> None:
    root = make_repo({"a.py": ""})
    (root / allowances.POLICY).unlink(missing_ok=True)
    assert allowances.load(root).reasons("type-checking", "files") == {}


@pytest.mark.parametrize(
    ("rule", "section", "shape"),
    [
        ("ambient-state", "no-such-section", "reasons"),
        ("no-such-rule", "files", "reasons"),
        ("ambient-state", "pure-callees", "reasons"),
        ("type-checking", "files", "names"),
        ("single-owner", "postgres-dial", "names"),
    ],
)
def test_a_rule_cannot_ask_for_a_section_that_does_not_exist(
    make_repo: RepoFactory, rule: str, section: str, shape: str
) -> None:
    policy = allowances.load(make_repo({"meizel.policy.toml": POLICY}))
    with pytest.raises(KeyError):
        (policy.reasons if shape == "reasons" else policy.names)(rule, section)


def test_a_malformed_policy_is_an_error_for_the_rule_that_reads_it(make_repo: RepoFactory) -> None:
    root = make_repo({"meizel.policy.toml": '[type-checking.files]\n"a.py" = ""\n'})
    with pytest.raises(
        ValueError, match=r"\[type-checking.files\] 'a.py' needs a non-empty reason"
    ):
        allowances.load(root)


def test_the_repository_policy_is_well_formed() -> None:
    _, problems = allowances.parse((ROOT / allowances.POLICY).read_text(encoding="utf-8"))
    assert problems == []


# meiva:note
# `Clock.from_settings()` builds the process clock from the process settings: the read the root
# declares is `settings`, and the clock it returns runs on the system timers, so it is not a second
# declaration of `clock`. Taking it out of `settings` would put the declarations of its callers back.
def test_the_process_clock_constructor_is_a_settings_read() -> None:
    policy = allowances.load(ROOT)
    reader = "base.clock.Clock.from_settings()"
    assert reader in policy.reasons("ambient-readers", "settings")
    assert reader not in policy.reasons("ambient-readers", "clock")


def test_no_rule_code_holds_an_exemption_of_its_own() -> None:
    policy = allowances.load(ROOT)
    exempt = {name for entries in policy.listed.values() for name in entries if "/" in name}
    assert len(exempt) > 50
    literals = {
        node.value: path.relative_to(ROOT).as_posix()
        for path in (ROOT / "meizel").rglob("*.py")
        if "tests" not in path.relative_to(ROOT).parts
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert {name: literals[name] for name in exempt & literals.keys()} == {}


@pytest.mark.parametrize("section", sorted(allowances.RETIRED_AMBIENT_SECTIONS))
def test_only_the_snapshot_decoder_accepts_each_retired_names_list(section: str) -> None:
    text = f'[ambient-state]\n{section} = ["base.db", "base.db:Database"]\n'
    assert allowances.parse(text)[1]
    policy, problems = allowances.parse_snapshot(text)
    assert problems == []
    assert policy.named[("ambient-state", section)] == ("base.db", "base.db:Database")


@pytest.mark.parametrize("value", ["3", '"base.db"', '["base.db", 3]', '[""]', '{x="why"}'])
def test_snapshot_decoder_preserves_retired_shape_validation(value: str) -> None:
    assert allowances.parse_snapshot(f"[ambient-state]\ndb-handles = {value}\n")[1]


@pytest.mark.parametrize("text", ["[unknown]\n", "[ambient-state]\nunknown = []\n", "["])
def test_snapshot_decoder_rejects_every_other_unknown_or_malformed_input(text: str) -> None:
    assert allowances.parse_snapshot(text)[1]


# meiva:note
# The allowance tables are a review surface, not a convenience: each entry says one file may do what
# the rule otherwise forbids. Pinning the counts makes adding one a deliberate edit here too, so a
# new allowance cannot arrive inside a change that looks local. Bump a number only with its entry.
def test_repo_policy_allowance_counts_are_pinned() -> None:
    policy = allowances.load(ROOT)
    # meiva:note
    # The 44th external site is `base/packages/plugins/loading/schedule_face.py`: it loads the
    # `schedules` and `ui_stats` faces a discovered plugin declares, and syntax-checks the
    # schedule scripts they hand over. A plugin's face is external code by definition, so the
    # one owner that loads it needs the allowance; #8025 added it and moved the count from 43.
    assert len(policy.external) == 44
    assert len(policy.loads) == 27


# meiva:note
# The declarations a change takes away, and the `Policy-Removal` line that answers them.
ALLOWANCES = allowances.Policy(
    external={"app/runner.py": "executes the schedule script the user wrote"},
    loads={"schedules/jobs/dry_import_child.py": ("schedules", "meiva")},
    default_arguments=("clock",),
    listed={("ambient-readers", "environ"): {"pkg.env.read()": "reads os.environ"}},
    named={("ambient-state", "sink-callees"): ("sink", "sink_now")},
)
_SEPARATOR = " \u2014 "


def _without(
    *,
    external: dict[str, str] | None = None,
    loads: dict[str, tuple[str, ...]] | None = None,
    default_arguments: tuple[str, ...] | None = None,
    listed: dict[tuple[str, str], dict[str, str]] | None = None,
    named: dict[tuple[str, str], tuple[str, ...]] | None = None,
) -> allowances.Policy:
    return allowances.Policy(
        external=ALLOWANCES.external if external is None else external,
        loads=ALLOWANCES.loads if loads is None else loads,
        default_arguments=ALLOWANCES.default_arguments
        if default_arguments is None
        else default_arguments,
        listed=ALLOWANCES.listed if listed is None else listed,
        named=ALLOWANCES.named if named is None else named,
    )


def _removal(
    declared: str,
    why: str = "the site is gone with its module",
    gap: str = "nothing reads it now",
) -> str:
    return f"Policy-Removal: {declared}{_SEPARATOR}{why}{_SEPARATOR}{gap}"


def test_a_removed_entry_is_one_declaration() -> None:
    report = allowances.removal_report(
        ALLOWANCES, _without(external={"other.py": "runs external code"}), ""
    )
    assert [item.named for item in report.removed] == ["external.app/runner.py"]
    assert [item.named for item in report.added] == ["external.other.py"]


def test_a_shortened_list_loses_exactly_the_names_it_dropped() -> None:
    after = _without(loads={"schedules/jobs/dry_import_child.py": ("schedules",)})
    report = allowances.removal_report(ALLOWANCES, after, "")
    assert [item.named for item in report.removed] == [
        "loads.schedules/jobs/dry_import_child.py -> meiva"
    ]


def test_a_deleted_section_loses_every_name_it_held() -> None:
    report = allowances.removal_report(ALLOWANCES, _without(named={}), "")
    assert [item.named for item in report.removed] == [
        "ambient-state.sink-callees.sink",
        "ambient-state.sink-callees.sink_now",
    ]


def test_a_rewritten_reason_is_not_a_removal() -> None:
    after = _without(external={"app/runner.py": "runs the script the operator names"})
    report = allowances.removal_report(ALLOWANCES, after, "")
    assert (report.removed, report.added) == ((), ())


def test_a_shortened_default_arguments_list_is_a_removal() -> None:
    report = allowances.removal_report(ALLOWANCES, _without(default_arguments=()), "")
    assert [item.named for item in report.removed] == ["default-arguments.clock"]


@pytest.mark.parametrize(
    ("text", "declared"),
    [
        (
            _removal("ambient-readers.environ.pkg.env.read()"),
            "ambient-readers.environ.pkg.env.read()",
        ),
        (_removal("AMBIENT-READERS.ENVIRON"), "AMBIENT-READERS.ENVIRON"),
        (
            "policy-removal:ambient-readers.environ"
            + _SEPARATOR
            + "no reader is left"
            + _SEPARATOR
            + "nothing reads it",
            "ambient-readers.environ",
        ),
    ],
    ids=["entry", "case", "no-space-after-the-colon"],
)
def test_a_declaration_is_read_with_its_three_fields(text: str, declared: str) -> None:
    report = allowances.removal_report(ALLOWANCES, ALLOWANCES, text)
    assert report.problems == ()
    assert [line.declared for line in report.declared] == [declared]
    assert report.declared[0].why
    assert report.declared[0].replacement


def test_a_line_with_two_fields_is_a_problem() -> None:
    report = allowances.removal_report(
        ALLOWANCES,
        ALLOWANCES,
        "Policy-Removal: ambient-readers.environ.pkg.env.read()" + _SEPARATOR + "gone",
    )
    assert report.declared == ()
    assert len(report.problems) == 1


@pytest.mark.parametrize(
    "line",
    [
        _removal("ambient-readers.environ", why="<why it is no longer needed>"),
        _removal("ambient-readers.environ", gap="short"),
        _removal("ambient-readers.environ.pkg.env.read()", gap="<the gap>"),
    ],
    ids=["placeholder-why", "too-short", "placeholder-gap"],
)
def test_a_field_left_unanswered_is_a_problem_when_the_line_names_a_removal(line: str) -> None:
    report = allowances.removal_report(ALLOWANCES, _without(listed={}), line)
    assert report.declared == ()
    assert len(report.problems) == 1
    assert len(report.unanswered) == 1


def test_a_quoted_template_answers_nothing_and_is_not_a_problem() -> None:
    template = (
        "Policy-Removal: <table>[.<entry>] — <why it is no longer needed>"
        " — <what replaces it, or the gap it leaves>"
    )
    report = allowances.removal_report(ALLOWANCES, _without(listed={}), template)
    assert (report.declared, report.problems) == ((), ())
    assert len(report.unanswered) == 1


def test_other_lines_are_not_declarations() -> None:
    report = allowances.removal_report(
        ALLOWANCES, ALLOWANCES, "## Why\nThe policy lost an entry.\n# Policy-Removal: ignored\n"
    )
    assert (report.declared, report.problems) == ((), ())


@pytest.mark.parametrize(
    ("declared", "open_"),
    [
        ("ambient-readers.environ.pkg.env.read()", False),
        ("ambient-readers.environ.environ", True),
        ("ambient-readers.environ", False),
        ("ambient-readers", False),
        ("ambient-state", True),
    ],
    ids=["entry", "longer-than-the-table", "table", "rule", "another-rule"],
)
def test_a_line_answers_the_entry_it_names_or_a_table_that_contains_it(
    declared: str, open_: bool
) -> None:
    report = allowances.removal_report(ALLOWANCES, _without(listed={}), _removal(declared))
    assert bool(report.unanswered) is open_
    assert bool(report.unused) is open_


def test_a_line_that_names_the_entry_answers_it() -> None:
    report = allowances.removal_report(
        ALLOWANCES, _without(external={}), _removal("external.app/runner.py")
    )
    assert (report.unanswered, report.unused) == ((), ())


def test_a_template_names_the_entry_alone_or_the_table_that_lost_several() -> None:
    report = allowances.removal_report(ALLOWANCES, _without(named={}, external={}), "")
    assert [line.split(_SEPARATOR)[0] for line in report.templates()] == [
        "Policy-Removal: ambient-state.sink-callees",
        "Policy-Removal: external.app/runner.py",
    ]


def test_a_template_in_the_body_does_not_answer_a_real_removal() -> None:
    template = (
        "Policy-Removal: <table>[.<entry>] — <why it is no longer needed>"
        " — <what replaces it, or the gap it leaves>"
    )
    report = allowances.removal_report(ALLOWANCES, _without(listed={}), template)
    assert report.problems == ()
    assert len(report.unanswered) == 1


@pytest.mark.parametrize(
    ("line", "missing"),
    [
        (
            "Policy-Removal: external.app/runner.py" + _SEPARATOR + "the module moved away",
            "<what replaces it, or the gap it leaves>",
        ),
        (
            "Policy-Removal: external.app/runner.py" + _SEPARATOR + "nothing reads it now",
            "<what replaces it, or the gap it leaves>",
        ),
        (
            "Policy-Removal:"
            + _SEPARATOR
            + "the module moved"
            + _SEPARATOR
            + "nothing replaces it",
            "`<table>[.<entry>]` name",
        ),
    ],
    ids=["one-field", "name-and-one-field", "no-name"],
)
def test_a_line_that_is_not_three_fields_says_which_field_is_missing(
    line: str, missing: str
) -> None:
    report = allowances.removal_report(ALLOWANCES, ALLOWANCES, line)
    assert len(report.problems) == 1
    assert missing in report.problems[0]
    assert "missing" in report.problems[0]


def test_a_table_answer_covers_only_the_leaves_below_it() -> None:
    other = allowances.Policy(
        external=ALLOWANCES.external,
        loads={},
        default_arguments=ALLOWANCES.default_arguments,
        listed=ALLOWANCES.listed,
        named=ALLOWANCES.named,
    )
    report = allowances.removal_report(ALLOWANCES, other, _removal("ambient-readers.environ"))
    assert [item.named for item in report.unanswered] == [
        "loads.schedules/jobs/dry_import_child.py -> meiva",
        "loads.schedules/jobs/dry_import_child.py -> schedules",
    ]


def test_a_rule_answer_does_not_cover_another_rule() -> None:
    report = allowances.removal_report(ALLOWANCES, _without(named={}), _removal("ambient-readers"))
    assert [item.named for item in report.unanswered] == [
        "ambient-state.sink-callees.sink",
        "ambient-state.sink-callees.sink_now",
    ]
