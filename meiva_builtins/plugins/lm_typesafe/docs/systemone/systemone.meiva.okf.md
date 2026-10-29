---
type: doc
title: SystemOne Decision Services
description: Explicit registration and typed SystemOne transport for an external local decision service.
tags: [plugins, llm-inference, decision-models]
---

# SystemOne Decision Services

`lm_typesafe` owns Jev and optional local SystemOne services under the
[[base/lm/docs/decision-models.meiva.okf.md]] contract. Inference libraries stay
outside Meiva. Defaults preserve Jev and Memory thresholds; registering a local
model alone changes no selection.

## Configuration and catalog identity

Local fields configure only the local service; Jev retains its fixed endpoint, ID,
price and required TypeSafe key. The plugin's `default_config.py` owns the image at
`$MEIVA_HOME/configs/lm_typesafe/config.json`. Its `local_endpoint` and `local_models`
are cluster-pinned, non-secret fields. Catalogs freeze configuration;
rebuild each owner after changes.
Unknown fields, invalid URLs, malformed registrations and unsupported question
types fail before inference. Local IDs never enter the chat catalog. Enrolled
runners require this plugin's gateway projection, even for empty registrations.
Old packets fail closed; update gateway and catalog owners together.

For example, an operator can register the following image after replacing the
revision with the actual immutable checkpoint commit or artifact
manifest SHA256:

```json
{
  "local_endpoint": "http://127.0.0.1:8000/v1/systemone",
  "local_models": {
    "local-decision-laya-multilingual-pinned": {
      "wire_model": "multilingual",
      "response_model": "laya-rl-agent",
      "revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "context_window": 1024,
      "max_request_tokens": 4096,
      "question_types": ["noul", "choice", "score"],
      "max_choice_options": 100,
      "max_score_levels": 10
    }
  }
}
```

The repeated `a` value is an example, not a real revision.
`revision` is an **operator declaration**, not proof that the remote process
loaded those weights. Pin the service runtime, tokenizer, calibration files,
precision and model artifact together in a deployment manifest. Give changed
model or calibration semantics a new Meiva model ID so usage and experimental
results remain attributable. The wire response's model name is only a receipt
check: Laya 0.4.17 returns the constant `laya-rl-agent` even when the request
selects `multilingual`, so `response_model` must be declared independently.
Every call supplies `wire_model`; this requests a checkpoint and does not attest
selection. Laya 0.4.17 can silently route an unknown bare alias, and its constant
response model name cannot detect that mistake. When a response provides `routing`, the adapter rejects any `routing.model`
that differs from `wire_model`; configure the canonical served alias rather
than a synonym or Hub ID the server rewrites. A missing route receipt remains
unverified. Verify the checkpoint independently before selecting a deployment.

`LOCAL_DECISION_API_KEY` is an optional real bearer credential delivered through
`ProviderKeys`, like other provider secrets. When absent the client omits the
Authorization header; it never substitutes another provider's key or a dummy
credential. A service that requires authentication returns 401/403 normally.
`local_endpoint` is an HTTP(S) `/v1/systemone` URL with optional proxy prefix,
without URL credentials, query or fragment.
Loopback refers to the **calling process's** machine; a split runner must use a
service address it can actually reach. Meiva does not launch or provision it.

Each registered ID has exactly one `PriceRates` in the normal catalog. Its
zero rate records no external API invoice; it does not price hardware,
electricity, hosting or operator time and does not imply unlimited compute.
Input token counts still come from the service and produce ordinary usage rows.

## Supported wire protocol

The client sends one `POST` with `model`, shared text `state`, and `questions`
keyed by Meiva question ID. The plugin's shared `systemone.py` codec follows the
[TypeSafe wire API](https://docs.typesafe.ai/api) for both hosted and local calls:

- `YesNo`: `type: noul`, `instructions`, and `criteria` with `true` / `false`.
  The response's numeric `noul` is P(true); P(false) is its complement.
- `Choice`: `type: choice`, `instructions`, and the complete option-description
  map as `criteria`. Return `choice`, complete named `probabilities`, and
  `confidence`.
- `Score`: `type: score`, `instructions`, and ordered `label: description`
  criteria. Return a distribution keyed by canonical zero-based string index,
  `confidence` and probability-weighted zero-based `score`.

Responses include `model`, `answers` keyed by question ID, and
`usage.input_tokens` as a non-negative integer. Missing usage is an error,
never an estimated or zero-filled count. Wrong answer types, unknown questions,
missing outcomes, non-finite numbers, invalid sums or inconsistent top answers
fail through the existing typed validation. A malformed answer after valid
usage still records the reported input. Confidence retains the service's
meaning and is not replaced with maximum probability. Core checks `Score.score`'s
range, not agreement with the distribution's expected value; the service owns
that semantic consistency.

Capabilities are declared per registration. Unsupported types or option/level
counts fail locally before HTTP; any narrower server limit must still return
an explicit error. Ordinary chat completions, reranker scores and missing
logits are not this protocol and are not converted to invented distributions.
The adapter does not call `/v1/chat/completions` or `/v1/decisions`.


Service limits, diagnostic receipts and acceptance:
[[serving.meiva.okf.md]].
