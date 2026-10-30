import pytest
from pydantic import ValidationError

from base.config.domains.gateway import GatewaySettings


# meiva:note
# The delivered-directory name is one path segment inside every agent's workspace. A separator
# would move the agent's own directory out of the workspace the delivery proof pins (and, with the
# name known only to the receiver, stall delivery), and a dot-only name would put uploads beside it.
@pytest.mark.parametrize("name", ["inbox", "uploads-2", "a.b_c-d"])
def test_a_single_segment_directory_name_is_accepted(name: str) -> None:
    assert GatewaySettings.model_validate({"upload_dir_name": name}).upload_dir_name == name


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "../x", "/abs", "with space", "a\\b"])
def test_a_name_that_is_not_one_segment_is_refused(name: str) -> None:
    with pytest.raises(ValidationError, match="upload_dir_name"):
        GatewaySettings.model_validate({"upload_dir_name": name})
