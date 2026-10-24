---
type: doc
title: SMS — SMS / Verification Code Reading
description: macOS Messages.app SMS reading skill — queries the local Messages database (`~/Library/Messages/chat.db`)
tags: []
---

# SMS — SMS / Verification Code Reading

## What is it
A macOS Messages.app SMS reading **skill** (`meiva_builtins/skills/integrations/sms/`) — queries the local Messages database (`~/Library/Messages/chat.db`) to retrieve 2FA verification codes and recent SMS messages. It is a self-contained on-demand script, not a persistent daemon, and not on the service roster (`build_services()`) — when an agent needs a 2FA code, it invokes it via `meiva.skills.sms`, following SKILL.md and running `.venv/bin/python meiva_builtins/skills/integrations/sms/scripts/query.py`.

## Core Responsibilities
- **Decisions over values** (`meiva_builtins/skills/integrations/sms/scripts/sms_messages.py`, a `strict` module): the verification-code patterns (`extract_codes`), the chat.db query for a lookback window (`build_chat_query`: SQL and parameters; a codes-only query scans a wider candidate window; a phone suffix matches literally), the mapping of the returned rows to messages (`select_messages`) and the printed lines. A page or window that selects nothing (`--limit 0`) is refused when the `ChatFilter` is built, and the CLI reports it as a usage error
- **Access pattern**: `query.py` reads chat.db directly via sqlite3 (`fetch_rows`); when blocked by TCC, `sqlite3.OperationalError` is raised directly (no fallback — fail fast, resolves by granting Full Disk Access to the calling process). The database path and `now` are inputs of `main`; only the `__main__` block reads `Path.home()` and the clock
- **CLI interface**: `.venv/bin/python meiva_builtins/skills/integrations/sms/scripts/query.py --recent-codes --phone-suffix 1118`

## Key Dependencies
- [[meiva_builtins/skills/docs/skills.meiva.okf.md|Skills index]] — full skills catalog
- [[tool-calls.meiva.okf.md]] — agent invokes this capability via the `meiva.skills.sms` skill

## Entry Points
- `meiva_builtins/skills/integrations/sms/scripts/query.py:query_messages` — the messages a filter selects from a chat.db
- `meiva_builtins/skills/integrations/sms/scripts/query.py:main` — CLI entry
- `meiva_builtins/skills/integrations/sms/SKILL.md` — skill manifest + usage

## Notes
- macOS only (depends on `~/Library/Messages/chat.db`)
- On-demand skill script, not a persistent daemon
