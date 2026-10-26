# meiva:note
# A writer process that dies hard at a chosen phase of a silent upload batch.
#
# Each phase leaves exactly the state a process killed at that point leaves: the reservation
# only, the first object linked, every object linked but no receipt, or the committed receipt.
# The phases are the public steps of `batches`, run in order and cut short by `os._exit`.

import hashlib
import json
import os
import sys
from pathlib import Path

import psycopg
from psycopg_pool import ConnectionPool

_ITEMS = [("a.png", b"aaa", "image/png"), ("b.png", b"bbb", "image/png")]
_NAMES = ["a.png", "b.png"]
_DEATH = 41


def main() -> None:
    config = json.load(sys.stdin)
    os.environ["MEIVA_HOME"] = config["home"]
    os.environ["MEIVA_CONFIG_FETCH"] = "skip"
    from gateway.routers.upload import batches as owner

    directory = Path(config["directory"])
    key = config.get("key", "crash")
    batch_id = hashlib.sha256(f"{key}:batch".encode()).hexdigest()[:32]
    staging_id = hashlib.sha256(f"{key}:staging".encode()).hexdigest()[:32]
    with ConnectionPool[psycopg.Connection](config["dsn"], min_size=1, max_size=1) as pool:
        if config["phase"] == "ready":
            owner.save_keyed_batch(
                pool,
                key,
                123,
                directory,
                _ITEMS,
                _NAMES,
                batch_id=batch_id,
                staging_id=staging_id,
                max_bytes=6,
                max_files=2,
                local_machine=config["machine"],
            )
            os._exit(_DEATH)
        claim = owner.claim_batch(
            pool,
            key,
            123,
            directory,
            _ITEMS,
            _NAMES,
            batch_id=batch_id,
            max_bytes=6,
            max_files=2,
            local_machine=config["machine"],
        )
        if config["phase"] == "first-file":
            owner.publish_files(directory, claim.manifest[:1], _ITEMS[:1], staging_id=staging_id)
        elif config["phase"] == "published":
            owner.publish_files(directory, claim.manifest, _ITEMS, staging_id=staging_id)
    os._exit(_DEATH)


if __name__ == "__main__":
    main()
