# Quota reset countdown

Quota cards need live relative deadlines for every window. Rendering dates inside
producer detail text lost the year and zone and selected only one Claude window.
We chose an additive labelled deadline array on existing stat values so the
browser can render complete provider instants with its shared clock and formatters.
Old rows keep an empty array until a normal probe refreshes them. Cached readings
retain their deadlines during rate limiting and error hysteresis; elapsed deadlines
say awaiting refresh because only a subsequent provider response confirms new quota.

Parsing detail text was rejected because presentation text cannot preserve the
original timestamp contract. A new general card framework was unnecessary for this
single additive field.


Review update: retained ordinary errors must preserve the reading timestamp as well
as cached quota and deadlines. Refreshing that timestamp on a failed probe made
repeated failures appear fresh. The writer now carries the retained-snapshot
choice through to the existing keep-stamp upsert, while errors without a cached
measurement and successful recovery keep their normal write timestamps.
