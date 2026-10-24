# meiva:note
# A real unavailable configuration must not prevent constructing independent command roots.

import sys
from pathlib import Path

from cli.database.runtime import CommandRuntime


def main(home: Path) -> None:
    first = CommandRuntime.create(home)
    second = CommandRuntime.create(home)
    assert first.config is not second.config
    assert first.machine is not second.machine
    assert not first.config.prepared and not second.config.prepared

    # meiva:note
    # Prove that skip did not make the broken configuration usable: actual delivery rejects it.
    try:
        first.config.prepare()
    except ValueError as exc:
        message = str(exc)
        assert "required config missing for this local-source unit" in message
        assert "MEIVA_DB_URL" in message and "MEIVA_REDIS_URL" in message
    else:
        raise AssertionError("the child's incomplete configuration was unexpectedly usable")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
