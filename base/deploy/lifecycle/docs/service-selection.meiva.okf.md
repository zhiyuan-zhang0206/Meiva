---
type: doc
title: Explicit service selection
description: Every meiva start names its service selection; no selection is persisted or reused.
tags: [deployment, services]
---

# Explicit service selection

`service_selection.py` resolves the selection one `meiva start` names on its
command line — exactly one of `--all-services`, `--only-service NAME…` or
`--disable-service NAME…` — into the set of services that start skips. Nothing
is written to the home: a later start never reuses an earlier start's choice,
and a bare `meiva start` is a usage error (lifecycle CLI choices are always
explicit; see [CLI argument discipline](../../../../docs/conventions/engineering/cli-argument-discipline.md)).

Every verb that starts or converges services takes the same explicit selection:
`meiva start`, `meiva restart` over the service tree, standalone `meiva converge` and
`meiva lgtm on|off`. What a start actually launched is a fact of the running tree,
read from the root's unit manifest (`launched_services` in
`services/supervision/meiva_root_glue/manifests.py`) only by the observers: health
probes and `meiva status`. The OS boot job carries the selected services on its own
command line (`--only-service` per service); an empty selection is refused rather
than removing the boot job.

`base/deploy/lifecycle/tests/test_service_selection.py` proves each explicit form,
the refusal of zero or several forms, and the refusal of unknown names. The
operator entry is described in [[cli/docs/start_identity.meiva.okf.md]].
