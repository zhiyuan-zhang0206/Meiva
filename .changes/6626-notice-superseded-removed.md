type: Removed

- `meiva.ui.notify` no longer returns a `.superseded` list on its `Notice`, and the
  `notice_resolved` event no longer carries `superseded_by`: with one open notice per
  agent nothing supersedes anything
  ([notice receipts](gateway/agents/docs/notice-receipts.meiva.okf.md)).
