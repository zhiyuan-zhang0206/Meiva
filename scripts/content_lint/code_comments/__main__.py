import sys

from scripts.content_lint.code_comments.cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
