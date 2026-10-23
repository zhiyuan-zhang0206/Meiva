# meiva:note
# A real MCP server over stdio for the daemon tests, launched as a child process by the daemon under test. What it does is steered by its environment, set from the `env` of its `.mcp.json` entry: `FIXTURE_SPAWN_LOG` is a file that gets one line (its pid) per start; `FIXTURE_DIE_STARTS=N` makes the first N starts, counted in that log, exit at once, before the MCP handshake; `FIXTURE_EFFECT_LOG` is a file that `bump` appends to. The tools: `echo`, `pid`, `fail` (a tool error), `noop`, `bump` (an effect with a visible count), `bump_then_die` (an effect, then the process exits during the call, so the reply is lost) and `exit_soon` (the process exits shortly after replying).
import os
import threading
from pathlib import Path

from mcp.server.mcpserver import MCPServer


def _count_start() -> int:
    log = os.environ.get("FIXTURE_SPAWN_LOG")
    if log is None:
        return 0
    with Path(log).open("a", encoding="utf-8") as handle:
        handle.write(f"{os.getpid()}\n")
    return len(Path(log).read_text(encoding="utf-8").splitlines())


if _count_start() <= int(os.environ.get("FIXTURE_DIE_STARTS", "-1")):
    os._exit(3)

server = MCPServer("fixture")


@server.tool()
def echo(text: str) -> str:
    return text


@server.tool()
def pid() -> int:
    return os.getpid()


@server.tool()
def fail() -> str:
    raise ValueError("denied")


@server.tool()
def noop() -> None:
    return None


@server.tool()
def bump() -> int:
    log = Path(os.environ["FIXTURE_EFFECT_LOG"])
    with log.open("a", encoding="utf-8") as handle:
        handle.write("effect\n")
    return len(log.read_text(encoding="utf-8").splitlines())


@server.tool()
def bump_then_die() -> str:
    bump()
    os._exit(7)


@server.tool()
def exit_soon() -> str:
    threading.Timer(0.1, lambda: os._exit(0)).start()
    return "scheduled"


if __name__ == "__main__":
    server.run("stdio")
