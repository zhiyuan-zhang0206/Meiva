# Prompt injection — boundary map + what is (and is not) built

> **Status 2026-11-12: nothing scans or marks external content.** A rule-based
> content scanner (`meiva/security.py`, a model check on `web.fetch`, SECURITY notes on
> flagged content) existed and was deleted: it only matched keywords, never blocked, and
> gave a false sense of protection ([decision](../../../docs/decisions/data/security/2026-11-12-no-content-scan.md)).
> Content that enters an agent's context (web pages, files, MCP results, peer and
> inbound messages, context files, memory) reaches it unchanged. The only injection-aware
> gate left is the install-time skill scan (`base/packages/skills/scan.py`). Everything
> structural (sandboxed reader, egress allowlist, privilege separation) is still deferred.

## Why this is categorically different from the auth question

The gateway-auth threat is **bounded** (no actor exists outside the single-user
trust boundary — see [`non-goals.md`](../../../docs/conventions/non-goals.md) "Auth / multi-user"). Prompt
injection is **unbounded**: untrusted content flows straight into the most
powerful actor in the system. The agent holds `execute_code` (raw Python = bash +
the full SDK); the moment attacker-controlled text enters its context and hijacks
it, it can do anything the agent can — exfiltrate via web/channels, write files,
`spawn`/`terminate` peers, trigger a cluster update, poison memory. The attacker never needs
to be on the network; they only need the agent to *read* their content.

## Ingestion surface — where untrusted content enters

- `meiva.web.fetch` / `meiva.web.search`, `meiva.understand(url)`, `meiva.mcps.chrome.*`
  (a live browser) — arbitrary web content.
- `meiva.files.read` + **`meiva_code`'s AGENTS.md auto-injection** — cloning a
  malicious repo auto-walks its `AGENTS.md` into the prompt: direct injection the
  agent never "chose" to read.
- The content-feed skills (x / youtube / zhihu / xiaohongshu / douyin) —
  untrusted social content by definition.
- A peer's `send_message` — an already-injected agent injects its peers/parent.
- **Memory recall — the nastiest.** A poisoned note is *durable*,
  *cross-machine*, and *auto-recalled*: one injection re-fires for sessions.
- **Skill install** — a third-party skill is code + instructions; installing one
  from an untrusted source is direct code execution, not just content.

## The boundary truth

1. **Content-layer defenses are mitigation, not a boundary.** Delimiters ("treat
   as data, never instructions"), classifiers scanning ingested content, and even
   RL-training the model to resist injection all *lower the rate* — they do not
   close it. Anthropic's own stance: "no browser agent is immune… a 1% attack
   success rate still represents meaningful risk." OpenClaw write-ups call the
   un-structured case "the security boundary that doesn't exist."
2. **The only real boundary is structural** — OS-level sandbox (container /
   gvisor / firecracker / seccomp + separate UID) + network **egress allowlist** +
   privilege separation. And even this **leaks**: Claude Code's sandbox shipped
   repeated bypasses (SOCKS5 null-byte across ~130 versions, `/proc` reads,
   network-allowlist bypass). Structural = defense-in-depth, not a guarantee.
3. **Meiva-specific: there is no hard boundary today, by two design choices.**
   - Meiva is **autonomous — no per-action human-in-the-loop**. Claude Code's
     *primary* everyday defense is a permission prompt before sensitive ops; Meiva
     deliberately doesn't have it.
   - `execute_code` is **raw Python**, so `MEIVA_SDK_DISABLE` / capability scoping
     is **not** a security boundary — a hijacked agent just `import subprocess`.
     (This is the same point `non-goals.md` "Agent permission verification"
     already makes: the real boundary needs a sandbox.) Reader/actor privilege
     separation only becomes a *real* boundary once the reader is OS-sandboxed;
     without that it still has bash.

## What the field actually ships (so we don't reinvent later)

- **Claude Code** — layered: (1) permission system (human approves sensitive
  ops); (2) classifiers scanning untrusted content (hidden text, manipulated
  images, deceptive UI); (3) safe-command allowlisting; (4) a sandbox runtime
  (beta) with kernel-level fs + network isolation, egress via a proxy with a
  domain allowlist + prompt-on-new-domain; (5) RL training. Explicitly "not
  solved."
- **OpenClaw** — (1) XML delimiters + "data only" directive; (2) **reader/actor
  privilege separation** (a limited-tool reader summarizes raw content; an actor
  with action tools reads only the summary, never the raw content) — there is an
  arXiv paper specifically on this as OpenClaw's structural defense; (3) sandbox
  with only provisioned credentials ("attacker has a sandbox, not your machine");
  (4) **an on-install skill-poisoning scanner** that vets every skill at install
  time.

## Candidate defenses at common entry points (on the shelf, not building now)

Ranked by leverage for Meiva's actual shape. None built; listed so a future
trigger has a starting point rather than a blank page.

1. **On-install skill scan** (à la OpenClaw) — vet a skill's text for injection /
   exfil patterns when it enters via `meiva plugins install`. Cheapest high-value
   move *if* untrusted skills ever become a thing.
2. **Sandboxed, deprivileged, disposable reader** for untrusted ingestion (web /
   feeds / chrome): no host fs, no secrets in env, egress allowlisted, no
   cluster-update / channels / spawn; returns extracted data as an artifact to the
   trusted layer. This is the structural answer — but it needs the OS sandbox
   (the deferred non-goal) to be real, since reader tool-restriction alone is
   bypassable.
3. **Memory-write gating** — keep untrusted-ingesting context from committing
   durable memory; only the trusted layer writes after vetting (kills the
   persistent-injection vector).
4. **AGENTS.md auto-injection from trusted repos only** — gate the auto-walk so a
   freshly-cloned untrusted repo's `AGENTS.md` isn't silently injected.

## Triggers — when to actually build

- Installing skills / plugins from **untrusted or third-party** sources (today
  everything is the user's own / trusted) → build the on-install scan + reader
  sandbox first.
- Processing **attacker-chosen arbitrary URLs at scale** (vs the curated feed
  accounts the user follows).
- Going **multi-user** or exposing anything publicly (couples with the auth
  trigger).
- **Any real incident** — a single confirmed injection flips the cost/benefit.

## Open research (the "how broad is the surface" question)

Before building anything, measure rather than guess: which ingestion entry points
actually carry untrusted content in real use, how often, and into which agents
(do the privileged orchestrators ever ingest raw untrusted content directly, or
is it already mostly funnelled through fetch/feed paths that could be isolated
cheaply?). The answer decides whether #1–#4 above are even needed, and in what
order. This ties into the [sandbox non-goal](../../../docs/conventions/non-goals.md) whose
trigger ("agent runs untrusted third-party input") is arguably already met by
`web.fetch` / feeds / chrome — the gap to size is "met in principle" vs "met at a
volume/exposure that warrants the build."
