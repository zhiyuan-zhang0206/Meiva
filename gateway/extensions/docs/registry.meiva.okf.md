---
type: doc
title: Extension registry write route
description: PUT /api/extensions/{kind}/{name} is the only CLI path into the cluster extension registry.
tags:
- gateway
- extensions
---

# Extension registry write route

`gateway/extensions/registry.py` owns `PUT /api/extensions/{kind}/{name}`. It is
the write behind `meiva skill install` and the bare-skill branch of
`meiva plugins install`: the CLI holds no database handle for it, so
authentication and validation happen here like every other cluster operation.

Body: `{source, source_ref, archive_b64}`, where `archive_b64` is the base64 of
the deterministic tar `base/packages/extensions/registry.py:pack_tree` builds.
The route unpacks it into a scratch directory (`unpack_tree` refuses entries
that escape it), then calls `register_tree`, so the content hash and the stored
blob derive from the bytes received, never from the client. `kind` is the closed
`ExtensionKind` enum and `name` a path-safe token; both are 422 otherwise, as is
an undecodable archive.

The size cap is the registry blob cap (8 MiB). The base64 length is checked
before decoding (413), and `register_tree` enforces the cap again on the packed
archive (413). PUT needs no operation key: the blob is content-addressed and the
row an upsert on its name, so a replay stores the same content (route contract
`IDEMPOTENT`).

The client half is `cli/commands/extensions/packages/cluster_registry.py`. An install
registers every package through the gateway before it copies anything onto the
machine; a refused or unreachable gateway fails the install. Start-time adoption
and materialization (`cli/commands/extensions/materialize.py`) stay local reads
and writes against the database, because they run before the gateway is up.
