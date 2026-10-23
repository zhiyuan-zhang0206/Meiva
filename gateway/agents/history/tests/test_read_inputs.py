# meiva:note
# Where the understanding switch and chunk ratio come from in a process.

from gateway.agents.history.read_inputs import understanding_chunk_ratio, understanding_enabled


def test_a_process_with_the_agent_domain_uses_its_own_values() -> None:
    aliases = {"MEIVA_UNDERSTANDING_ENABLED": "false", "MEIVA_UNDERSTANDING_CHUNK_RATIO": "0.9"}
    assert (
        understanding_enabled(domain_value=True, env_aliases=aliases, field_default=False) is True
    )
    assert (
        understanding_chunk_ratio(domain_value=0.3, env_aliases=aliases, field_default=0.5) == 0.3
    )


def test_a_gateway_profile_reads_the_unit_env_file() -> None:
    aliases = {"MEIVA_UNDERSTANDING_ENABLED": " True ", "MEIVA_UNDERSTANDING_CHUNK_RATIO": "0.25"}
    assert (
        understanding_enabled(domain_value=None, env_aliases=aliases, field_default=False) is True
    )
    assert (
        understanding_chunk_ratio(domain_value=None, env_aliases=aliases, field_default=0.5) == 0.25
    )


def test_a_gateway_profile_without_a_file_value_keeps_the_field_default() -> None:
    assert understanding_enabled(domain_value=None, env_aliases={}, field_default=False) is False
    assert understanding_enabled(domain_value=None, env_aliases={}, field_default=True) is True
    assert understanding_chunk_ratio(domain_value=None, env_aliases={}, field_default=0.5) == 0.5


def test_the_switch_accepts_only_the_truthy_spellings() -> None:
    for raw, expected in [("1", True), ("yes", True), ("on", True), ("0", False), ("nope", False)]:
        aliases = {"MEIVA_UNDERSTANDING_ENABLED": raw}
        assert (
            understanding_enabled(domain_value=None, env_aliases=aliases, field_default=True)
            is expected
        )
