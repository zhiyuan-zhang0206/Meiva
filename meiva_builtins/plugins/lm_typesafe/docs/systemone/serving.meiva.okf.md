---
type: doc
title: SystemOne Serving Limits and Acceptance
description: Service capacity, truncation receipts and deadlines for optional local typed decisions.
tags: [plugins, llm-inference, decision-models]
---

# SystemOne Serving Limits and Acceptance

Configuration and typed wire contract: [[systemone.meiva.okf.md]].

Laya's actual native service is documented in its
[self-hosting guide](https://github.com/NandhaKishorM/laya/blob/v0.4.17/README.md)
and [server source](https://github.com/NandhaKishorM/laya/blob/v0.4.17/laya/serve.py).
Protocol references are stable 0.4.17 and source snapshot
`fe9a6c35023a7d94102e7feaaeaae8857d9d1b07`. Source compatibility and a client
HTTP smoke do not establish model quality, calibration or Meiva deployment
acceptance.
Laya's default Choice cap is 100; its server also limits requests to 64 questions. Its optional tournament mode performs a
different multi-stage algorithm; this adapter does not declare that equivalent
to a native 255-option decision. Deem's narrower capabilities likewise need a
matching registration and separate service acceptance.

## Capacity, truncation and deadlines

A catalog's context limit is a conservative preflight **estimate**, not the
remote tokenizer or the available state budget. The service must reject
unsupported or oversized requests rather than silently shorten the state or
collapse option descriptions. Record effective input and question-head budgets,
special-token overhead, truncation behavior and concurrency limits in the
service manifest. A model card's maximum length is not a serving guarantee.

The adapter rejects reported `usage.truncated`, nonzero
`state_tokens_dropped`, nonempty `truncated_questions`, or `usage.options`
(collapsed options). Laya's `LAYA_JEV_STRICT=1` removes those fields; their
absence cannot prove that the whole input was read. Verify overflow behavior
against the chosen runtime and keep the diagnostic receipt enabled. Laya 0.4.17
can shorten option descriptions without a collapse receipt;
valid response shape is not proof of complete semantic input. Its native
service requires separate acceptance before use as a hard rejection gate.

One request carries all questions about one state. Different candidate states
remain separate calls; there is no implicit `/batch` fan-out, retry, fallback,
chunking or threshold calibration. The caller's timeout bounds the whole local
HTTP attempt as well as HTTPX phases; cancellation propagates. With `timeout=None`
the caller explicitly removes the total deadline. Deadline/transport/status
failures are `DecisionRequestError`, and callers decide whether to retry.
