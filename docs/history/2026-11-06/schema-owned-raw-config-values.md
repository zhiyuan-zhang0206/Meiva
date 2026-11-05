# Read config values through their model's raw field mapping

Core candidate validation and installed-plugin metadata must follow the schema,
including fields that an installed plugin introduces after this repository was
built. Replacing those lookups with a copied field list would create another
owner and silently miss new fields.

Both readers now use Pydantic's public iteration protocol to capture raw field
values, then select by the owning schema's field names. A serialized model dump
was rejected: it can omit excluded fields and turn nested models into mappings.
The raw mapping retains those fields, field names rather than aliases, nested
object identities and SecretStr values. Candidate validation still controls its
updates, removals and defaults, and metadata still admits only declared fields.

This repays two dynamic-attribute occurrences without changing the checker,
allowances, roots or the accepted schema. The misleading fixed-name premise in
the former debt reasons was not used to narrow the plugin surface.
