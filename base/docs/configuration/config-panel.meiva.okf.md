---
type: doc
title: Configuration Panel Metadata
description: Registry and display metadata for configuration editors.
tags:
- configuration
- infrastructure
---

# Configuration Panel Metadata

`base/config/config_registry.py` is the single projection of field aliases,
annotations, editor types, choices, and `json_schema_extra` metadata. Both the
gateway metadata view and the local config CLI use it, preventing scope,
sensitivity, and editability policy from diverging.

Panel field labels and descriptions come from each Pydantic field declaration
(`title` and `description`). Every core field declares a title; panel metadata
fails if one is missing or blank. Plugin fields can declare a title alongside
their scope metadata. For compatibility, plugin fields without a title display
their field name. The API exposes the environment variable separately as a
technical identifier.

`base/config/admin/metadata.py` and `base/packages/plugin_config_admin.py`
compose the core and plugin field metadata. The gateway sends the labels on
both the configuration and per-model resolved views, and the frontend uses
them for field names while retaining environment identifiers in details and
search. The broader settings and bootstrap context is in
[[base/docs/configuration/configuration.meiva.okf.md]].
