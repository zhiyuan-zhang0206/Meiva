# meiva:note
# The auditor's promise: a landed commit is judged by the receipt checker its own tree carried, and
# a commit the tool cannot judge is reported as its own class instead of passing silently.


from __future__ import annotations

from pathlib import Path

from scripts.hooks.audit_merge_receipts import (
    Judgement,
    Landed,
    evaluate,
    exit_code,
    full_sha,
    gate,
    receipt_lines,
)

# meiva:note
# Two checkers that differ the way the real ones did: the newer vocabulary requires `scope`, the
# older one never heard of it. Both take the three arguments the real checker takes, so the
# evaluation path is the one under test.
_HEADER = (
    "import argparse, sys\n"
    "parser = argparse.ArgumentParser()\n"
    "parser.add_argument('--receipt-file')\n"
    "parser.add_argument('--head')\n"
    "parser.add_argument('--repo-root')\n"
    "arguments = parser.parse_args()\n"
    "line = open(arguments.receipt_file).read()\n"
)
_OLD = _HEADER + "sys.exit(0 if 'diff-checks/v1 ' in line else 1)\n"
_NEW = _HEADER + "sys.exit(0 if 'scope=' in line else 1)\n"

_OLD_RECEIPT = (
    "diff-checks/v1 @abc123 base=def456 pyright=0err(1m36s) err_in_this_diff=0 ruff=ok(0s) "
    "tests=RAN passed=645(6m40s) failed=0 generated=scripts/hooks/diff_checks.py"
)


# meiva:note
# The defect this prevents: judging history with today's vocabulary calls every honest old receipt
# a violation. The same line, two checkers, two verdicts -- and the pin decides which one is read.
def test_a_line_is_judged_by_the_checker_it_is_pinned_to(tmp_path: Path) -> None:
    passed, _ = evaluate(_OLD, _OLD_RECEIPT, "abc123", tmp_path)
    rejected, detail = evaluate(_NEW, _OLD_RECEIPT, "abc123", tmp_path)

    assert passed is True, "the commit's own vocabulary accepts the line it produced"
    assert rejected is False, "a newer checker would refuse a field the line predates"
    assert detail


def test_receipt_lines_are_the_lines_that_start_with_the_prefix() -> None:
    body = "## What\n\ndiff-checks/v1 @abc123 base=def456 x=1\n and a sentence about it\n"

    assert receipt_lines(body) == ["diff-checks/v1 @abc123 base=def456 x=1"]


# meiva:note
# A commit with no pull request number is a different fact from a commit whose pull request carried
# no receipt: the first says nothing about the gate, the second is what it refuses.
def test_a_commit_without_a_pull_request_is_its_own_class_not_a_violation() -> None:
    alone = Judgement(Landed("abc", "a direct commit"), "no-pull-request", "")
    missing = Judgement(Landed("def", "a pull request (#1)"), "no-receipt", "")

    assert exit_code([alone]) == 0
    assert exit_code([missing]) == 1


# meiva:note
# A tool that could not read the pull request is a hole in the audit, and a hole is not a pass.
def test_an_unverified_commit_is_not_a_pass() -> None:
    unverified = Judgement(Landed("abc", "a pull request (#1)"), "unverified", "gh failed")

    assert exit_code([unverified]) == 2
    assert exit_code([unverified, Judgement(Landed("def", "(#2)"), "passed", "")]) == 2


# meiva:note
# A short sha never reaches the checker: it would compare a 12-character string against the
# receipt's 40 and report "produced for another commit" -- a false violation that blames the author
# for the auditor's argument.
def test_only_a_full_sha_is_handed_to_the_checker() -> None:
    assert full_sha("1d9a5e0e3281b7332effab60909bf09ceda25d48")
    assert not full_sha("1d9a5e0e3281")
    assert not full_sha("")


# meiva:note
# The gate guards what the cluster lands. A commit from the user's own identity carries no trailer
# and no receipt; reporting it as a violation would make the auditor's first output noise, and a
# protection nobody reads is not a protection.
def test_who_pushed_decides_whether_the_gate_applies() -> None:
    cluster = Landed(
        "abc", "a change (#1)", author="87293881+zhiyuan-zhang0206@users.noreply.github.com"
    )
    trailed = Landed(
        "def",
        "a change (#2)",
        author="someone@example.com",
        message="body\n\nCo-authored-by: Meiva #7230",
    )
    user = Landed("ghi", "a change (#3)", author="wuji.zhangagain@gmail.com")
    stranger = Landed("jkl", "a change (#4)", author="someone@example.com")

    assert gate(cluster, "") == "cluster-agent"
    assert gate(trailed, "") == "cluster-agent"
    assert gate(user, "") == "user-authored"
    assert gate(stranger, "").startswith("unknown(")


# meiva:note
# A commit outside the gate is not a violation and not a hole: it says so and the audit stays green.
def test_a_user_authored_commit_is_reported_aside_and_changes_no_exit_code() -> None:
    outside = Judgement(Landed("abc", "(#1)"), "not-gated", "user-authored")
    unknown = Judgement(Landed("def", "(#2)"), "unknown", "unknown(someone@example.com)")

    assert exit_code([outside]) == 0
    assert exit_code([unknown]) == 2
