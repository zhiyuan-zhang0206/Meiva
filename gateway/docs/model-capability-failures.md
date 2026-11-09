# Model capability failures

[Gateway model catalog failure boundary](../http/docs/model-catalog.md) owns catalog availability. An enabled
provider failure leaves the authenticated management surface running; requests
that require model metadata fail explicitly. This is separate from database,
core-configuration and authentication startup validity.

## Relationship to Other Nodes

- [Gateway](gateway.md) — the domain node; catalog ownership is [Model Catalog](../http/docs/model-catalog.md).
