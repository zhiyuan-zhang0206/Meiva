import json

from base.host.boundary.untyped import object_of


def test_object_of_hands_back_the_call_answer_unchanged() -> None:
    decoded = object_of(json.loads, '{"a": [1, 2]}')
    assert decoded == {"a": [1, 2]}


def test_object_of_forwards_positional_and_keyword_arguments() -> None:
    assert object_of(int, "ff", base=16) == 255
