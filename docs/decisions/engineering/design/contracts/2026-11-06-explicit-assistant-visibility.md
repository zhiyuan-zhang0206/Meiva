# Declare assistant text visibility at the start of each block

## Context

A one-day audit found 37 background text blocks whose attempted user-facing
header was lost to punctuation or Markdown formatting. Other background
blocks addressed the user without any header. The web timeline could surface
a background block temporarily simply because it was the latest text.

## Decision

Require a standalone first-line marker on new native and external harness
assistant text blocks. The marker selects the main conversation or expandable
Details regardless of wake source. Invalid text stays in Details, and the
model receives a correction note. A terminal reply has one immediate repair
opportunity. Stored and streamed output carries a discriminator so historical
messages keep their original routing.

## Consequences

Visibility is explicit before the user sees any body text. The model still
decides what warrants the user's attention. Invalid terminal text can incur
one extra model call. Details is a presentation control and remains readable.

## Alternatives considered

A more permissive parser for the previous marker would recover punctuation
and Markdown variants, while still leaving missing intent ambiguous. A
dedicated structured model field could encode visibility, but would require
a common structured response mechanism across native model providers and
external harnesses.
