# Authorize Mermaid styles under the production policy

Production requires a response nonce on style elements. Mermaid's generated
stylesheet lacked it during measurement and after SVG injection, producing
black filled paths and hiding labels. Development allows inline styles, so its
normal preview concealed the defect.

Use the existing trusted nonce context. Mermaid 12.3.0 exposes no nonce option;
a pinned pnpm patch forwards the caller-owned measurement host's nonce to its
generated style before insertion. After the strict renderer sanitizes the SVG,
restore the nonce only on its generated root style. Connected-element
serialization hides the nonce attribute, so measurement authorization alone
does not authorize the final SVG.

An observer would run after insertion and could race layout. Giving every style
in model-supplied markup the response nonce would widen trust. The selected path
owns one generated stylesheet and preserves Mermaid's strict sanitizer and the
production policy. The sanitizer's transient parse can still emit a style CSP
rejection; this change keeps that security boundary rather than bypassing it.

A local real-library fixture with the production policy reproduced black paths
before the change. Browser controls cover simultaneous and sequential diagrams,
light/dark themes, invalid source fallback and hostile label markup. This is
isolated fixture evidence, with no production data or gateway involved.
