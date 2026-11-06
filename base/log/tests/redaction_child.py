# meiva:note
# Child process for the log-redaction test: open the process's own file sink, then log the four
# shapes the rule must separate -- a URL password, a secret-named value in a serialized dump, and
# two strings that must come back byte-identical. Running it in its own interpreter exercises the
# wiring (`init_subprocess_logger` installing the patcher) without patching the module-level guard.

from __future__ import annotations

import sys
from pathlib import Path

from base.log import logger
from base.log.process import init_subprocess_logger

# meiva:note
# Stand-ins for the two shapes the rule masks. Constants rather than literals at the call site:
# the bandit rule reads a literal handed to a `token=`/`password=` argument as a real credential.
_FAKE_PASSWORD = "fixture-value-argument"  # noqa: S105
_FAKE_TOKEN = "fixture-value-mapping"  # noqa: S105
_FAKE_ENV = "fixture-value-env"


def main(home: str) -> None:
    init_subprocess_logger(agent_id=7, home=Path(home))
    logger.info(
        "[exec] {body}",
        event="exec",
        body=(
            'export MEIVA_DB_URL="postgresql://meiva_g0_runner:'
            'fixture-value-url@10.1.2.3:6433/meiva_main"'
        ),
    )
    logger.info(
        "{dump}",
        event="exec",
        dump='{"api":{"gateway":"0e03","token":"fixture-value-dump"},"bundle":"055e"}',
    )
    logger.info("export MEIVA_API_TOKEN=" + _FAKE_ENV, event="log")
    logger.info("fetched https://example.com/path?x=1 in 12ms", event="log")
    logger.info(
        "structured payload",
        event="log",
        token=_FAKE_TOKEN,
        password=_FAKE_PASSWORD,
        api={"token": _FAKE_TOKEN},
        token_count=3,
    )


if __name__ == "__main__":
    main(sys.argv[1])
