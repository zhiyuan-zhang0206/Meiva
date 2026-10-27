import json

import httpx
import pytest

from base.agents.context.gateway_client import transport


def test_a_json_object_body_is_returned_as_the_object() -> None:
    response = httpx.Response(200, json={"status": "enqueued", "open_tasks": []})

    assert transport.json_object(response) == {"status": "enqueued", "open_tasks": []}


@pytest.mark.parametrize("body", [[1, 2], "enqueued", 7, None])
def test_a_body_that_is_not_an_object_is_refused_by_json_object(body: object) -> None:
    response = httpx.Response(200, content=json.dumps(body).encode())

    with pytest.raises(TypeError, match="not a JSON object"):
        transport.json_object(response)


def test_a_json_array_of_objects_is_returned_as_rows() -> None:
    response = httpx.Response(200, json=[{"id": 1}, {"id": 2}])

    assert transport.json_rows(response) == [{"id": 1}, {"id": 2}]
    assert transport.json_rows(httpx.Response(200, json=[])) == []


@pytest.mark.parametrize("body", [{"id": 1}, "rows", None])
def test_a_body_that_is_not_an_array_is_refused_by_json_rows(body: object) -> None:
    response = httpx.Response(200, content=json.dumps(body).encode())

    with pytest.raises(TypeError, match="not a JSON array"):
        transport.json_rows(response)


def test_a_row_that_is_not_an_object_is_refused_by_json_rows() -> None:
    response = httpx.Response(200, json=[{"id": 1}, 2])

    with pytest.raises(TypeError, match="row is not a JSON object"):
        transport.json_rows(response)
