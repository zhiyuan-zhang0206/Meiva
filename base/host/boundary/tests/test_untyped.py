import json

from base.host.boundary.untyped import object_mapping, object_of, object_sequence


def test_object_of_hands_back_the_call_answer_unchanged() -> None:
    decoded = object_of(json.loads, '{"a": [1, 2]}')
    assert decoded == {"a": [1, 2]}


def test_object_of_forwards_positional_and_keyword_arguments() -> None:
    assert object_of(int, "ff", base=16) == 255


def test_object_mapping_and_sequence_hand_the_same_container_back() -> None:
    mapping = {"a": 1}
    items = [1, 2]
    assert object_mapping(mapping) is mapping
    assert object_sequence(items) is items
