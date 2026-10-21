type: Changed

- An expired notice now reaches its agent as a message that names the notice's
  local id, its title and when it lapsed, says nothing is pending and that the
  single open notice is free again — instead of a "Re: ..." that read like a user
  reply. The row is queued for a terminated owner too, with no wake and no
  resurrection, while the user's side stays silent: expiry posts no notification
  and the question only leaves the queue
  ([notify](meiva_builtins/plugins/meiva_fleet/docs/notify.meiva.okf.md)).
