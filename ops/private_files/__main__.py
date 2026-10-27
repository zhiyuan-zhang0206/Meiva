from base.paths.home import resolve_meiva_home
from ops.private_files import main

raise SystemExit(main(home=resolve_meiva_home()))
