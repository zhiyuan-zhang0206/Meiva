---
type: doc
title: Control configuration panels
description: Independently loading model views and ordinary configuration editors.
tags: [frontend, configuration]
---

# Control configuration panels

The ordinary field editor, cluster default-model selector and per-model resolved
view load independently. Failure to load a model catalog must not disable the
ordinary field editor or leave the model views claiming to load forever.

A failed model request produces a visible unavailable state and prevents model editing, while ordinary configuration fields remain editable. Provider exception text is not rendered.

Per-model values resolve shared code defaults, model code defaults and explicit
configuration. There is no extra per-model store: row labels reveal the existing
field editor, clearing filters and expanding its group if necessary. The source
badge names the winning layer; the weaker layers explain the values it shadowed.
The view follows the cluster default until the user picks a model. A spawn-time
agent overlay is outside this view.

The cluster default is a birth input with its own narrow write endpoint. Editing
it does not change existing agents. Local `picked` remains null until user input,
so a new server value cannot be overwritten by an untouched stale selection.
Superseded models stay visible when already stored but are absent from new
choices. After saving, both the stored default and shared model query refresh.

Page tests wait for the per-model request chain as well as the field editor when
asserting the settled page. Shared labels and badges are scoped to their panel;
counts taken after only one of the independent requests completes are unstable.

## Display grouping ownership

`app/control/config-group-defs.ts` owns the field-group membership and ordering tables,
hidden field list, filter choices and special model-panel anchors. The displayed
group labels come from `app/control/sections.ts`, which also owns the navigation anchors.
The default-model and per-model panels have their own elements and never become
ordinary field buckets, so an anchor exists once. Consumers import these tables
directly from their owner.

`app/control/config-groups.ts` owns field projection: explicit env-var membership wins over
the backend-domain fallback, and unknown domains remain visible in Other.
Unmapped fields sort after curated fields. Hidden fields still retain their raw
overrides in the merge-patch write; hiding a row must not change its value.
Group-membership and hidden-field tables remain disjoint, as the grouping test
asserts.
