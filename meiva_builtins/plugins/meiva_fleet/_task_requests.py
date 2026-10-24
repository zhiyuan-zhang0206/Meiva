# meiva:note
# The request a task operation records beside its receipt, built from the caller's own arguments.
# A replay compares the stored request with the new one, so a request holds what the caller asked
# for (an omitted owner is the actor, an omitted reminder interval stays omitted) and never a
# value a default resolved to later.


def effective_owner(owner: int | None, actor: int) -> int:
    return owner if owner is not None else actor


def creation_request(
    *,
    title: str,
    description: str,
    parent: int,
    owner: int,
    priority: str,
    remind_interval_seconds: int | None,
) -> dict[str, object]:
    return {
        "title": title,
        "description": description,
        "parent": parent,
        "owner": owner,
        "priority": priority,
        "remind_interval_seconds": remind_interval_seconds,
    }


# meiva:note
# The effective update inputs: a field that was not passed is absent, and so is a `None` other than
# the explicit `parent_id=None` (move to the root). `unchanged` is the sentinel of an argument the
# caller left out, captured before notes acquire a timestamp or parents resolve.
def update_request(values: dict[str, object], *, unchanged: object) -> dict[str, object]:
    return {
        name: value
        for name, value in values.items()
        if value is not unchanged and (value is not None or name == "parent_id")
    }
