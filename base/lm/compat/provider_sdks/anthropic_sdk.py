# meiva:note
# The complete `anthropic` import graph, entered under this module's own import lock.
#
# `agent_host` builds a runtime per turn, and the same process runs other work on executor threads
# (`asyncio.to_thread`, the ops pool), so a provider SDK can be reached for the first time from more
# than one thread. CPython holds one import lock per module name: a second *root* import of a package
# waits for the running one, but a *submodule* import does not — its parent is already in
# `sys.modules`, so it runs against a package whose `__init__` is still executing, and the
# `from ... import name` behind it reads a half-built module. That surfaced as four crashed turns in
# 24h on company-mini (#6538): `ImportError: cannot import name 'DEFAULT_MAX_RETRIES' from partially
# initialized module 'anthropic._base_client'`, and the same shape for `Messages` from
# `anthropic.resources.beta`.
#
# The one root import here loads that whole graph, so a second thread asking for this module waits
# on its lock and then reads a complete package. Every other site reaches the SDK through this
# module; a submodule import of its own (`from anthropic.types import ...`) reopens the race.
# meiva:link base/lm/compat/docs/compat.meiva.okf.md

import anthropic

__all__ = ["anthropic"]
