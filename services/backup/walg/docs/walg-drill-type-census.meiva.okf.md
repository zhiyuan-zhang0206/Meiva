---
type: doc
title: The drill's type census
description: "Which stored classes a recovery drill accounts for, how a retired class is told from a revived one, and why the verdict is not dated."
tags:
- services
- backup
- walg
---

# The drill's type census

`verify_restored_database` reads the type names out of the newest checkpoint of the newest
`CENSUS_THREADS` (200) conversations — each checkpoint's materialized channels plus the writes
of that checkpoint — and accounts for every name it finds. Both drills share it: the logical
artifact restore and the WAL-G drill.

- **Expected.** Loadable: LangGraph's own safe set plus `STATIC_CHECKPOINT_MSGPACK_TYPES`.
  Retired: `RETIRED_CHECKPOINT_MSGPACK_TYPES`, the classes whose rows remain after the class
  was deleted.
- **A fault** is an unknown name, a row this build cannot decode, a retired name **this
  build's reader still resolves**, or a sample that exposed nothing to read — a zero
  denominator is not a pass.
- **The verdict is not dated.** A write after the deleting commit proves only that some host
  still ran pre-deletion code; this cluster's deploys are user-gated (2026-10-30: five of five
  newest conversations carried a `NativeWorkTarget` blob first written 2026-10-21 04:0xZ, nine
  hours after the deletion landed on 2026-10-20 11:52Z). So `retired_at` in the registry is
  context, never a verdict.
- **How "revived" is asked.** Through the reader's own serializer, not `importlib`: the stored
  row is decoded twice, once as the reader runs and once with the retired name added to the
  allowlist. A difference means the class is in this tree and the stored arguments still build
  it — the registry contradicting the reader.
- **The record carries the denominators**:
  `type census: 10 types over 200 conversations (2026-10-23…2026-10-30), 2 145 blobs + 903
  writes = …, 0 unknown, 2 retired (0 revived)`.
