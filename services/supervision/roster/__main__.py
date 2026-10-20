# meiva:note
# `python -m services.supervision.roster` prints this checkout's service names on one line, for a
# caller that imports no application code: `cli/fleet_update.py` checks the operator's service
# names with it before touching any host.
import sys

from services.supervision.roster import build_services

sys.stdout.write(" ".join(spec.session for spec in build_services()) + "\n")
