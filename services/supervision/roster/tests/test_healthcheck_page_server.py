from __future__ import annotations

from base.paths import meiva_home
from services.supervision.roster.composition import build_services


def _spec_session() -> str:
    specs = build_services(home=meiva_home())
    return next(s.session for s in specs if s.session == "page-server")


def test_spec_session_uses_kebab_case() -> None:
    specs = build_services(home=meiva_home())
    assert any(s.session == "page-server" for s in specs)
