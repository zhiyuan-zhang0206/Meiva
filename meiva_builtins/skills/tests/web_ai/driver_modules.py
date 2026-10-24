# meiva:note
# The modules of the web-ai driver, loaded the way the skill scripts are loaded: by file path, each registered in `sys.modules` under the bare name its siblings import it by (`_sites`, `_idle`, ...). A test therefore holds the very objects the driver holds, and the profile and result classes it builds are the ones the driver matches on. Every call loads a fresh set, so a test module keeps the set it loaded.
from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType

from meiva_builtins.skills.tests.skill_script import load_skill_script


# meiva:note
# The loaded modules of the driver, by their file stem without the leading underscore.
@dataclass(frozen=True)
class DriverModules:
    sites: ModuleType
    idle: ModuleType
    pacing: ModuleType
    js: ModuleType
    pages: ModuleType
    rows: ModuleType
    landing: ModuleType
    dom: ModuleType
    utils: ModuleType
    webchat: ModuleType


def _load(stem: str) -> ModuleType:
    return load_skill_script("integrations", "web-ai", "scripts", f"{stem}.py", name=stem)


# meiva:note
# Load the driver's modules in dependency order, so each import of a sibling finds the module registered just before.
def load_driver_modules() -> DriverModules:
    sites = _load("_sites")
    idle = _load("_idle")
    pacing = _load("_pacing")
    js = _load("_js")
    pages = _load("_pages")
    rows = _load("_rows")
    landing = _load("_landing")
    dom = _load("_dom")
    utils = _load("_utils")
    webchat = _load("webchat")
    return DriverModules(sites, idle, pacing, js, pages, rows, landing, dom, utils, webchat)
