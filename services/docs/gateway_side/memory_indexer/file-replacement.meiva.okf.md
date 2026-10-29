---
type: doc
title: Memory Index File Replacement
description: Full-file chunk replacement and why stale-tail deletion must precede the new rows' upsert.
---

# Memory Index File Replacement

a re-embedded file commits as one unit — after every row of the file is embedded, the new rows upsert and the rows the current content no longer produces (removed description, shrunken body tail, emptied file) are deleted via `delete_stale_rows` (kind -> current row count; the indexer writes contiguous chunk_idx from 0, so any row past its kind's count is a stale tail). The tail delete runs BEFORE the upsert: if the upsert then fails, the surviving rows still carry the old content hash, so `all_meta` keeps disagreeing with the file and the next patrol re-embeds it — the reverse order would leave the tail while the new mtime/hash make the file look current (the state issue #1946 is about). A file whose embedding fails part way keeps its old rows intact (consistent all-old state; the next patrol retries it) and only fully-embedded sibling files commit.
