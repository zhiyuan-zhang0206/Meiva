# meiva:note
# The pure decisions of the audit tools in `scripts/audit`.
#
# Each audit is an effect shell: it reads the checkout, the GitHub API or the database and prints
# what these modules decide. Grouping a reference into its report, what a module move leaves
# behind, what the branch-protection and schema declarations promise, and what the impact graph
# measures are all values in, decisions out.

from __future__ import annotations

from scripts.audit.analysis.frozen import FROZEN_PREFIXES, is_frozen
from scripts.audit.analysis.moves import old_references, referenced_names
from scripts.audit.analysis.protection import (
    drift_report,
    encode_branch,
    expected_checks,
    mapping,
    trunk_gate_findings,
)
from scripts.audit.analysis.reach import Census, Site, census, report_lines
from scripts.audit.analysis.references import (
    Door,
    Hit,
    Repo,
    Target,
    build_matcher,
    file_hits,
    index_repo,
    is_doc_file,
    resolve,
)
from scripts.audit.analysis.report import Report, as_json, group_of, render, scan
from scripts.audit.analysis.schema import (
    Drift,
    classify_column,
    classify_table,
    compare,
    schema_columns,
    strip_comments,
)

__all__ = [
    "FROZEN_PREFIXES",
    "Census",
    "Door",
    "Drift",
    "Hit",
    "Repo",
    "Report",
    "Site",
    "Target",
    "as_json",
    "build_matcher",
    "census",
    "classify_column",
    "classify_table",
    "compare",
    "drift_report",
    "encode_branch",
    "expected_checks",
    "file_hits",
    "group_of",
    "index_repo",
    "is_doc_file",
    "is_frozen",
    "mapping",
    "old_references",
    "referenced_names",
    "render",
    "report_lines",
    "resolve",
    "scan",
    "schema_columns",
    "strip_comments",
    "trunk_gate_findings",
]
