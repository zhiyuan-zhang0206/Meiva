# The loading ring

`components/timeline/overlays.tsx` shares one ring between the reader's
older-page request and the cold load of a thread with nothing on screen yet.
Each draws an arc with a 15% opacity track and an 8-unit radius in a 24-unit
box. The older-page ring overlays the top of the viewport without changing
the message layout.

A reader who asked for `prefers-reduced-motion: reduce` gets the same ring
breathing (opacity only, `animate-pulse`) instead of a frozen frame: a still ring
reads as a stuck loader, not a busy one.

The ring is mounted for one reader request and stays that same element until the
request lands: a re-render reuses the `<svg>`, and a CSS animation restarts only
when its element is created or its animation name changes, so the spin runs on
rather than starting over. The store's in-flight flag also covers cold fill and compact-history retention.
The reader request alone presents the older-page ring. See
[Timeline history paging](history-paging.md).
