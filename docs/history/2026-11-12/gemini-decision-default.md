# Gemini Decision 1 as the decision default

The user requested Gemini decision support and its adoption as our default. Google announced the dedicated Gemini Decision 1 model on November 11, 2026; its typed probability answers match our separate decision primitive. We register it in the Google builtin plugin, keeping chat model selection independent. The skill recall, memory rerank and reply-suggestion gate defaults move together; explicit pins remain unchanged.

The native REST adapter avoids an SDK/dependency upgrade and preserves request/error accounting. The existing Google key is reused through injected provider keys, with synthetic text-only connectivity verified. No business messages were sent as probes. Prices are $0.10 per million input tokens and zero output. Source: https://blog.google/innovation-and-ai/technology/developers-tools/gemini-decisions-api/ .

Existing thresholds were calibrated on Jev. This change preserves those user-visible policy thresholds rather than mixing provider integration with threshold retuning; synthetic protocol checks establish interoperability, not production calibration quality. Explicit model overrides provide rollback to Jev after the ordinary rollout boundary. No live agents, stored pins, operator targets or production configuration are migrated by this code change.
