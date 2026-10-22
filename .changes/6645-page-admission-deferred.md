type: Fixed

- A page whose shell could not be admitted while `pty-sessions` was still starting is
  now deferred to the next reconcile pass instead of being closed: the failed attempt
  releases the `session_name` it had reserved (compare-and-set, so a concurrent
  reconcile's newer name survives), and the page server asks the backend
  (`SessionBackend.service_available()`) whether the service is there at all before
  treating a creation failure as this page's fault. A restart no longer costs every
  open page its session and its registration (task #5458).
