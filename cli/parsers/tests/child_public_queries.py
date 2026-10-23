# meiva:link cli/docs/cli.meiva.okf.md
import sys

from cli.parsers import command_options, parse_args

if __name__ == "__main__":
    assert command_options()
    assert callable(parse_args(["status"]).func)
    if sys.argv[1:]:
        assert sys.argv[1:] == ["memory", "search", "text", "--limit", "8"]
        args = parse_args()
        assert args.query == "text" and args.limit == 8
    assert "base.config" not in sys.modules
    assert "cli.commands.lifecycle.status" not in sys.modules
