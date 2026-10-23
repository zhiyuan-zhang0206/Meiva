# meiva:note
# What a silent upload batch is, from its bytes and names alone: values in, the plan out.

import hashlib

import pytest

from gateway.routers.upload.upload_plan import (
    RESERVED_PREFIX,
    SuffixRefusal,
    UploadItem,
    batch_fingerprint,
    plan_manifest,
)

PNG: UploadItem = ("photo.png", b"image", "image/png")


def test_the_fingerprint_follows_name_bytes_and_type_in_order() -> None:
    base = batch_fingerprint([PNG], ["photo.png"])
    assert base == batch_fingerprint([PNG], ["photo.png"])
    assert base != batch_fingerprint([PNG], ["renamed.png"])
    assert base != batch_fingerprint([("photo.png", b"other", "image/png")], ["photo.png"])
    assert base != batch_fingerprint([("photo.png", b"image", "image/jpeg")], ["photo.png"])
    two = [PNG, ("b.png", b"b", "image/png")]
    assert batch_fingerprint(two, ["photo.png", "b.png"]) != batch_fingerprint(
        two[::-1], ["b.png", "photo.png"]
    )


def test_objects_are_named_by_batch_and_position_keeping_only_the_suffix() -> None:
    items = plan_manifest([PNG, ("b", b"x", "text/plain")], ["etc/passwd.PNG", "b"], "abc123")
    assert items == [
        {
            "filename": "etc/passwd.PNG",
            "stored_name": f"{RESERVED_PREFIX}abc123-0.PNG",
            "size": 5,
            "sha256": hashlib.sha256(b"image").hexdigest(),
            "content_type": "image/png",
        },
        {
            "filename": "b",
            "stored_name": f"{RESERVED_PREFIX}abc123-1",
            "size": 1,
            "sha256": hashlib.sha256(b"x").hexdigest(),
            "content_type": "text/plain",
        },
    ]


@pytest.mark.parametrize(
    "name",
    ["a.p%ng", "a.p?ng", "a.p#ng", "a.pn\x01g", "a.png\x00", "a." + "z" * 129],
)
def test_a_suffix_that_cannot_ride_a_url_refuses_the_whole_batch(name: str) -> None:
    assert plan_manifest([PNG, PNG], ["fine.png", name], "abc") == SuffixRefusal(name)


def test_a_backslash_name_is_read_as_a_path_for_its_suffix() -> None:
    items = plan_manifest([PNG], ["C:\\dir\\shot.png"], "abc")
    assert not isinstance(items, SuffixRefusal)
    assert str(items[0]["stored_name"]).endswith("-0.png")
