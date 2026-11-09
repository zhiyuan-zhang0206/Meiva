# Built-in plugins

These packages ship with Meiva. The generic registration, contribution surfaces,
loading and manifest contracts belong to [Plugin System](../../docs/plugins/plugins.md).
Each implementation below owns its behavior and component documentation.

## DeepSeek provider plugin

The DeepSeek provider (`meiva_builtins/plugins/lm_deepseek/provider.py`) talks to DeepSeek's Anthropic-compatible endpoint via `DeepSeekChatModel`, reads `DEEPSEEK_API_KEY` from process environment or `.env` aliases (so gateway draft models work after capability-key stripping), maps graded reasoning effort onto `output_config.effort` via `extra_body` and `none` onto `thinking={"type": "disabled"}`, and declares native image attachment support (`media_types=frozenset({"image"})`, 32 MiB inline limit, 8192 px up to 14 images / 4096 px from 15 images) on `deepseek-flash` while keeping the prefix fallback `vision=False`.

`deepseek-flash` (DeepSeek V4.1-Flash) accepts Anthropic-format `image` blocks
on `/anthropic` (`document` blocks are unsupported, so PDF remains out), pins a
150s stream TTFT timeout to bound queue stalls, and uses 0.374 / 0.512
compaction thresholds on its 1M context window.

| Plugin | Responsibility |
|---|---|
| [meiva_cloudflared — Cloudflared Tunnel Keepalive Plugin](meiva_cloudflared/README.md) | Cloudflared tunnel keepalive watchdog service |
| [meiva_code — Coding Convention Plugin](meiva_code/docs/meiva_code.md) | Coding workspace and context |
| [meiva_fleet — Fleet Plugin Overview](meiva_fleet/docs/meiva_fleet.md) | Agent collaboration and tasks |
| [meiva_memory — Memory Pool Plugin](meiva_memory/docs/meiva_memory.md) | Long-term memory |
| [meiva_self_evolution — Self-Evolution Plugin](meiva_self_evolution/README.md) | Self-evolution hooks and compact reminder |
| [meiva_tailscale — Tailscale Mesh VPN Keepalive Plugin](meiva_tailscale/README.md) | Tailscale mesh VPN keepalive watchdog service |
| [meiva_to_user — To-User Panel Soft-Limit Reminder](meiva_to_user/docs/meiva_to_user.md) | To-user panel soft-limit reminder |
| [meiva_usage — Scheduled Multi-Account Usage and Balance Collectors](meiva_usage/README.md) | Scheduled multi-account usage and balance collectors |

Skills and MCP packages have separate catalogs under Built-ins; plugin-owned
skills remain under their plugin's component tree.

On a `meiva.understand` media call a Gemini model answers, `media_resolution` maps `low`/`medium`/`high` onto `google.genai.types.MediaResolution` (imported lazily inside the provider branch) and `media_thinking_level` takes precedence over global `MEIVA_REASONING_EFFORT` while leaving `include_thoughts=None` so thought blocks are not requested. On the agent main path, `include_thoughts=True` surfaces canonical `{"type": "thinking", "thinking": ...}` blocks unless thinking is disabled. `gemini-3.8-flash`, `gemini-3.1-pro-preview`, and `gemini-4-argon` declare `effort_levels=("low", "medium", "high")` because `minimal` returns HTTP 400 on those models; `gemini-3.1-pro-preview` additionally pins `llm_retry_max_attempts=10` (preview rate limits) and `llm_stream_ttft_timeout_seconds=90.0` (Gemini streams no protocol preamble before the first thought/content chunk); `gemini-4-argon` pins the same 90s TTFT because google-genai retries a 429/503 internally for ~31s without emitting a byte, so the 30s shared floor kills a throttled-but-healthy request (agent 6933, 2026-10-18). `gemini-flash-lite-latest` tracks Google's stable Flash-Lite alias (`gemini-3.5-flash-lite`, defaulting to `minimal`).
