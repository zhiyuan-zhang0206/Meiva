# Restore collector silence after observing the emit fix

The earlier silence rule was retired because a schedule process had no bound
telemetry pipeline. The runner binding fix now reaches production Loki: a
read-only two-hour sample on November 11 contains all four collectors, including
the slowest collector at roughly thirty-minute intervals. This is sufficient
evidence to restore the existing global silence policy rather than introduce a
database reader or broaden monitoring grants.

The restored selector follows the event contract without a service-name filter.
The 35-minute window and five-minute pending period retain the prior policy.
This detects silence across the fleet; it does not identify one missing
collector while another continues emitting. Per-collector expected sets remain
a separate policy decision. Production rule activation requires the normal
provisioning rollout after review.
