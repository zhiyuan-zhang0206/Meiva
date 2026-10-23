import json
import pathlib
import sys

import pytest

from base.host.proc import run_bounded
from base.log.tests.log_records import LogRecords
from meiva_builtins.plugins.meiva_syntax_fix._imports import detect_missing_imports, insert_imports
from meiva_builtins.plugins.meiva_syntax_fix._punct import (
    FULLWIDTH_QUOTE_MAP,
    MAX_TOKENIZE_LINE_LENGTH,
    PUNCT_MAP,
    translate_outside_strings,
)
from meiva_builtins.plugins.meiva_syntax_fix._ruff import (
    ruff_fix,
    ruff_format,
    ruff_undefined_names,
)
from meiva_builtins.plugins.meiva_syntax_fix._ruff_plan import RuffRun
from meiva_builtins.plugins.meiva_syntax_fix._ruff_run import ruff_executable, system_ruff
from meiva_builtins.plugins.meiva_syntax_fix.agent_runtime import fix_chinese_punctuation

_RUFF = system_ruff()
_NO_PATH_CHILD = pathlib.Path(__file__).with_name("child_ruff_without_path.py")


def scripted_ruff(directory: pathlib.Path, body: str, *, timeout_seconds: float = 5) -> RuffRun:
    path = directory / "ruff"
    path.write_text(f"#!{sys.executable}\nimport sys\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return RuffRun(executable=str(path), timeout_seconds=timeout_seconds)


_ECHO = "sys.stdout.buffer.write(sys.stdin.buffer.read())"
_SLEEP = "import time\ntime.sleep(30)"


class TestFixChinesePunctuation:
    def test_no_chinese_no_change(self):
        code, n = fix_chinese_punctuation('print("hello")')
        assert code == 'print("hello")'
        assert n == 0

    def test_chinese_comma_to_ascii(self):
        code, n = fix_chinese_punctuation("print(1\uff0c2)")
        assert code == "print(1,2)"
        assert n == 1

    def test_chinese_period_to_dot(self):
        code, n = fix_chinese_punctuation("import os\u3002")
        assert code == "import os."
        assert n == 1

    def test_chinese_left_quote_to_ascii(self):
        code, n = fix_chinese_punctuation("print\uff081\uff09")
        assert code == "print(1)"
        assert n == 2

    def test_multiple_replacements(self):
        code, n = fix_chinese_punctuation("a\uff0cb\u3002c\uff08d\uff09")
        assert code == "a,b.c(d)"
        assert n == 4

    def test_empty_code(self):
        code, n = fix_chinese_punctuation("")
        assert code == ""
        assert n == 0

    def test_in_string_punctuation_preserved(self):
        src = 'msg = "\u4f60\u597d\uff0c\u4e16\u754c"'
        code, n = fix_chinese_punctuation(src)
        assert code == src
        assert n == 0

    def test_in_triple_quote_preserved(self):
        src = 'p = """\u95ee\u9898\u80cc\u666f\uff1a\u8bf7\u7528 \u201c\u667a\u80fd\u201d \u6a21\u5f0f\u3002"""'
        code, n = fix_chinese_punctuation(src)
        assert code == src
        assert n == 0
        compile(code, "<t>", "exec")

    def test_comment_punctuation_preserved(self):
        src = "x = 1  # \u8c03\u7528\uff08\u91cd\u8981\uff09"
        code, n = fix_chinese_punctuation(src)
        assert code == src
        assert n == 0

    def test_code_position_fixed_but_string_preserved(self):
        src = 'print\uff08"a\uff0cb"\uff09'
        code, n = fix_chinese_punctuation(src)
        assert code == 'print("a\uff0cb")'
        assert n == 2

    def test_fullwidth_quote_delimiters_fixed_interior_preserved(self):
        src = "msg = \u201c\u4f60\u597d\uff0c\u4e16\u754c\u201d"
        code, n = fix_chinese_punctuation(src)
        assert code == 'msg = "\u4f60\u597d\uff0c\u4e16\u754c"'
        assert n == 2
        compile(code, "<t>", "exec")

    def test_tokenize_failure_returns_unchanged(self):
        src = 'x = ("a\uff0cb"'
        code, n = fix_chinese_punctuation(src)
        assert code == src
        assert n == 0


class TestTranslateOutsideStringsCrashGuard:
    CRASH_INPUT = (
        "x = f'''() => {\n"
        "      x = 1;\n"
        "      return {height: r.height,\n"
        "              brandAbsent: !h.query('a') && !h.query('b'),\n"
        '    }""")\n'
        "'''"
    )

    @pytest.mark.skipif(
        sys.version_info < (3, 12),
        reason="the guarded C-tokenizer crash exists on 3.12+ (SystemError on "
        "3.12, MemoryError on 3.13+; the guard degrades both)",
    )
    def test_crash_input_degrades_to_noop_quote_pass(self):
        code, n = translate_outside_strings(self.CRASH_INPUT, FULLWIDTH_QUOTE_MAP)
        assert code == self.CRASH_INPUT
        assert n == 0

    @pytest.mark.skipif(
        sys.version_info < (3, 12),
        reason="the guarded C-tokenizer crash exists on 3.12+ (SystemError on "
        "3.12, MemoryError on 3.13+; the guard degrades both)",
    )
    def test_crash_input_degrades_to_noop_punct_pass(self):
        code, n = translate_outside_strings(self.CRASH_INPUT, PUNCT_MAP)
        assert code == self.CRASH_INPUT
        assert n == 0

    @pytest.mark.skipif(
        sys.version_info < (3, 12),
        reason="3.11 tokenizes the input (and would translate the fullwidth "
        "comma); the guard degrades the 3.12+ crash manifestations",
    )
    def test_pipeline_degrades_on_crash_input(self):
        src = self.CRASH_INPUT.replace("() => {", "() => {\uff0c")
        code, n = fix_chinese_punctuation(src)
        assert code == src
        assert n == 0

    @pytest.mark.skipif(
        sys.version_info < (3, 12),
        reason="3.11 tokenizes the input (and would translate the fullwidth "
        "comma); the guard degrades the 3.12+ crash manifestations",
    )
    def test_escapes_fixer_degrades_on_crash_input(self):
        from meiva_builtins.plugins.meiva_syntax_fix._escapes import fix_invalid_escapes

        code, n = fix_invalid_escapes(self.CRASH_INPUT)
        assert code == self.CRASH_INPUT
        assert n == 0

    def test_oversized_line_skips_translation(self):
        src = "a" * MAX_TOKENIZE_LINE_LENGTH + "\uff0c"
        code, n = translate_outside_strings(src, PUNCT_MAP)
        assert code == src
        assert n == 0

    def test_lone_surrogate_skips_translation(self):
        src = "x = \uff0c\ud800"
        code, n = translate_outside_strings(src, PUNCT_MAP)
        assert code == src
        assert n == 0

    def test_guard_does_not_block_normal_translation(self):
        code, n = translate_outside_strings("print(1\uff0c2)", PUNCT_MAP)
        assert code == "print(1,2)"
        assert n == 1


class TestDetectMissingImports:
    def test_ruff_extracts_undefined_name_from_unicode_source(self):
        code = 'print("\u4f60\u597d")\nprint(missing_name)\n'
        assert ruff_undefined_names(code, _RUFF) == {"missing_name"}

    def test_ruff_undefined_names_round_trips_utf8(self, tmp_path: pathlib.Path):
        ruff = scripted_ruff(
            tmp_path,
            "sys.stdin.buffer.read()\n"
            'sys.stdout.buffer.write(\'[{"code":"F821","message":"Undefined name `\\u4f60`"}]\'.encode())',
        )

        assert ruff_undefined_names('print("\u4f60\u597d")\nprint(\u4f60)\n', ruff) == {"\u4f60"}

    def test_empty_code_returns_empty(self):
        assert detect_missing_imports("", _RUFF) == []

    def test_whitespace_only_returns_empty(self):
        assert detect_missing_imports("   \n  ", _RUFF) == []

    def test_no_unknown_usage_returns_empty(self):
        assert detect_missing_imports("x = 1", _RUFF) == []

    def test_detects_json_usage(self):
        result = detect_missing_imports("json.dumps({'a': 1})", _RUFF)
        assert "import json" in result

    def test_detects_math_usage(self):
        result = detect_missing_imports("math.sqrt(4)", _RUFF)
        assert "import math" in result

    def test_detects_re_usage(self):
        result = detect_missing_imports("re.compile(r'.*')", _RUFF)
        assert "import re" in result

    def test_detects_datetime_usage(self):
        result = detect_missing_imports("datetime.datetime.now()", _RUFF)
        assert "import datetime" in result

    def test_skips_already_imported(self):
        code = "import json\njson.dumps({'a': 1})"
        assert detect_missing_imports(code, _RUFF) == []

    def test_skips_already_from_imported(self):
        code = "from json import dumps\ndumps({'a': 1})"
        assert detect_missing_imports(code, _RUFF) == []

    def test_skips_builtins(self):
        assert detect_missing_imports("True", _RUFF) == []
        assert detect_missing_imports("print('hello')", _RUFF) == []
        assert detect_missing_imports("len([1,2])", _RUFF) == []

    def test_detects_os_import(self):
        result = detect_missing_imports("os.getcwd()", _RUFF)
        assert "import os" in result

    def test_detects_sys_import(self):
        result = detect_missing_imports("sys.argv", _RUFF)
        assert "import sys" in result

    def test_detects_time_import(self):
        result = detect_missing_imports("time.sleep(1)", _RUFF)
        assert "import time" in result

    def test_detects_dotted_import_urllib(self):
        result = detect_missing_imports("urllib.parse.urlparse('http://x.com')", _RUFF)
        assert "import urllib.parse" in result

    def test_detects_concurrent_futures(self):
        result = detect_missing_imports("concurrent.futures.ThreadPoolExecutor()", _RUFF)
        assert any("concurrent.futures" in s for s in result)

    def test_multiple_missing_sorted(self):
        code = """
json.dumps(x)
math.sqrt(y)
re.compile(z)
"""
        result = detect_missing_imports(code, _RUFF)
        imports = [s for s in result if s.startswith("import ")]
        assert imports == sorted(imports)

    def test_meiva_transitive_skipped(self):
        code = "os.getcwd()\nsys.argv\ntime.time()"
        result = detect_missing_imports(code, _RUFF)
        stmts = set(result)
        assert "import os" in stmts
        assert "import sys" in stmts
        assert "import time" in stmts

    def test_meiva_detected(self):
        result = detect_missing_imports("meiva.files.read('x')", _RUFF)
        assert "import meiva" in result

    def test_meiva_already_imported_skipped(self):
        code = "import meiva\nmeiva.files.read('x')"
        assert detect_missing_imports(code, _RUFF) == []

    def test_import_below_first_usage_counts_as_missing(self):
        code = "os.environ.get('X')\nimport os\nos.walk('/')\n"
        result = detect_missing_imports(code, _RUFF)
        assert "import os" in result

    def test_import_above_first_usage_skipped(self):
        code = "import os\nos.environ.get('X')\n"
        assert detect_missing_imports(code, _RUFF) == []

    def test_dotted_import_below_first_usage_counts_as_missing(self):
        code = "urllib.parse.urlparse('http://x')\nimport urllib.parse\n"
        assert "import urllib.parse" in detect_missing_imports(code, _RUFF)

    def test_from_import_below_first_usage_counts_as_missing(self):
        code = "json.dumps({'a': 1})\nfrom json import dumps\n"
        assert "import json" in detect_missing_imports(code, _RUFF)


class TestInsertImports:
    def test_simple_code_inserts_at_top(self):
        code = "print('hello')"
        result = insert_imports(code, ["import json"])
        assert result.startswith("import json\n")
        assert "print('hello')" in result

    def test_after_shebang(self):
        code = "#!/usr/bin/env python\nprint('hello')"
        result = insert_imports(code, ["import json"])
        lines = result.split("\n")
        assert lines[0] == "#!/usr/bin/env python"
        assert "import json" in lines[1]

    def test_after_module_docstring(self):
        code = '"""Module docstring.\n"""\nprint(1)'
        result = insert_imports(code, ["import json"])
        lines = result.split("\n")
        assert lines[0] == '"""Module docstring.'
        assert "import json" in result
        assert "print(1)" in result

    def test_after_comments(self):
        code = "# comment 1\n# comment 2\nprint(1)"
        result = insert_imports(code, ["import json"])
        lines = result.split("\n")
        assert lines[0] == "# comment 1"
        assert lines[1] == "# comment 2"
        assert "import json" in lines[2]

    def test_empty_imports_no_change(self):
        code = "print(1)"
        result = insert_imports(code, [])
        assert "print(1)" in result

    def test_multiple_imports(self):
        code = "print(1)"
        result = insert_imports(code, ["import json", "import math"])
        assert "import json" in result
        assert "import math" in result


class TestRuffFix:
    def test_ruff_available_runs_fix(self):
        code = 'import os\nimport os\nprint("\u4f60\u597d")\n'
        result = ruff_fix(code, _RUFF)
        assert isinstance(result, str)
        assert "\u4f60\u597d" in result

    def test_ruff_fix_round_trips_utf8(self, tmp_path: pathlib.Path):
        code = 'print("\u4f60\u597d")\n'

        assert ruff_fix(code, scripted_ruff(tmp_path, _ECHO)) == code

    def test_ruff_fix_takes_the_rewrite_of_a_clean_exit(self, tmp_path: pathlib.Path):
        ruff = scripted_ruff(tmp_path, 'sys.stdout.write("rewritten\\n")')

        assert ruff_fix("import os\n", ruff) == "rewritten\n"

    def test_ruff_nonzero_returns_original(self, tmp_path: pathlib.Path):
        ruff = scripted_ruff(tmp_path, 'sys.stderr.write("error")\nsys.exit(1)')

        assert ruff_fix("import os\n", ruff) == "import os\n"

    def test_ruff_empty_stdout_returns_original(self, tmp_path: pathlib.Path):
        assert ruff_fix("import os\n", scripted_ruff(tmp_path, "pass")) == "import os\n"


class TestRuffFormat:
    def test_ruff_available_normalizes_style(self):
        result = ruff_format('message="\u4f60\u597d"\n', _RUFF)
        assert result == 'message = "\u4f60\u597d"\n'

    def test_ruff_format_round_trips_utf8(self, tmp_path: pathlib.Path):
        code = 'print("\u4f60\u597d")\n'

        assert ruff_format(code, scripted_ruff(tmp_path, _ECHO)) == code

    def test_ruff_nonzero_returns_original(self, tmp_path: pathlib.Path):
        ruff = scripted_ruff(tmp_path, 'sys.stderr.write("error")\nsys.exit(2)')

        assert ruff_format("x=1\n", ruff) == "x=1\n"

    def test_ruff_empty_stdout_returns_original(self, tmp_path: pathlib.Path):
        assert ruff_format("x=1\n", scripted_ruff(tmp_path, "pass")) == "x=1\n"


class TestRuffGiveUpLogging:
    def test_a_missing_ruff_passes_through_and_is_reported_for_each_skipped_stage(
        self, tmp_path: pathlib.Path, loguru_records: LogRecords
    ):
        missing = RuffRun(executable=str(tmp_path / "no-ruff"))

        for code in ("a = 1\n", "b = 2\n"):
            assert ruff_fix(code, missing) == code
        assert ruff_format("c = 3\n", missing) == "c = 3\n"
        assert ruff_undefined_names("d\n", missing) == frozenset()
        messages = loguru_records.messages("WARNING")
        assert sum("not found" in message for message in messages) == 4, messages

    def test_a_ruff_over_its_budget_passes_through_with_the_budget_and_input_size(
        self, tmp_path: pathlib.Path, loguru_records: LogRecords
    ):
        ruff = scripted_ruff(tmp_path, _SLEEP, timeout_seconds=0.2)

        assert ruff_fix("import os\n", ruff) == "import os\n"
        assert ruff_format("x=1\n", ruff) == "x=1\n"
        assert ruff_undefined_names("import os\n", ruff) == frozenset()
        messages = loguru_records.messages("WARNING")
        assert any(
            "check --fix did not finish within 0.2s on 10-char source" in m for m in messages
        )
        assert any("format did not finish within 0.2s on 4-char source" in m for m in messages)
        assert any("check --select F821 did not finish within 0.2s" in m for m in messages)

    def test_a_ruff_the_os_refuses_to_run_passes_through_with_the_errno(
        self, tmp_path: pathlib.Path, loguru_records: LogRecords
    ):
        refused = tmp_path / "ruff"
        refused.write_text("not executable", encoding="utf-8")
        ruff = RuffRun(executable=str(refused))

        assert ruff_fix("import os\n", ruff) == "import os\n"
        assert ruff_undefined_names("import os\n", ruff) == frozenset()
        messages = loguru_records.messages("WARNING")
        assert any("errno=13" in m and "Permission denied" in m for m in messages), messages

    def test_source_the_text_codec_cannot_encode_passes_through(
        self, tmp_path: pathlib.Path, loguru_records: LogRecords
    ):
        code = 'print("\u4f60")\n\ud800\n'
        ruff = scripted_ruff(tmp_path, _ECHO)

        assert ruff_fix(code, ruff) == code
        assert ruff_format(code, ruff) == code
        assert ruff_undefined_names(code, ruff) == frozenset()
        messages = loguru_records.messages("WARNING")
        assert sum("UnicodeEncodeError" in message for message in messages) == 3, messages


class TestRuffExecutableResolution:
    def test_resolves_to_the_interpreters_own_ruff(self):
        resolved = pathlib.Path(ruff_executable())
        assert resolved.is_file()
        assert resolved.parent == pathlib.Path(sys.executable).parent

    def test_ruff_backed_fixers_work_with_ruff_absent_from_path(self):
        run = run_bounded(
            [sys.executable, "-I", str(_NO_PATH_CHILD)],
            env={"PATH": ""},
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert run.returncode == 0, run.stderr
        assert json.loads(run.stdout) == ["x = 1\n", ["import json"]]


def is_broken(code: str) -> bool:
    try:
        compile(code, "<t>", "exec")
    except SyntaxError:
        return True
    return False
