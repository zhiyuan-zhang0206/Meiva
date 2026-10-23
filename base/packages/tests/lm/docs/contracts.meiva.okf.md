---
type: doc
title: Language model test contracts
description: Regression assertions for model construction, tuning and provider-error fixtures.
tags: [testing, llm-inference]
---

# Language model test contracts

These records bind regression tests to their current behavior owners. The
construction tests inspect client configuration without calling provider APIs.
They do not establish current vendor behavior.

## Billing evidence

Behavior owner: [[base/lm/docs/provider-errors.meiva.okf.md]].

Billing classification is independent of retry class. A specific billing code can coexist with a generic error type; reporting preserves the original type. A Google prepayment rejection remains distinguishable from ordinary rate limiting with the same HTTP status.

## Completed-call billing spans and fail-fast waiting

Behavior owners: [[base/lm/usage/docs/usage.meiva.okf.md]] and
[[base/lm/docs/provider-errors.meiva.okf.md]].

Billing span scenarios run in fresh interpreters with closed child environments
and private homes. They require the real tracing arm to initialize against an
owned loopback collector, then use the SDK's `SimpleSpanProcessor` and
`InMemorySpanExporter` to observe finished spans. The v1 fields, original start
and actual end, cache-write TTL pricing, unpriced marker, missing usage and
absent-versus-zero cache metering retain their assertions. These checks do not
establish a live provider receipt or remote telemetry persistence.

The disabled case loads its actual flag from the private environment file.
A scoped profile observes the real `get_tracer` entry without replacing it;
a positive acquisition control precedes the assertion that billing acquires
nothing. A real SDK processor records the billing span start and throws the
same observed exception; billing still returns normally and reports that
exception. Each child flushes and shuts down its SDK provider and collector.

Unknown runnable failures retain the original exception object and one attempt,
even with a typed provider cause. A scoped profile observes the real `time.sleep`
C-call, with a `sleep(0)` positive control, before requiring no retry wait with
two configured retries and the unchanged nonzero delay. Both error classes and
both cause states remain parameterized. The profile and its observations belong
only to that child process; no runtime function or SDK lookup is replaced.

## Reasoning visibility

Behavior owner: [[base/lm/docs/lm/lm.meiva.okf.md]].

Adaptive Claude construction supplies summarized thinking display when absent and preserves an explicit display choice. OpenAI construction requests Responses API reasoning summaries. These settings keep reasoning text available to the timeline.

## Scaled budget

Behavior owner: [[base/lm/docs/lm/context-budget.meiva.okf.md]].

Context thresholds scale with the catalog window. DeepSeek Flash retains its model-specific 374k/512k thresholds. A binding hard ceiling scales the reminder by the same ratio so it remains below forced compaction.

## Provider key channel

Behavior owner: [[base/lm/docs/provider-plugins.meiva.okf.md]].

Provider key validation uses the declared environment channel, with the local environment file as fallback. Legacy Settings aliases do not authorize a spawn. Construction can use the fallback after a gateway profile removes a provider key from the process environment without restoring that key to the process.

Provider validation scenarios run in fresh processes with real environment files.
The file-only cases begin with no live keys and write their fallback file after
configuration loads. Construction cases receive live keys through the child
environment. The MIMO/Kimi cases load a real Settings snapshot, withdraw both
keys through the normal environment-authority pass, then verify the stale
snapshot cannot authorize a spawn. Invalid model/effort combinations and
overrides retain their original assertions. The custom-provider case reads a
real file, while a withdrawn cross-provider model resolves through actual
catalog rows before its build context is inspected.

## Explicit tuning

Behavior owner: [[base/lm/docs/model-configuration.meiva.okf.md]].

An explicit empty effort masks a model default. A profile without the owning settings domain resolves from the supplied model and shared layers without recovering a hidden settings reader.

## Response receipt fixtures

`providers/test_openai_fast.py` uses `Response.model_construct` to reproduce
the locked OpenAI SDK's non-strict network-response construction. Strict model
validation could reject a newer service-tier receipt before Meiva consumes it.
This fixture exercises receipt handling; it does not claim that strict parsing
accepts the same values or that a live provider returned them.
