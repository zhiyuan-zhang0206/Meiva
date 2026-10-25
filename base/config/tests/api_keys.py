"""The provider-key mock the meiva, gateway and integration tests share.

One definition, registered once per path-scoped module that imports it (importing
a fixture into a module registers it under that name there), so each of those
modules keeps its autouse batch in one alphabetical order.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pydantic import SecretStr

from base.config import get_field, set_field

_API_KEY_FIELDS = (
    "anthropic_api_key",
    "deepseek_api_key",
    "gemini_api_key",
    "openai_api_key",
    "xiaomi_api_key",
    "moonshot_api_key",
    "zhipu_api_key",
    "dashscope_api_key",
)


@pytest.fixture(autouse=True)
def mock_api_keys() -> Iterator[None]:
    """Set all API keys to dummy values so spawn validation passes."""
    saved = {attr: get_field(attr) for attr in _API_KEY_FIELDS}
    placeholder = SecretStr("test-api-credential")
    for attr in _API_KEY_FIELDS:
        set_field(attr, placeholder)
    try:
        yield
    finally:
        for attr, value in saved.items():
            set_field(attr, value)
