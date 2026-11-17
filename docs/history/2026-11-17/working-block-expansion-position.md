# Preserve the reading position when opening work

On 2026-11-17 the user asked for a working block to stay where it is when
opened. This superseded the 2026-11-09 choice to jump to the expanded active
block's bottom and follow its output (implemented in
[PR #8772](https://github.com/zhiyuan-zhang0206/Meiva/pull/8772), commit
`9a74a6ce6db12e686fa8e1a17f5c5790c62666c7`). Active and settled manual expansion now
share the existing header-position anchor. Explicit navigation to the bottom,
new sends, and automatic expansion retain their existing contracts.

The removed live-only branch requested following and started a tail ride.
Using the existing settled-block path avoids a second scrolling mechanism.
The TimelineView consumer regression first failed for live expansion from
following, paused, and riding states; settled controls already passed.

Current behavior is owned by [the live-block contract](../../../ui/web/src/docs/frontend-components/timeline-view/live-block.md).
