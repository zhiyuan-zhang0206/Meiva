from base.config import settings


def test_the_decay_defaults_to_three() -> None:
    assert type(settings.agent).model_fields["understanding_group_check_decay"].default == 3


def test_the_check_cadence_defaults_to_sixty_open_nodes() -> None:
    field = type(settings.agent).model_fields["understanding_group_check_open"]
    assert field.default == 60
