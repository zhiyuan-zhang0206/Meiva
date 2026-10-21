type: Changed

- `meiva.ui.notify` now fails while the agent already holds an open notice, naming
  that notice's id and the two ways out — dismiss it or edit it in place — instead of
  silently replacing it. A second post used to supersede the first, which over 30 days
  of production erased 51.7% of the 259 notices that needed an answer before the user
  saw them ([notify contract](meiva_builtins/plugins/meiva_fleet/docs/notify.meiva.okf.md)).
