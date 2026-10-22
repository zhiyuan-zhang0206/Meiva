type: Fixed

- `meiva start` records a `lifecycle_request` row (`up`, naming the services it is
  about to launch) on a cold start, before the manifest is published and the root is
  spawned. A whole-cluster stop leaves no live root, so the per-unit `up` rows never
  existed for the start that followed; `audit_events` could name who stopped a host
  and not who brought it back. A start that dies during bring-up now names its caller
  too (task #5535).
