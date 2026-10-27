# meiva:note
# Typed start-delivery operations imported only after the CLI child admits its home.

from pathlib import Path

from base.host.db_authority.model import GenerationSecret
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime

__all__ = ["operator_handles", "verify_start_delivery"]


def operator_handles(home: Path) -> tuple[CommandRuntime, OperatorDatabaseFactory]:
    from cli.database import open_operator_database, operator_database_factory
    from cli.database.runtime import CommandRuntime

    command = CommandRuntime.create(home)
    factory = operator_database_factory(
        local_host=command.machine.host, home=command.home, open_database=open_operator_database
    )
    return command, factory


def verify_start_delivery(
    scenario: str,
    home: Path,
    command: CommandRuntime,
    factory: OperatorDatabaseFactory,
    expected: GenerationSecret | None,
) -> None:
    from urllib.parse import urlsplit

    from base.db import NoDatabaseAuthorityError
    from base.host.db_authority.tests.generation import seed_active_generation
    from cli.commands.data_plane.completion import adopt_gateway_login

    config = command.config
    url: str = config.view.data_plane.db_url
    if scenario in {"start_delivery_refused", "start_delivery_launcher"}:
        assert expected is not None
        reason = config.view.env_boot.db_authority_refusal
        assert reason is not None
        assert (
            "source checkout" if scenario == "start_delivery_refused" else "agent-profile"
        ) in reason
        try:
            factory().connect()
        except NoDatabaseAuthorityError as exc:
            assert expected.roles.gateway.password not in str(exc)
            return
        raise AssertionError("an unauthorized start reached the database")
    if scenario == "start_delivery_cold":
        assert urlsplit(url).password is None
        secret: GenerationSecret = seed_active_generation(home)
        adopt_gateway_login(
            home,
            url,
            environment=config.process_environment(),
            database_factory=factory,
        )
    else:
        assert expected is not None
        secret = expected
        assert urlsplit(url).password == secret.roles.gateway.password
    assert urlsplit(factory.db_url()).password == secret.roles.gateway.password
