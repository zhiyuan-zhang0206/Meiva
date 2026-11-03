import sys

from base.paths.home import resolve_meiva_home
from ops.private_files import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], home=resolve_meiva_home()))
