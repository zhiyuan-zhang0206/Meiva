---
type: doc
title: The send anchor
description: A send parks the reader's own message a fixed share down the viewport over a bottom reserve the reply consumes, so streaming never moves the reader.
tags:
- frontend
---

# The send anchor

A send is its own arrival, not a pin. The page hands the timeline the inbound
its delivery receipt named (`sendAnchor` in the timeline store), and
`useSendAnchor` waits for that row to mount, parks it `SEND_ANCHOR_TOP_SHARE`
(20%) down the viewport and reports `notifyAnchored`: following stops there, so
every later chunk paints below the reader's place instead of moving it. A reader
parked mid-history is parked there too — they just wrote the message — and a
send whose row never mounts moves nothing.

Parking needs room the reply has not written yet: a bottom reserve
(`[data-send-reserve]`, outside the content column so it carries no row
spacing) fills exactly the height missing between the parked position and the
current end, and gives back every pixel the reply grows, so `scrollHeight` —
and with it the viewport — stays put until the reply fills the viewport and
starts growing past it.

The reserve is also why the anchor owns the follow flag instead of merely
scrolling. While it is there the viewport sits at the bottom of a padded
document, so the position reads "at bottom" with nothing to follow, and the
at-bottom reconciliation would re-stick the reader on the first layout pass and
drag them down again. The controller ignores that reconciliation until it
observes the viewport genuinely away from the bottom — the point where the
reply has consumed the reserve — and from there the ordinary rules decide
again: scrolling down to the bottom re-follows, so does the scroll-to-bottom
control (it stays shown while the anchor holds, because it is the reader's only
way back), and a thread change drops the reserve with the mount. A bookmark
still settling is released at the moment the row is parked, not when the
request arrives: until then the reader's saved place still owns the viewport.

## Relationship to Other Nodes

- [[ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.meiva.okf.md|Timeline sticky scroll]] — the follow flag and scroll ownership this node extends.
