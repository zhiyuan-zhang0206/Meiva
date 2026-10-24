from pathlib import Path

__all__ = ["daily_profile_path"]


# meiva:note
# Converge supplies the OS system and user home captured by its command runtime.
# This selects a candidate only; provisioning checks the directory after its skip gates.
def daily_profile_path(*, system: str, user_home: Path) -> Path:
    match system:
        case "darwin":
            return user_home / "Library" / "Application Support" / "Google" / "Chrome"
        case "linux":
            return user_home / ".config" / "google-chrome"
        case _:
            raise ValueError(f"unsupported browser profile system {system!r}: linux or darwin")
