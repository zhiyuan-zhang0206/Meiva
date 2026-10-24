from base.cluster.machine import MachineFacts, MachineRoles


def machine_identity(
    *, name: str = "test-runner", roles: MachineRoles | None = frozenset({"agent-runner"})
) -> MachineFacts:
    return MachineFacts.of(name=name, role=() if roles is None else roles)


def allow_identity(*_args: object, **_kwargs: object) -> bool:
    return True


def deny_identity(*_args: object, **_kwargs: object) -> bool:
    return False


def ignore_arguments(*_args: object, **_kwargs: object) -> None:
    pass
