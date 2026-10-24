# meiva:note
# A real interpreter that cannot import `mcp.types`: the directory named in argv[1] holds an `mcp` package without that module and goes first on the module path. It classifies one error and reports whether the missing dependency surfaced as an ImportError (exit 0) or was swallowed into an answer (exit 1).
import sys


def main(shadow: str) -> int:
    sys.path.insert(0, shadow)
    from meiva.mcps.config import is_transport_error

    try:
        answer = is_transport_error(RuntimeError("classify me"))
    except ImportError as exc:
        print(f"ImportError: {exc}")
        return 0
    print(f"classified as {answer} without the required dependency")
    return 1


raise SystemExit(main(sys.argv[1]))
