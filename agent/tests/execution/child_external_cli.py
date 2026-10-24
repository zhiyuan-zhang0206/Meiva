# meiva:note
# The external CLI of the exit-delivery test: `child_external_cli.py <sample file> <lease id>` is a
# fresh interpreter that attaches to the real lease the test created, reads the sample file through
# the SDK once and exits. It replaces nothing: the lease, the agent's checkpoint and the machine are
# the rows and the environment the parent test prepared.
# meiva:link meiva/impersonation/docs/attachment.meiva.okf.md

import sys

import meiva
from meiva import impersonation

with impersonation.attach(sys.argv[2]):
    assert meiva.files.read(sys.argv[1]) == "tail event"
