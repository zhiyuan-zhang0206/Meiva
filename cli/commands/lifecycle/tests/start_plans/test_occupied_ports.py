# meiva:note
# What a start says when another unit already answers on a health port it was about to bind.

from __future__ import annotations

from cli.commands.lifecycle.start.occupied_ports import Occupant, refusal_text

_FOREIGN = (
    "identity mismatch on http://localhost:8103/healthz: home='/home/meiva/.meiva' != "
    "'/home/other/.meiva' — another unit's daemon holds this port"
)


def test_the_report_names_the_occupant_the_remedy_and_the_stopgap() -> None:
    text = refusal_text((Occupant("agent-host", "meiva-agent-host", _FOREIGN),))

    assert "    meiva-agent-host: " + _FOREIGN in text
    assert "/home/meiva/.meiva" in text
    assert "Not starting — NOTHING was launched" in text
    assert "Stop the listed daemon" in text
    assert "--disable-service agent-host" in text


# meiva:note
# An operator moving a unit needs the whole overlap in one pass: reporting only the first
# port turns one fix into a sequence of restarts, each revealing the next collision.
def test_every_conflicting_port_is_reported_not_just_the_first() -> None:
    text = refusal_text(
        (
            Occupant("labeler", "meiva-labeler", "a"),
            Occupant("ops", "meiva-ops", "b"),
        )
    )

    assert "    meiva-labeler: a\n    meiva-ops: b\n" in text
    assert text.endswith(
        "drop the listed daemons from this start: --disable-service labeler "
        "--disable-service ops\n"
        "  (that daemon then does not run at all — it is a stopgap, not the fix)."
    )


def test_the_report_opens_with_the_blank_line_and_cross_of_a_refusal() -> None:
    text = refusal_text((Occupant("ops", "meiva-ops", "x"),))

    assert text.startswith("\n✗ another unit already answers on this unit's daemon health ports:\n")
