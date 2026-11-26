# EmbeddingGemma 2 HTTP Provider

`MEIVA_EMBEDDING_BACKEND=embeddinggemma` selects the text-only, 768-dimensional
provider. `gemini` remains the default. The gateway and indexer use HTTP only;
neither imports a model runtime or downloads weights. The inference server is either an
external, operator-run service (the explicit `MEIVA_EMBEDDINGGEMMA_ENDPOINT` form) or the
[managed service](#managed-hosting) that `meiva-root` runs on one machine of the cluster.

## Configuration and artifact identity

The gateway and all memory services must start with the same cluster-pinned values. With
[managed hosting](#managed-hosting) the endpoint and model locator may be left unset and are
derived; the explicit form below always wins:

- `MEIVA_EMBEDDINGGEMMA_ENDPOINT`: the full HTTP(S) URL ending in `/v1/embeddings`.
  URLs carrying credentials, a query, or a fragment are rejected.
- `MEIVA_EMBEDDINGGEMMA_MODEL`: the exact model locator the server accepts and
  echoes in its response, for example `/models/pinned-text.litertlm,gpu`.
- `MEIVA_EMBEDDINGGEMMA_REVISION`: a lowercase 64-hex artifact SHA256 or a
  lowercase 40-hex immutable checkpoint commit. A LiteRT/quantized artifact needs
  its own identity; it must not borrow the identity of different reference weights.
- `MEIVA_EMBEDDINGGEMMA_API_KEY`: optional bearer token, kept separate from Gemini's
  credential and excluded from config representations.

The revision is an **operator declaration** that must match the actual served
artifact. The standard embedding response echoes a model locator; it does not
attest the remote weights' hash. The client verifies that echoed locator, response
shape, indices and numerical validity, but does not claim remote artifact
attestation. Pin the service's weights and verify their identity independently.

The semantic fingerprint hashes the revision, retrieval prefixes, 768-dimensional
output and normalization protocol. Endpoint and server-side locator changes do not
change the space when the pinned artifact and protocol stay the same. A changed
artifact, including quantization, needs a new revision and derived index.
Metadata readers (store startup, health and pgvector provisioning) receive this
revision without constructing a provider that can make network or billing calls.

## Managed hosting

`meiva-root` can run the inference server itself, so it appears in `meiva status`, has a
readiness probe and is restarted like any other unit. It is **off by default**: with
`MEIVA_EMBEDDINGGEMMA_HOST` empty no machine runs it and nothing else in this document changes.

### Placement: one setting names the host

`MEIVA_EMBEDDINGGEMMA_HOST` (cluster-pinned) is the reachable address of the hosting machine,
written exactly as that machine's `MEIVA_MACHINE_HOST`. This is the same fact the cluster
already uses to say where a machine can be dialled, so placement needs no registry lookup in
configuration code:

- **Which machine runs the service.** The roster's `embeddinggemma` unit is gated to the one
  machine whose own `MEIVA_MACHINE_HOST` equals the placement host (gateway, runner or both).
  Every other machine reports `EmbeddingGemma is hosted on <host>, not on this machine`. A
  hosting machine whose revision is not a 64-hex SHA256 is refused with that reason instead of
  crash-looping.
- **Where clients dial.** When `MEIVA_EMBEDDINGGEMMA_ENDPOINT` is unset, the gateway and the
  memory indexer derive `http://<host>:<MEIVA_EMBEDDINGGEMMA_PORT>/v1/embeddings`; on the hosting
  machine itself the host becomes `127.0.0.1`. An explicit endpoint always wins (an external
  server keeps working, and the managed unit can run beside it).
- **Model locator.** When `MEIVA_EMBEDDINGGEMMA_MODEL` is unset, server and clients use the
  device-independent locator `embeddinggemma-2`. A deployment that already sets a locator,
  including the legacy `<path>,cpu` / `<path>,gpu` form, keeps it: the managed server accepts
  exactly the locator it is told and treats it as an opaque label.

### Settings

| Setting | Scope | Default | Meaning |
|---|---|---|---|
| `MEIVA_EMBEDDINGGEMMA_HOST` | cluster | empty (no managed service) | Placement host, as above |
| `MEIVA_EMBEDDINGGEMMA_PORT` | cluster | `9379` | Listen port and derived-endpoint port |
| `MEIVA_EMBEDDINGGEMMA_DEVICE` | host | `cpu` | `cpu` or `gpu`; anything else fails validation at startup |
| `MEIVA_EMBEDDINGGEMMA_MODEL_PATH` | host | `$MEIVA_HOME/embeddinggemma/model.litertlm` | LiteRT-LM model file |
| `MEIVA_EMBEDDINGGEMMA_PYTHON` | host | empty (the checkout's `.venv/bin/python`) | Interpreter that runs the server |
| `MEIVA_EMBEDDINGGEMMA_LITERT_PATH` | host | empty | Directory holding `litert_lm`, appended to the server's `sys.path` |
| `MEIVA_EMBEDDINGGEMMA_CACHE_DIR` | host | `$MEIVA_HOME/embeddinggemma/cache` | LiteRT compiled-program and weight caches |
| `MEIVA_EMBEDDINGGEMMA_BIND_HOST` | host | `0.0.0.0` | Listen address; use `127.0.0.1` on a single machine |
| `MEIVA_EMBEDDINGGEMMA_MAX_INPUT_LENGTH` | host | `8192` | Maximum tokens per input; longer input is rejected |
| `MEIVA_EMBEDDINGGEMMA_REVISION` | cluster | none | The pinned artifact; for the managed server it must be the 64-hex SHA256 of the model file |

Cluster-scope values travel to every machine in the bootstrap packet; host-scope values are
read from the hosting machine's own configuration, so they differ per machine and are inert on
the others.

### Switching `cpu` and `gpu`

Set `MEIVA_EMBEDDINGGEMMA_DEVICE` on the hosting machine and restart `meiva-root` there. Only
the `--device` argument of the unit changes. The served artifact, the model locator, the
endpoint and the embedding-space fingerprint (which hashes the revision and the retrieval
protocol, never the device, host or locator) are unchanged, so the gateway, the indexer and
memory-search need no change and no index is rebuilt; a different fingerprint would be an HTTP
409 and a forced reindex. The tests pin this: the fingerprint is identical for legacy `,cpu`,
legacy `,gpu`, managed `cpu`, managed `gpu` and a moved host. The probe reads the device from
`/healthz`, so a process left from the other device reads as down.

The two devices run the same artifact but not bit-identical arithmetic: on the reference machine
(RTX 4060 Laptop, WebGPU) GPU vectors differ from CPU vectors by a cosine of about 0.998.
Both stay in one space by the revision contract; a deployment that wants strictly uniform
vectors should re-run the indexer's patrol after a device change.

### The LiteRT-LM runtime stays out of the repository environment

`litert-lm` is not a dependency of the repository and is not installed by `uv sync`. The server
imports it lazily, when it starts, from the interpreter that runs it:

- install `litert-lm`, `fastapi` and `uvicorn` into a dedicated virtualenv and point
  `MEIVA_EMBEDDINGGEMMA_PYTHON` at its interpreter; or
- keep the checkout's interpreter and set `MEIVA_EMBEDDINGGEMMA_LITERT_PATH` to a directory
  holding the `litert_lm` package (for example a `pip install --target` directory). The path is
  appended after the interpreter's own packages.

A missing runtime fails the start with a message naming both settings. Tests cover backend
selection, the overflow policy and error mapping with an injected runtime, so CI never needs
the package.

### What the server does

- `POST /v1/embeddings` follows the raw-input contract above: no task prefix, `float`
  encoding, `dimensions == 768`, `input_overflow_strategy=ERROR` (an over-long input is HTTP
  400, never truncated), one worker thread and one request at a time. A request naming another
  model locator is HTTP 400.
- `GET /healthz` reports `status`, `model`, `revision`, `device`, `max_input_length`, the
  model's `signature_selection` and `open_ms`. It names no path.
- At startup the server hashes the model file and refuses to serve unless it equals the
  revision, then opens the engine on the chosen device and computes one warm-up embedding
  before it listens. `SIGTERM` runs a graceful shutdown that closes the engine.
- It has no authentication of its own except an optional bearer token: when
  `MEIVA_EMBEDDINGGEMMA_API_KEY` is configured the roster launches the server with
  `--require-api-key`, and the server refuses to start unless that variable is also present in its own
  process environment. Prefer `MEIVA_EMBEDDINGGEMMA_BIND_HOST` on a private address, or
  `127.0.0.1` when every client is on the hosting machine.

### Migrating from a hand-run service

1. Put the model file at `MEIVA_EMBEDDINGGEMMA_MODEL_PATH` (or point the setting at it) and keep
   `MEIVA_EMBEDDINGGEMMA_REVISION` at its SHA256. Install or locate `litert_lm` as above.
2. On the hosting machine set `MEIVA_EMBEDDINGGEMMA_HOST` to its `MEIVA_MACHINE_HOST`, and
   `MEIVA_EMBEDDINGGEMMA_DEVICE`, `MEIVA_EMBEDDINGGEMMA_PYTHON` / `_LITERT_PATH` as needed.
   Leave the existing `MEIVA_EMBEDDINGGEMMA_ENDPOINT` and `MEIVA_EMBEDDINGGEMMA_MODEL` as they
   are, or unset them to adopt the derived values; either way the fingerprint is unchanged.
3. Stop the old unit, then restart `meiva-root` on the hosting machine. (Both bind the same
   port, so the old unit must stop first; the managed unit's probe also rejects an old server
   that reports a `backend` instead of a `device`.)
4. `meiva status` shows `embeddinggemma`; memory search keeps its index.

## Raw-input HTTP contract

The supported wire behavior follows
[LiteRT-LM v0.18.0 `openai_embeddings.py`](https://github.com/google-ai-edge/LiteRT-LM/blob/v0.18.0/python/litert_lm_cli/commands/serve/openai_embeddings.py).
That handler sends `input` directly to `compute_embedding_batch`; it does not
add task prefixes. A compatible server must preserve this raw-input behavior.
A server that adds its own prefixes is incompatible with this adapter.

The adapter owns the checkpoint model card's two text templates:

- Query: `task: search result | query: {text}`.
- Document: `title: none | text: {text}`.

These templates follow the
[EmbeddingGemma 2 model card](https://ai.google.dev/gemma/docs/embeddinggemma/model_card_2),
which differs from the LiteRT guide's prefix table. Do not prepend a second
template in the server. The request asks for `encoding_format=float`,
`dimensions=768`, `normalize=true` and `insert_special_tokens=true`.
The response must identify the configured model and contain every requested
integer index exactly once. Results are restored to input order. Wrong widths,
non-finite numbers and zero vectors fail the call; valid vectors are L2-normalized
and returned as float32.

Each operation makes one HTTP attempt under the configured
`MEIVA_EMBED_TIMEOUT_SECONDS` cancellation deadline, including response-body reads.
The indexer's patrol retries failed paths; the search caller owns query retries.
This avoids replaying expensive local work inside the request. The provider's
batch budget is exactly one attempt timeout. The indexer's liveness ceiling covers
the larger of that budget and the shared batch-commit budget, plus scheduling
slack; a fast local embedder must not make a legitimate 300-second NumPy commit
look wedged. Synchronous calls require a worker thread without a running event loop;
async queries use native asynchronous HTTP. No remote API billing is emitted for
this local provider.

## Effective input capacity

The model card's 8,192-token window is not proof that a particular server
instance accepts that many tokens. The adapter does not load a tokenizer or
attest the remote runtime's effective `max_input_length`, tokenizer, or truncation
policy. The existing query builder can send up to 2,000 characters, whose token
count depends on the actual text and includes the query prefix and special tokens.
Before deployment acceptance, record the served artifact, tokenizer, effective
input limit, prefix/special-token overhead and truncation policy in the deployment
manifest, and probe the boundary with real inputs. A compatible deployment must
reject over-limit input rather than silently truncate it. The HTTP adapter alone
cannot establish that capacity guarantee; quality/resource experiments must not
claim an 8K serving limit merely from the model card.

## Switching an index safely

The internal NumPy index API uses `/v1` paths, and every operation carries `x-meiva-embedding-space`. The
service rejects a missing or mismatched fingerprint with HTTP 409 before reading
or mutating the index. This includes metadata, deletes and batch writes. Health
probes carry the configured fingerprint too. Legacy unversioned paths do not exist on the new service. New clients get 404
from old services before connecting or writing; old clients get 404 from the new
service. A versioned request without this header fails with 409. Update/restart the gateway, indexer and
memory-search together; a partially updated deployment can be unavailable rather
than comparing or labelling vectors from different spaces.

At startup, a NumPy cache containing another fingerprint is ignored, even if its
width matches. The normal patrol rebuilds the derived cache. pgvector searches
filter by the configured `embedder`, so old-space rows cannot become candidates
while reconciliation is still in progress. A dimension change retains the
existing pgvector provisioning requirement.

This provider adds no personal-memory indexing, changes no chunking policy, and
retains the existing decision-model rerank step. Local embeddings alone do not
make the whole memory search offline. Resource use, cold load, latency and useful
batch size require measurements of the separately deployed model service.
