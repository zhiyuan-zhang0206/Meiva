from __future__ import annotations

import re
from pathlib import Path

from meiva_builtins.plugins.meiva_usage.default_config import UsageConfig

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def test_default_config_empty() -> None:
    config = UsageConfig()
    assert config.collectors == ()


def test_zero_raw_cjk_characters() -> None:
    cjk_pattern = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af\u3000-\u303f]")

    violations: list[str] = []
    for py_file in _PACKAGE_ROOT.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        matches = cjk_pattern.findall(text)
        if matches:
            violations.append(f"{py_file}: contains CJK characters: {set(matches)}")

    assert not violations, f"Raw CJK characters found: {violations}"


def test_zero_hardcoded_secrets_or_personal_data() -> None:
    secret_patterns = [
        re.compile(r"GOCSPX-[A-Za-z0-9_-]+"),
        re.compile(r"ghp_[A-Za-z0-9]{36}"),
        re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    ]

    violations: list[str] = []
    for py_file in _PACKAGE_ROOT.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for pattern in secret_patterns:
            if pattern.search(text):
                violations.append(f"{py_file}: matches secret pattern {pattern.pattern}")

    assert not violations, f"Forbidden secret patterns found: {violations}"
