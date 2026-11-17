# Message image layout

Human messages place their image thumbnails before their body, matching the
requested reading order. The shared thumbnail trigger now follows the image's
natural dimensions within the existing 256 × 192 px bounds and the pane width.
The previous fixed 4:3 trigger reserved large empty bands around wide pictures.
Attach caption pairing and upload file-card ordering remain unchanged.

The fixed trigger previously reserved layout before decode. Upload references
have no dependable dimensions, so removing that reservation allows initial
layout to change. Adding upload metadata would widen the wire contract; this
change instead reuses the current reading bookmark after image load or failure,
including histories below the row-measurement threshold. The existing owner of
width-change compensation captures image events within its viewport and reapplies
the latest bookmark in the next frame, allowing failure fallbacks to commit.
Following and active send/restore holds retain their existing owners.

Validation reproduced both visible defects in the real TimelineView test
consumer before implementation, and reproduced short-history reader displacement
before extending bookmark compensation. Local Chromium fixtures exercise actual
TimelineView markup and CSS without a production gateway. Full suites remain CI
work; these fixtures do not establish a production rollout.
