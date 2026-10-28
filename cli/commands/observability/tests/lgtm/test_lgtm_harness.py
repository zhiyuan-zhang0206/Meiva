# meiva:note
# The release mirror the native LGTM tests serve: one build of a release is one set of bytes.
#
# A converge pass reads the pinned SHA from the checkout and verifies what the mirror answers, so
# the bytes `pins()` hashes and the bytes `routes()` serves have to be the same bytes even when the
# two calls land in different seconds.

from __future__ import annotations

import hashlib
import time
from typing import cast

from cli.commands.observability.tests.lgtm.lgtm_harness import Mirror, tar_bytes

BASE_URL = "http://mirror.test"

# meiva:note
# The two builds compared below must land in different wall-clock seconds: an archive that records
# its build time gives one release another SHA at every crossing.
CROSS_SECOND = 1.2


def test_two_builds_of_one_tar_are_byte_identical() -> None:
    entries = {"prometheus-3.13.2.linux-amd64/prometheus": b"#!/bin/sh\n"}

    first = tar_bytes(entries)
    time.sleep(CROSS_SECOND)

    assert tar_bytes(entries) == first


def test_a_mirror_pins_the_archives_it_serves() -> None:
    mirror = Mirror("linux-amd64")
    pins = cast(dict[str, dict[str, dict[str, dict[str, str]]]], mirror.pins(BASE_URL))
    time.sleep(CROSS_SECOND)
    routes = mirror.routes()

    assert {
        asset["url"].removeprefix(BASE_URL): asset["sha256"]
        for spec in pins.values()
        for asset in spec["assets"].values()
    } == {path: hashlib.sha256(reply.body).hexdigest() for path, reply in routes.items()}
