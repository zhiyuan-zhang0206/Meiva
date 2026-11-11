# Strict memory OKF frontmatter

Memory notes accept only description, verified, and stale_after in frontmatter.
Unknown keys and legacy agent/ava HTML attribution comments now fail validation,
so retired metadata cannot silently survive consolidation. Index labels use
filenames; Dream flags every unknown key for cleanup. This keeps provenance in
inline source links and prevents a growing list of legacy exceptions.
