from pathlib import Path

from base.host.env.harness_binaries import harness_unit_path
from base.host.system.hostport import Host

__all__ = ["read_launchd_path", "read_meiva_binary"]


# meiva:note
# The admitted registrar resolves only this checkout's executable; PATH is the fallback
# for an installation without its venv. No scheduler definition reads the world itself.
def read_meiva_binary(repo: Path, *, host: Host) -> str:
    candidate = repo / ".venv" / "bin" / "meiva"
    return str(candidate) if candidate.exists() else host.which("meiva") or str(candidate)


# meiva:note
# This is also the PATH policy used by legacy health/autostart capture roots. Keep harness
# directories, ordering and deduplication in their existing owner.
def read_launchd_path(meiva: str, *, user_home: Path, host: Host) -> str:
    brew = host.which("brew")
    brew_dirs = [str(Path(brew).parent)] if brew else ["/opt/homebrew/bin", "/usr/local/bin"]
    return harness_unit_path(
        leading=[str(Path(meiva).parent), *brew_dirs],
        home=user_home,
        what="the launchd job PATH",
    )
