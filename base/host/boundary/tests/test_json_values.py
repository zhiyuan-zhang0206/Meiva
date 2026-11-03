from base.host.boundary.json_values import is_object_dict, is_object_list


def test_object_dict_accepts_only_a_dict_with_string_keys() -> None:
    assert is_object_dict({"a": 1})
    assert is_object_dict({})
    assert not is_object_dict({1: "a"})
    assert not is_object_dict({"a": 1, 2: "b"})
    assert not is_object_dict([("a", 1)])
    assert not is_object_dict(None)


def test_object_list_accepts_only_a_list() -> None:
    assert is_object_list([1, "a"])
    assert is_object_list([])
    assert not is_object_list(("a",))
    assert not is_object_list({"a": 1})
