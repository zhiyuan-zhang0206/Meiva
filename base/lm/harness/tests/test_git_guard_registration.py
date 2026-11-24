from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from base.host.env.harness import git_zero_trace_guard as guard
from base.lm.harness.registration import (
    HANDLE_ENV,
    HarnessRegistration,
    RegistrationStore,
    handle_digest,
)


def test_the_guard_resolves_a_handle_through_a_registration_the_store_wrote(tmp_path: Path) -> None:
    store = RegistrationStore(tmp_path / "harness" / "registrations")
    stamp = datetime.now(UTC).isoformat()
    store.write(
        HarnessRegistration(
            agent_id=4242,
            harness="codex",
            external_session_id="s",
            machine="m",
            controller_pid=1,
            handle_sha256=handle_digest("h-secret"),
            created_at=stamp,
            expires_at=stamp,
        )
    )
    environ = {"MEIVA_HOME": str(tmp_path), HANDLE_ENV: "h-secret"}
    assert guard.HANDLE_ENV == HANDLE_ENV
    assert guard.self_agent_id(environ) == 4242
    assert guard.self_agent_id({**environ, HANDLE_ENV: "other"}) is None
    assert guard.self_agent_id({**environ, "MEIVA_AGENT_ID": "77"}) == 77
