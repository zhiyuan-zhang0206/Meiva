# meiva:note
# A row is never handed to a harness again under a key the harness already recorded (task #7423):
# each handback names a new key, and the key an earlier handover was recorded under is still found.
# meiva:link base/lm/harness/docs/delivery.md

from base.agents.messages.harness_input_key import (
    HANDBACK_COUNT_FIELD,
    HANDED_KEYS_FIELD,
    INPUT_KEY_FIELD,
    handback_count,
    handed_input_keys,
    recorded_input_key,
    row_input_key,
)


def test_a_row_never_handed_back_keeps_its_first_key() -> None:
    assert row_input_key(42, None) == "inbound-42"
    assert row_input_key(42, {"note_tag": "sdk_hint"}) == "inbound-42"


def test_every_handback_names_a_key_no_earlier_handover_used() -> None:
    keys = [row_input_key(42, {HANDBACK_COUNT_FIELD: n} if n else None) for n in range(4)]

    assert keys == [
        "inbound-42",
        "inbound-42-handback-1",
        "inbound-42-handback-2",
        "inbound-42-handback-3",
    ]
    assert len(set(keys)) == len(keys)


def test_a_group_key_names_the_input_a_row_rode_in_but_never_hands_a_row_over() -> None:
    payload = {INPUT_KEY_FIELD: "inbound-40", HANDBACK_COUNT_FIELD: 1}

    assert recorded_input_key(42, payload) == "inbound-40"
    assert row_input_key(42, payload) == "inbound-42-handback-1"
    assert recorded_input_key(42, {HANDBACK_COUNT_FIELD: 2}) == "inbound-42-handback-2"


def test_a_count_that_is_not_a_count_is_no_handback() -> None:
    assert handback_count({HANDBACK_COUNT_FIELD: "3"}) == 3
    assert handback_count({HANDBACK_COUNT_FIELD: True}) == 0
    assert handback_count({HANDBACK_COUNT_FIELD: -1}) == 0
    assert handback_count({HANDBACK_COUNT_FIELD: "x"}) == 0


def test_a_handed_back_row_is_known_by_every_key_it_was_handed_over_under() -> None:
    payload = {HANDBACK_COUNT_FIELD: 2, HANDED_KEYS_FIELD: ["inbound-40", "inbound-42-handback-1"]}

    assert handed_input_keys(42, payload) == (
        "inbound-40",
        "inbound-42-handback-1",
        "inbound-42-handback-2",
    )
    assert handed_input_keys(42, None) == ("inbound-42",)
    assert handed_input_keys(42, {HANDED_KEYS_FIELD: "not a list"}) == ("inbound-42",)
