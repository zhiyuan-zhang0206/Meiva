from __future__ import annotations

from base.paths import meiva_home
from services.supervision.roster import composition

_KEEPABLE_UNITS = ("loki", "prometheus", "tempo", "grafana", "otel-collector")


def test_the_five_keepable_observability_units_are_roster_entries_that_need_no_database() -> None:
    by_name = {spec.session: spec for spec in composition.build_services(home=meiva_home())}
    for unit in _KEEPABLE_UNITS:
        assert unit in by_name, unit
        assert not by_name[unit].requires_db, unit
