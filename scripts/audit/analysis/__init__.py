# meiva:note
# The pure decisions of the audit tools in `scripts/audit`.
#
# Each audit is an effect shell: it reads the checkout, the GitHub API or the database and prints
# what these modules decide. Grouping a reference into its report, what a module move leaves
# behind, what the branch-protection and schema declarations promise, and what the impact graph
# measures are all values in, decisions out.

from __future__ import annotations

from scripts.audit.analysis.ddl import schema_columns, strip_comments
from scripts.audit.analysis.moves import (
    is_text,
    module_pair,
    old_references,
    pair_result,
    referenced_names,
    verdict_line,
)
from scripts.audit.analysis.protection import (
    audit_target,
    drift_report,
    encode_branch,
    expected_checks,
    mapping,
)
from scripts.audit.analysis.protection_report import error_detail, outcome, report_text
from scripts.audit.analysis.reach import Census, Site, census, report_lines
from scripts.audit.analysis.references import (
    GENERATED_FILES,
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
from scripts.audit.analysis.report import (
    Report,
    as_json,
    group_of,
    limit_error,
    relative_target,
    render,
    scan,
)
from scripts.audit.analysis.schema import (
    Drift,
    allowlist_entries,
    classify_column,
    classify_table,
    compare,
    live_columns,
)

__all__ = [
    "GENERATED_FILES",
    "Census",
    "Door",
    "Drift",
    "Hit",
    "Repo",
    "Report",
    "Site",
    "Target",
    "allowlist_entries",
    "as_json",
    "audit_target",
    "build_matcher",
    "census",
    "classify_column",
    "classify_table",
    "compare",
    "drift_report",
    "encode_branch",
    "error_detail",
    "expected_checks",
    "file_hits",
    "group_of",
    "index_repo",
    "is_doc_file",
    "is_text",
    "limit_error",
    "live_columns",
    "mapping",
    "module_pair",
    "old_references",
    "outcome",
    "pair_result",
    "referenced_names",
    "relative_target",
    "render",
    "report_lines",
    "report_text",
    "resolve",
    "scan",
    "schema_columns",
    "strip_comments",
    "verdict_line",
]
