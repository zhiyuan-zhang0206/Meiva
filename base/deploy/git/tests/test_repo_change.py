import pytest

from base.deploy.git.repo_change import classify_change, is_doc_path


@pytest.mark.parametrize(
    "path",
    [
        "docs/contributing.md",
        "docs/decisions/foo.md",
        "docs/postmortems/x.md",
        "future/y.md",
        "docs/conventions/z.md",
        "docs/README.md",
        "assets/img.png",
        "schedules/x.json",
        "README.md",
        "AGENTS.md",
    ],
)
def test_is_doc_path(path: str) -> None:
    assert is_doc_path(path)


@pytest.mark.parametrize("path", ["ui/web/AGENTS.md", "gateway/README.md"])
def test_is_doc_path_leaves_nested_code_docs_to_their_directory(path: str) -> None:
    assert not is_doc_path(path)


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (["ui/web/app/page.tsx", "ui/web/src/lib/transport/api.ts"], (True, False)),
        (["agent/graph/exec/node.py"], (False, True)),
        (
            [
                "docs/decisions/foo.md",
                "docs/postmortems/x.md",
                "future/y.md",
                "docs/conventions/z.md",
                "docs/README.md",
                "assets/img.png",
                "schedules/x.json",
            ],
            (False, False),
        ),
        (["README.md", "AGENTS.md"], (False, False)),
        (["ui/web/AGENTS.md"], (True, False)),
        (["gateway/README.md"], (False, True)),
        ([], (False, False)),
        (["ui/web/app/page.tsx", "agent/graph/exec/node.py"], (True, True)),
    ],
)
def test_classify_change(paths: list[str], expected: tuple[bool, bool]) -> None:
    assert classify_change(paths) == expected


@pytest.mark.parametrize(
    "path",
    [
        "schedules/tests/test_x.py",
        "schedules/tests/conftest.py",
        "schedules/tests/meizel.toml",
        "schedules/tests/path_scopes.toml",
    ],
)
def test_a_test_under_a_doc_root_is_code(path: str) -> None:
    assert not is_doc_path(path)
    assert classify_change([path]) == (False, True)


@pytest.mark.parametrize(
    "path",
    [
        "schedules/db-write-stall-watch-schedule.py",
        "schedules/jobs/catchup.py",
        "schedules/docs/entry-composition.md",
    ],
)
def test_schedule_templates_stay_documentation(path: str) -> None:
    assert is_doc_path(path)
    assert classify_change([path]) == (False, False)


def test_component_okf_docs_do_not_select_a_code_suite() -> None:
    paths = [
        "scripts/lint/docs/changed-files-mode.md",
        "base/deploy/docs/deploy.md",
        "gateway/docs/gateway.md",
        "ui/web/src/docs/frontend-components/page.md",
        "meiva_builtins/skills/meiva-guide/docs/agents.md",
    ]
    for path in paths:
        assert is_doc_path(path), path
        assert classify_change([path]) == (False, False), path


def test_component_doc_rules_preserve_code_and_test_inputs() -> None:
    cases = {
        "scripts/lint/tests/docs/expected.md": (False, True),
        "base/lm/tests/docs/expected.md": (False, True),
        "gateway/docs/tests/expected.md": (False, True),
        "tests/docs/expected.md": (False, True),
        "ui/web/tests/docs/expected.md": (True, False),
        "gateway/gateway.md": (False, True),
        "gateway/docs/config.json": (False, True),
        "gateway/docs/AGENTS.md": (False, True),
        "meiva_builtins/skills/meiva-guide/docs/SKILL.md": (False, True),
    }
    for path, expected in cases.items():
        assert not is_doc_path(path), path
        assert classify_change([path]) == expected, path

    assert classify_change(["gateway/docs/gateway.md", "gateway/api.py"]) == (
        False,
        True,
    )
