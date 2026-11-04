---
type: doc
title: Control and Insights first paint
description: Why the vertical anchor pages paint their headings before their section bodies, and how the mount queue spreads the bodies and the config form over idle slices.
tags: [frontend, performance, control, insights]
---

# Settings and Insights first paint

Settings mounts only the selected category. Its default General panel contains the
model selector and curated fields; advanced configuration, inventories and schedules
load when selected. Legacy config sub-anchors select Advanced and display anchors
select Display. The header and navigation render immediately. Section bodies and
field groups still use the idle mount queue, so Advanced does not mount its entire
field catalog in one frame. Insights retains its vertical anchor layout.

The home sidebar prefetches the Settings route in production; the loading boundary
shows the Settings title while a cold route arrives. APIs start when their category
mounts. Unmounting a category stops its polling while query caching supports revisits.

Settings inventories keep implementation metadata out of ordinary rows. Skills
show the name and enablement control; their source layer still governs editability
without being displayed. MCP rows omit transport summaries (commands, socket paths
and endpoint URLs). Plugin descriptions remain visible as capability summaries.
