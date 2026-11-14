# Delegate Web Vitals measurement to the official library

The previous collector summed all unexpected layout shifts, selected the slowest
event duration, and stopped on the first hidden transition. Long authenticated
sessions therefore had different semantics from standard CLS and INP.

We selected the stable, dependency-free web-vitals 6.2.3 package rather than
maintaining session windows, interaction grouping/ranking and browser lifecycle
edge cases locally. The library owns measurement and document-lifetime observers;
the adapter owns scalar formatting and the existing telemetry transport. Default
reporting avoids sending every intermediate change. Soft-navigation measurement
and attribution are not enabled.

References checked on 2026-11-14:
- https://web.dev/articles/cls
- https://web.dev/articles/inp
- https://github.com/GoogleChrome/web-vitals/tree/v6.2.3
  (400d01968abffcd91a6f0307c9aaac97c5f7a76a)

The integration keeps element/key/value semantics and limits. It flushes when the
library actually reports, including delayed INP processing, so transport listener
ordering cannot strand those metrics. Observers remain document-scoped across
React remounts; the library has no disposal API. Test reset invalidates adapter
callbacks and is not a production observer cleanup mechanism.

Historical CLS/INP series and event counts have a semantic break at rollout:
windowed CLS, ranked interactions, subsequent hidden checkpoints and bfcache
restores differ from old samples. No product performance gain is established by
correcting measurement. The frontend telemetry owner documents interpretation.
