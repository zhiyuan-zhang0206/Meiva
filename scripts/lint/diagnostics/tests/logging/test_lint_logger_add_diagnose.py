from __future__ import annotations

from pathlib import Path

import pytest

from scripts.lint.diagnostics import logger_add_diagnose as _lint


def _violations(src: str) -> list[tuple[int, str]]:
    return _lint.violations_in_source(src)


def test_add_with_no_diagnose_kwarg_is_flagged():
    src = 'logger.add(sys.stderr, format="{message}")\n'
    violations = _violations(src)
    assert len(violations) == 1
    assert "passes no `diagnose=False`" in violations[0][1]


def test_add_with_diagnose_false_is_clean():
    src = 'logger.add(sys.stderr, format="{message}", diagnose=False)\n'
    assert _violations(src) == []


def test_add_with_diagnose_true_is_flagged():
    src = "logger.add(sys.stderr, diagnose=True)\n"
    violations = _violations(src)
    assert len(violations) == 1
    assert "must pass the literal `False`" in violations[0][1]


def test_add_with_non_literal_diagnose_is_flagged():
    src = "logger.add(sys.stderr, diagnose=some_flag)\n"
    violations = _violations(src)
    assert len(violations) == 1
    assert "must pass the literal `False`" in violations[0][1]


def test_add_with_kwargs_unpack_only_is_flagged():
    src = "logger.add(sink, **opts)\n"
    violations = _violations(src)
    assert len(violations) == 1
    assert "passes no `diagnose=False`" in violations[0][1]


def test_add_with_explicit_diagnose_false_and_kwargs_unpack_is_clean():
    src = "logger.add(sink, diagnose=False, **opts)\n"
    assert _violations(src) == []


def test_underscore_logger_alias_is_covered():
    src = "_logger.add(sink, diagnose=True)\n"
    assert len(_violations(src)) == 1


def test_attribute_logger_is_covered():
    src = "self.logger.add(sink)\n"
    violations = _violations(src)
    assert len(violations) == 1
    assert "passes no `diagnose=False`" in violations[0][1]


def test_logger_name_in_any_case_is_covered():
    for receiver in ("_global_logger", "LOGGER", "runLogger", "self.Logger"):
        assert len(_violations(f"{receiver}.add(sink)\n")) == 1, receiver


def test_multiline_call_with_diagnose_false_is_clean():
    src = (
        'logger.add(\n    path,\n    serialize=True,\n    level="DEBUG",\n    diagnose=False,\n)\n'
    )
    assert _violations(src) == []


def test_unrelated_add_call_is_not_a_construction():
    src = "queue.add(item)\n"
    assert _violations(src) == []


def test_explicit_missing_target_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = tmp_path / "ok.py"
    good.write_text("value = 1\n", encoding="utf-8")
    missing = tmp_path / "typo.py"
    assert _lint.main([str(missing)]) == 1
    assert str(missing) in capsys.readouterr().err
    assert _lint.main([str(good), str(missing)]) == 1


def test_directory_with_dangling_symlink_member_is_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "ok.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "dangling.py").symlink_to(tmp_path / "missing.py")
    assert _lint.main([str(tmp_path)]) == 0
    (tmp_path / "viol.py").write_text("logger.add(sys.stderr)\n", encoding="utf-8")
    assert _lint.main([str(tmp_path)]) == 1
    assert "passes no `diagnose=False`" in capsys.readouterr().out


# meiva:note
# The placement rule is the half no runtime hook can enforce: `base.log` disables frame-variable
# rendering on the handlers that exist when it is imported and `add_sink` on the ones registered
# since, but a `logger.add` after startup is invisible to both. A file outside `base/log/` is
# refused whether or not it passes `diagnose=False` — it also skips the redaction patcher.
def test_add_outside_base_log_is_flagged_even_with_diagnose_false():
    src = 'logger.add(sys.stderr, format="{message}", diagnose=False)\n'
    violations = _lint.sink_placement_violations_in_source(src, "agent/takeover/relay.py")
    assert len(violations) == 1
    assert "outside base/log/" in violations[0][1]


def test_add_inside_base_log_is_clean():
    src = 'logger.add(sys.stderr, format="{message}", diagnose=False)\n'
    assert _lint.sink_placement_violations_in_source(src, "base/log/sinks.py") == []


def test_add_inside_a_subdirectory_of_base_log_is_clean():
    src = "logger.add(sink, diagnose=False)\n"
    assert _lint.sink_placement_violations_in_source(src, "base/log/client/extra.py") == []


# meiva:note
# The reverse validation the task card asks for: one `logger.add(sys.stderr)` with loguru's
# defaults is red on both rules at once, so neither rule can be satisfied by the other.
def test_a_default_sink_outside_the_funnel_is_red_on_both_rules():
    src = "logger.add(sys.stderr)\n"

    assert _lint.violations_in_source(src, "agent/takeover/relay.py")
    assert _lint.sink_placement_violations_in_source(src, "agent/takeover/relay.py")


def test_a_scanned_file_outside_base_log_reports_placement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "sink.py").write_text("logger.add(sys.stderr, diagnose=False)\n", encoding="utf-8")
    assert _lint.main([str(tmp_path)]) == 1
    assert "outside base/log/" in capsys.readouterr().out


def test_test_file_is_exempt(tmp_path: Path) -> None:
    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    (test_dir / "test_something.py").write_text("logger.add(sys.stderr)\n", encoding="utf-8")
    assert _lint.main([str(test_dir)]) == 0
