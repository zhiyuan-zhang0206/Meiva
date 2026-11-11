# meiva:note
# Which public path a staged upload took, read from its headers: values in, a classification out.

import pytest

from gateway.upload_delivery.ingress import UploadIngress, classify_ingress


def test_a_cloudflare_request_keeps_only_the_edge_code_and_presence_flags() -> None:
    headers = {
        "cf-ray": "8a1b2c3d4e5f6a7b-lax",
        "cf-connecting-ip": "203.0.113.9",
        "host": "Meiva.Example.com",
        "content-length": "1153024",
    }
    assert classify_ingress(headers) == UploadIngress(
        via_cloudflare=True,
        cf_colo="LAX",
        cf_connecting_ip=True,
        host="meiva.example.com",
        content_length=1153024,
    )


def test_a_direct_request_carries_no_cloudflare_facts() -> None:
    assert classify_ingress({"host": "gateway.internal:8000"}) == UploadIngress(
        via_cloudflare=False,
        cf_colo=None,
        cf_connecting_ip=False,
        host="gateway.internal",
        content_length=None,
    )


@pytest.mark.parametrize("ray", ["", "nodash", "abc-", "abc-12", "abc-toolongcolo", "abc-L X"])
def test_a_malformed_ray_still_counts_as_cloudflare_without_a_colo(ray: str) -> None:
    result = classify_ingress({"cf-ray": ray})
    assert (result.via_cloudflare, result.cf_colo) == (True, None)


def test_the_ray_id_and_the_client_address_never_reach_the_result() -> None:
    result = classify_ingress(
        {"cf-ray": "8a1b2c3d4e5f6a7b-SJC", "cf-connecting-ip": "198.51.100.7"}
    )
    assert "8a1b2c3d4e5f6a7b" not in repr(result)
    assert "198.51.100.7" not in repr(result)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("example.com:443", "example.com"),
        ("[::1]:8000", "::1"),
        ("  Example.COM  ", "example.com"),
        ("", None),
        (":8000", None),
        ("a" * 400, "a" * 255),
    ],
)
def test_the_host_is_the_lowercased_name_without_a_port(host: str, expected: str | None) -> None:
    assert classify_ingress({"host": host}).host == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0", 0), (" 42 ", 42), ("-1", None), ("1.5", None), ("abc", None), ("", None), ("٣", None)],
)
def test_a_content_length_is_a_non_negative_integer_or_absent(
    value: str, expected: int | None
) -> None:
    assert classify_ingress({"content-length": value}).content_length == expected
