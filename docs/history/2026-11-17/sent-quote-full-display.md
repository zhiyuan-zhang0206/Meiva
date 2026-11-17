# Sent quote full display

On 2026-11-17 the user requested that sent timeline quotes display their complete saved snapshot by default. The sent card therefore preserves paragraph breaks and wraps long unbroken lines within the conversation column. Source navigation remains available.

The compact expandable Composer preview remains useful while composing and is unchanged. The 4000 Unicode character snapshot limit and send, persistence, and source authorization contracts remain unchanged. This decision supersedes the compact sent-card presentation in the initial quote implementation described in [selected text quotes](selected-text-quotes.md).

The existing stubbed Home browser fixture covers multiline saved quotes and unbroken text at 1280px and 390px, including default visibility, geometry and source navigation. It establishes rendering behavior; the original backend and Preview evidence continues to cover the unchanged transport.
