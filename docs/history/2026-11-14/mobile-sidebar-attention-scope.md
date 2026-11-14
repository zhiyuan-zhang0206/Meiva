# Mobile sidebar attention scope and theme color

On 2026-11-14 the user reported a deep-orange `99+` phone sidebar badge and
expected `1` using the theme orange. The previous decision selected a deeper
orange for small white digits; the user correction chooses the shared sunset
orange with white digits. Its measured white contrast is approximately 2.56:1
in both themes, below the small-text AA threshold. We preserve the requested
color instead of silently substituting another shade.

The prior decision is preserved in
[Reserve orange for attention](../2026-11-12/reserve-orange-for-attention.md).
