# Preserve literal request delimiters

AGY request text has no escaping contract for delimiter-like user content. A
nesting parser or a list of regular-expression matches would assign meaning to
literal tags and could remove part of an inbox message before identity matching.
The reader therefore unwraps only the outside frame and preserves its interior,
including apparent queue separators. This favors retaining uncertain framing
over losing text; unknown trailers and incomplete frames remain intact.

The bounded five-host snapshot contained no completed multi-request row, so queue
controls are synthetic. Real input comparisons and synthetic defect controls are
reported separately in the contribution evidence.
