# One configurable policy for Meiva message timestamps

The user retained control over timestamps and weekday rendering. Uniformity means
Meiva delivery writers honor the same settings, rather than forcing them on.
System chat and synthetic notes previously had separate prefix responsibilities
that could omit stamps or add them twice. The note writer now owns formatting;
callers supply creation time and unprefixed content.

We retain the replacement-summary classifier heading before its timestamp so
existing structured classification and historical checkpoints remain readable.
Provider-owned internal events and execute_code attachment companions stay outside
the delivery contract. Adding text to attachment blocks would disturb their
multimodal pairing without improving message provenance.

The Timeline body contract remains separate from model-facing context: changing
formatting must not change which paragraphs a person sees or copies.

A timezone note uses one captured policy for both its timezone description and
its prefix. Reading the clock factory twice can mix independent snapshots or
consume two supplied observations for one note, so those facts travel together.
