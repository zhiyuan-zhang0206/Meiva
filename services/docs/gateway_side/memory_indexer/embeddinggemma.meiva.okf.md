---
type: doc
title: EmbeddingGemma 2 HTTP Provider
description: Pinned text-only EmbeddingGemma retrieval through a separate raw-input embedding server, including model-space identity and fail-closed index switching.
---

# EmbeddingGemma 2 HTTP Provider

`MEIVA_EMBEDDING_BACKEND=embeddinggemma` selects the text-only, 768-dimensional
provider. `gemini` remains the default. The gateway and indexer use HTTP only;
neither imports a model runtime, downloads weights, nor starts an inference server.
The server can run on the gateway host or another reachable machine.

## Configuration and artifact identity

The gateway and all memory services must start with the same cluster-pinned values:

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
