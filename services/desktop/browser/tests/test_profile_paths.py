from pathlib import Path

import pytest

from services.desktop.browser.profile_paths import daily_profile_path


@pytest.mark.parametrize(
    ("system", "suffix"),
    [("darwin", "Library/Application Support/Google/Chrome"), ("linux", ".config/google-chrome")],
)
def test_daily_profile_path_uses_the_captured_user_home(system: str, suffix: str) -> None:
    user_home = Path("/captured user/home")
    assert daily_profile_path(system=system, user_home=user_home) == user_home / suffix


@pytest.mark.parametrize("system", ["win32", "", "Darwin"])
def test_daily_profile_path_rejects_an_unknown_system(system: str) -> None:
    with pytest.raises(ValueError, match="unsupported browser profile system"):
        daily_profile_path(system=system, user_home=Path("/captured/home"))
