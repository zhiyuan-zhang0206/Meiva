import pytest
from pydantic import ValidationError

from base.config.domains.transfer import TransferSettings


def test_ceilings_default_to_the_values_the_feature_landed_with() -> None:
    config = TransferSettings.model_validate({})
    assert config.max_workspace_files == 20_000
    assert config.max_workspace_bytes == 256 * 1024 * 1024


@pytest.mark.parametrize("field", ["max_workspace_files", "max_workspace_bytes"])
def test_every_ceiling_must_be_positive(field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        TransferSettings.model_validate({field: 0})


def test_environment_alias_overrides_the_default() -> None:
    config = TransferSettings.from_environment({"MEIVA_TRANSFER_MAX_WORKSPACE_FILES": "7"})
    assert config.max_workspace_files == 7
