---
type: doc
title: "IM notice lookup after channel retirement"
description: "Manual notice lookup and retained historical native-poll receipts after Telegram retirement."
---

# IM notice lookup

The native Telegram automatic notice poller is retired. `/notice list` reads
open response-required notices and returns their rendered text in the requesting
conversation. Shared UI notices, account-bound reply windows and resolution
handlers remain available. Existing poll cutover, acceptance receipts and queued
outbound records remain in Postgres for history; the retained adapter set cannot
dispatch the retired Telegram or Feishu accounts.

The decision is recorded in
[IM channel retirement](../../../../docs/history/2026-11-06/im-channel-retirement.md).
