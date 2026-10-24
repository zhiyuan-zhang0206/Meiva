# meiva:note
# The modules of the gmail skill, loaded the way the skill scripts are loaded: by file path,
# each registered in `sys.modules` under the bare name its siblings import it by
# (`gmail_auth`, `imap`, ...), dependencies first; the CLI entry is `gmail_feed`. A test therefore holds the very objects the
# skill holds, and the error and plan classes it builds are the ones the skill raises and
# matches on. Every call loads a fresh set, so a test module keeps the set it loaded.

from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType

from meiva_builtins.skills.tests.skill_script import load_skill_script


@dataclass(frozen=True)
class GmailModules:
    auth: ModuleType
    headers: ModuleType
    wire: ModuleType
    queries: ModuleType
    compose: ModuleType
    imap: ModuleType
    smtp: ModuleType
    feed: ModuleType


def _load(stem: str, name: str) -> ModuleType:
    return load_skill_script("integrations", "gmail", "scripts", f"{stem}.py", name=name)


def load_gmail_modules() -> GmailModules:
    return GmailModules(
        auth=_load("gmail_auth", "gmail_auth"),
        headers=_load("gmail_headers", "gmail_headers"),
        wire=_load("gmail_wire", "gmail_wire"),
        queries=_load("gmail_queries", "gmail_queries"),
        compose=_load("gmail_compose", "gmail_compose"),
        imap=_load("imap", "imap"),
        smtp=_load("smtp", "smtp"),
        feed=_load("feed", "gmail_feed"),
    )
