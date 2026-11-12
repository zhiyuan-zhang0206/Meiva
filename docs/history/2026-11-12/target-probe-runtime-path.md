# Bind target generation probes to the future runtime path

The kept-root preflight imports target code from a disposable checkout while the
future start executes from the host's source checkout. Its manifest already used
the future working directory, but its environment builder defaulted to the
imported checkout's virtualenv. This made PATH and VIRTUAL_ENV differ solely
because of the reader location, so an otherwise identical launch was refused.

The probe now explicitly supplies the future source checkout's virtualenv to the
existing environment builder. Normal start environment construction and generation
comparison stay unchanged. We chose the existing explicit input over rewriting
path strings after construction or excluding PATH/VIRTUAL_ENV from the digest:
those alternatives would conceal real runtime changes.

The original production refusal establishes a probe-context difference, not
runtime compatibility of any particular target release. Repository tests and
isolated controls do not authorize a rollout or establish production health.
