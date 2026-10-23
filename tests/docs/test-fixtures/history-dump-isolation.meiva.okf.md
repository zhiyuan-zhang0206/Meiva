---
type: doc
title: History-dump test isolation
description: Consistent boot defaults and explicit feature scenarios for pre-compact history dumps.
tags: [testing, compaction]
---

# History-dump test isolation

History dumping defaults on in production. The test bootstrap pins
`MEIVA_COMPACT_HISTORY_DUMP=false` before the first settings construction and
records the same key in the private home's `.env`, so the singleton, pre-warmed
fallback, fresh settings and subprocesses agree. Pinning only the singleton
would miss the import-time snapshot; omitting the cluster-scoped key from `.env`
would let the authority pass discard it. Keeping ordinary compaction tests off
also preserves their exact summary tails without extra dump notes or JSONL
files. History feature tests pass an enabled `HistoryDump` value or their own
runtime slices for each scenario, without changing the settings singleton.
