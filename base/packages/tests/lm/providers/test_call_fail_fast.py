# meiva:note
# Unknown errors retain identity and never enter retry waiting, even with a provider cause.
# meiva:link base/packages/tests/lm/docs/contracts.meiva.okf.md

from pathlib import Path

import pytest

from base.packages.tests.lm.providers.lm_process import run_fail_fast


@pytest.mark.parametrize("error", [TypeError("bad code"), ValueError("bad input")])
@pytest.mark.parametrize("provider_cause", [False, True])
def test_unknown_runnable_error_is_not_retried_or_wrapped(
    tmp_path: Path,
    error: Exception,
    provider_cause: bool,
) -> None:
    run_fail_fast(tmp_path, type(error).__name__, str(provider_cause))
