import sys
from pathlib import Path


def main() -> None:
    match sys.argv[1]:
        case "chrome":
            sys.stdin.buffer.read()
        case "seed":
            from services.desktop.browser.profile import Console, ensure_browser_profile

            ensure_browser_profile(
                interactive=True,
                daily_profile=Path(sys.argv[2]),
                profile=Path(sys.argv[3]),
                console=Console.of_process(),
            )
        case _:
            raise ValueError(f"unknown profile child mode: {sys.argv[1]}")


if __name__ == "__main__":
    main()
