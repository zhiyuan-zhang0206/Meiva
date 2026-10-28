---
type: doc
title: Browser API requests
description: How the browser's gateway client is split into pure plans, thin shells and explicit ports, and how each layer is tested.
tags: [frontend]
---

# Browser API requests

## Layout (`ui/web/src/lib/transport`)

Decisions are pure modules over values (declared `strict` in the directory's
`meizel.toml`); the shells that run them take their effects as required ports.
`gateway/` is the HTTP client, `stream/` the shared event stream, and the files at
the top are what the rest of the app imports.

| Decision | Pure module | Shell that executes it |
|---|---|---|
| Request paths and query strings | `gateway/routes/` (`agent-`, `timeline-`, `control-`, `config-routes.ts`) | `gateway/api-factory.ts`, `gateway/api-control.ts` |
| Request init, operation key and its headers | `gateway/plans/request-plan.ts`, `key-plan.ts` | `gateway/http.ts` |
| Error text, receipts, ambiguity of a failure | `gateway/plans/response-plan.ts` | `gateway/http.ts` |
| Slow-request report | `gateway/plans/timing-plan.ts` | `gateway/http.ts` |
| Message delivery and reconciliation | `gateway/plans/delivery-plan.ts` (a state machine: state and event in, next action out) | `gateway/message-delivery.ts` |
| Upload batch and receipt validation | `gateway/plans/upload-plan.ts` | `gateway/upload-delivery.ts` |
| System SSE frames, parse-failure count | `sse-frames.ts` | `useEventStream.tsx` |
| Reconnect delay, ready state, tab-share URLs, unions, merged frames | `stream/sse-plan.ts`, `stream/share-plan.ts` | `sse-lifecycle.ts`, `stream/sse-share.ts`, `stream/stream-hub.ts` |

## Ports and the composition root

`transport.ts` defines the only ports: `HttpTransport` (fetch, the monotonic
clock read around each request, the upload channel, key entropy),
`StreamTransport` (EventSource, BroadcastChannel, Web Locks, page visibility, the
wall clock, a diagnostics sink) and `SessionPort`. `system-transport.ts` is the
only module that builds them from real globals. `createHttp(transport, base,
telemetry)` gives the `Http` client every endpoint is written against;
`createApi(http)` gives the `Api` object. The HTTP owner also owns response-body
consumption: `accepted` preserves raw HTTP refusal text for no-body endpoints,
`rawJson` applies that refusal contract before JSON decoding, and `decoded`
passes status plus decoded unknown payload to a pure receipt validator. Endpoints
keep request-planning errors inside their existing Promise contract. Uploads
subscribe to the upload event owner's progress interface instead of replacing its
handler property. `api.ts` composes the app's one client;
`components/shell/providers.tsx` hands it to `ApiProvider` and a `StreamHub` (which
owns the shared-stream objects of the page) to `StreamHubProvider`. The root takes
that client and hub as inputs, so a test composes the same tree with its own.

Every reader takes what the root hands down: components and hooks call `useApi()`,
the global and per-agent streams arrive through `useEventStream.tsx`'s contexts, and
a module that cannot call a hook takes the client as a required parameter
(`inspector-queries.ts:fetchWindowedInspect`,
`use-harness-availability.ts:probeQuery`). All of them throw outside their provider;
only the composition root itself imports `api.ts`.

## Test boundary

Plans are asserted as values: a path, an init, a verdict, the next delivery
action. Shells run against a real HTTP server on port 0
(`gateway/tests/gateway-server.ts`, which can also answer with no reply, headers
without a body, or a dropped connection) through the platform's own `fetch`,
which the test setup file captures before it installs its guards
(`gateway/tests/real-network.ts`). This boundary also owns Node's `createServer`;
callers pass `realNetwork.createServer` explicitly to `startGateway`, keeping
resource acquisition visible without making the server fixture a global I/O owner.
Only what a network cannot produce is a double
in `tests/`: upload progress events (`gateway/tests/fake-upload.ts`) and the EventSource state
machine, tab channels and Web Locks (`stream/tests/fake-stream.ts`). Hook tests use
`gatewayRig` and `apiWrapper` through the public test door
`tests/api/api-provider.tsx` with that same real
server and `createApi` factory. Held HTTP responses expose pending and cancellation
behavior; response data and HTTP failures travel through the production decoder.
Only deliberately completing a fetch after abort needs the narrow
`tests/api/uncancellable-fetch.ts` fault. Presentation tests with complete,
fresh query-cache state use `cacheOnlyApi`: the real factory over the test setup's
network guard, so an unexpected cold read fails instead of receiving invented data.

Presentation tests otherwise render the page under real providers with the suite's
fakes in `test-support/`: `transport` is the real endpoint surface with every call a
`vi.fn()` (an unanswered call rejects with its own name), `renderWithApi` renders
under `ApiProvider`, and `TestStreamProvider` publishes the stream contexts with a
recording `subscribe` over a `StreamHub` built from the transport's own `FakeTab`. A
test answers the calls it cares about and delivers a frame by hand; no test replaces
the transport module.

A test wraps what it renders in `ApiTestProvider` (or imports `renderWithApi`). A bare
`render()` of a component that reads the transport throws `useApi must be used inside
<ApiProvider>`: that is the design, not a regression -- the message names the missing
provider, and the fix is to render under one with a fake.

## Retryability and the panel's words

`ApiError` keeps the envelope's `code` and `retryable`. A reader chooses what to
say from those, not from the status: a refusal the server named is permanent for
that request, so only a retryable answer may ask for a retry, and one that fails
without an answer at all (a dropped connection) keeps that advice. The shared
context panel is the first reader that does — it names
`to_user_document_too_large`, `to_user_reference_gone` and
`to_user_document_unreadable` differently and offers a file too large to render
as the file itself through the resource endpoint
([classes](../../../../../gateway/agents/docs/to-user-read-failures.meiva.okf.md)).

Endpoint-shape cases assert the gateway path, method and payload rather than a
particular test host. These cases do not establish runtime response-schema
validation: generated OpenAPI types describe success payloads, while the
transport preserves HTTP failures and decodes JSON.
