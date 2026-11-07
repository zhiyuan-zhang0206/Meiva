from __future__ import annotations

from pathlib import Path

from meizel.tests.synthetic import RepoFactory, analysis_of, diagnostics, failures

ROOT = '[roles]\napplication = ["app"]\n'
MANIFEST = '[purity]\n"mod.py" = "strict"\n'
INJECTED = (
    "from pathlib import Path\n\n\ndef read(deps: object) -> str:\n    return deps.read_text()\n"
)
REAL = "from pathlib import Path\n\n\ndef read() -> str:\n    return Path('a').read_text()\n"


def repo(make_repo: RepoFactory, source: str, sites: dict[str, str], *, ceiling: str = "1") -> Path:
    listed = "".join(f'"{key}" = "{why}"\n' for key, why in sites.items())
    policy = (
        '[injected-calls]\n"max-sites" = ["' + ceiling + '"]\n\n[injected-calls.sites]\n' + listed
    )
    return make_repo(
        {
            "meizel.toml": ROOT,
            "meizel.policy.toml": policy,
            "app/__init__.py": "",
            "app/meizel.toml": MANIFEST,
            "app/mod.py": source,
        }
    )


# meiva:note
# The false positive this rule exists for: the purity rule matches a path method by its attribute name,
# so a callable handed in as a parameter reads as `Path.read_text`. A declared site says which call it
# is -- and the run says so separately, so the real reads are not added to it.
def test_a_declared_injected_call_is_not_an_impure_call(make_repo: RepoFactory) -> None:
    site = "app/mod.py#read#deps.read_text"
    root = repo(make_repo, INJECTED, {site: "read= is the reader the entry hands in, not a Path"})
    assert analysis_of(root).verdict == []
    assert [d.code.value for d in diagnostics(root, "impure-call")] == []
    scan = analysis_of(root).scans["app/mod.py"]
    assert scan.injected == (site,)
    assert "1 site declared an injected callable, not a read" in analysis_of(root).ok_line()


# meiva:note
# The positive control in miniature: the declaration covers the site it names and no other. A real read
# in the same file is still reported, still recorded, and still counted.
def test_a_real_read_beside_a_declared_site_is_still_reported(make_repo: RepoFactory) -> None:
    source = (
        "from pathlib import Path\n\n\ndef read(deps: object) -> str:\n"
        "    return deps.read_text()\n\n\ndef other() -> str:\n    return Path('a').read_text()\n"
    )
    site = "app/mod.py#read#deps.read_text"
    root = repo(make_repo, source, {site: "read= is the reader the entry hands in"})
    assert [d.code.value for d in diagnostics(root, "impure-call")] == ["impure-call"]
    assert [d.message for d in diagnostics(root, "impure-call")] == [
        "impure filesystem call `Path.read_text` in strict module"
    ]
    assert ("impure-call", "app/mod.py") in failures(root)


# meiva:note
# The counter-proof, one shape at a time: a site whose receiver is provably a path cannot be declared.
# The receiver is not a parameter, nothing honours the entry, and the approval is stale -- so the
# declaration is never a shield over a read.
def test_a_declared_real_read_is_refused(make_repo: RepoFactory) -> None:
    site = "app/mod.py#read#Path.read_text"
    root = repo(make_repo, REAL, {site: "claims the read is injected"})
    assert ("stale-approval", "app/mod.py") in failures(root)
    assert [d.message for d in diagnostics(root, "stale-approval")] == [
        f"[injected-calls.sites] {site!r} approves no injected call: "
        "the function makes no call with that receiver and method"
    ]
    assert ("impure-call", "app/mod.py") in failures(root)


# meiva:note
# The same refusal for the shape a narrowing would have missed: `p = Path(...)` then `p.read_text()`.
# `p` is a local, not a parameter, so no declaration reaches it.
def test_a_declared_read_through_a_local_path_is_refused(make_repo: RepoFactory) -> None:
    source = (
        "from pathlib import Path\n\n\ndef read() -> str:\n"
        "    p = Path('a')\n    return p.read_text()\n"
    )
    site = "app/mod.py#read#p.read_text"
    root = repo(make_repo, source, {site: "claims the read is injected"})
    assert [d.code.value for d in diagnostics(root, "stale-approval")] == ["stale-approval"]
    assert [d.message for d in diagnostics(root, "stale-approval")] == [
        f"[injected-calls.sites] {site!r} approves no injected call: "
        "`p` is not a parameter of the function"
    ]
    assert [d.code.value for d in diagnostics(root, "impure-call")] == ["impure-call"]


# meiva:note
# The strongest form of the counter-proof: a parameter that is annotated a path holds one, so the call
# on it is a read even though the receiver is a parameter. Nothing honours the entry, and the read is
# reported.
def test_a_declared_read_on_a_path_parameter_is_refused(make_repo: RepoFactory) -> None:
    source = (
        "from pathlib import Path\n\n\ndef read(path: Path) -> str:\n    return path.read_text()\n"
    )
    site = "app/mod.py#read#path.read_text"
    root = repo(make_repo, source, {site: "claims the read is injected"})
    assert [d.code.value for d in diagnostics(root, "stale-approval")] == ["stale-approval"]
    assert [d.message for d in diagnostics(root, "stale-approval")] == [
        f"[injected-calls.sites] {site!r} approves no injected call: "
        "`path` is annotated a path type, so a call on it reads"
    ]
    assert [d.code.value for d in diagnostics(root, "impure-call")] == ["impure-call"]


# meiva:note
# The list is a budget, not a door: a run that finds more declared sites than the ceiling refuses
# rather than hiding one more reported read.
def test_more_sites_than_the_ceiling_is_a_budget_failure(make_repo: RepoFactory) -> None:
    sites = {
        "app/mod.py#read#deps.read_text": "the reader handed in",
        "app/mod.py#read#deps.write_text": "the writer handed in",
    }
    root = repo(make_repo, INJECTED, sites)
    assert ("approved-budget", "meizel.policy.toml") in failures(root)


# meiva:note
# A key that names no site shape no scanner could honour is a policy defect, not a silent no-op.
def test_a_site_key_of_the_wrong_shape_is_a_policy_defect(make_repo: RepoFactory) -> None:
    root = repo(make_repo, INJECTED, {"app/mod.py#read_text": "no function, no receiver"})
    assert ("policy-syntax", "meizel.policy.toml") in failures(root)
