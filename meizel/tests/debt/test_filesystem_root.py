# meiva:note
# `filesystem`: the kind a `[roots]` declaration licenses a read of a file with, the effects it does
# not reach, and the declaration that stops being true when the read goes.

from __future__ import annotations

from pathlib import Path

from meizel.contracts.analysis import render_policy
from meizel.graph import FILESYSTEM_READS
from meizel.graph.model import OS_KINDS, AmbientKind
from meizel.language import DEFAULT_IMPURE_CALLS
from meizel.tests.synthetic import RepoFactory, analysis_of, diagnostics, failures

ROOT = '[roles]\napplication = ["app"]\n'
READ = "from pathlib import Path\n\ndef read() -> bytes:\n    return Path('x').read_bytes()\n"
WRITE = "from pathlib import Path\n\ndef store() -> None:\n    Path('x').write_text('y')\n"
DOOR = (
    '[purity]\n"mod.py" = "strict"\n'
    '[roots]\n"mod.py" = { kinds = ["filesystem"], why = "the source tree door" }\n'
)


def repo(make_repo: RepoFactory, files: dict[str, str], manifest: str = "") -> Path:
    return make_repo(
        {"meizel.toml": ROOT, "app/__init__.py": "", "app/meizel.toml": manifest, **files}
    )


# meiva:note
# The kind a manifest may declare: `[roots]` accepts the standard library's kinds and the policy's
# state kinds, and a name outside them is `manifest-syntax`. The read it licenses is a purity item,
# not an acquisition, so nothing else would notice the kind going missing here.
def test_the_kind_is_one_a_manifest_may_declare() -> None:
    assert AmbientKind.FILESYSTEM in OS_KINDS


# meiva:note
# The read a root pays for: no acquisition is recorded for it (a read of a file is an effect), so the
# declaration reaches the purity rule by name and the call stops being this file's impurity.
def test_a_filesystem_root_licenses_a_read(make_repo: RepoFactory) -> None:
    root = repo(make_repo, {"app/mod.py": READ}, DOOR)
    assert analysis_of(root).verdict == []


# meiva:note
# The other face of the same rule: without the declaration the call is what it was, so the licensing
# is a property of `[roots]` and not of the table.
def test_the_same_read_without_the_root_stays_an_impure_call(make_repo: RepoFactory) -> None:
    root = repo(make_repo, {"app/mod.py": READ}, '[purity]\n"mod.py" = "strict"\n')
    assert [d.message for d in diagnostics(root, "impure-call")] == [
        "impure filesystem call `Path.read_bytes` in strict module"
    ]


# meiva:note
# The boundary the kind keeps: a write is an effect of the same door, not a read of the filesystem,
# so a root that reads a file and writes another still owes the record for the write -- the boundary
# `time.sleep` keeps inside a `clock` root.
def test_a_filesystem_root_does_not_license_a_write(make_repo: RepoFactory) -> None:
    root = repo(make_repo, {"app/mod.py": WRITE}, DOOR)
    assert [d.message for d in diagnostics(root, "impure-call")] == [
        "impure filesystem call `Path.write_text` in strict module"
    ]


# meiva:note
# A call whose name does not decide whether it reads: `open` writes what its mode says, so the
# declaration cannot reach it and the file keeps the record.
def test_a_filesystem_root_does_not_license_an_open(make_repo: RepoFactory) -> None:
    source = "def head() -> str:\n    with open('x') as handle:\n        return handle.read()\n"
    root = repo(make_repo, {"app/mod.py": source}, DOOR)
    assert [d.message for d in diagnostics(root, "impure-call")] == [
        "impure call `open` in strict module"
    ]


# meiva:note
# A listing given no path reads the working directory, which is the `cwd` kind and an acquisition on
# its own line: the `filesystem` root must not swallow it, or the declaration would license a read it
# does not name (`meizel/graph/licensing.py` excludes the family for this reason). All three codes
# stay: the read of the directory, the call the purity rule charges, and the declaration the file no
# longer needs.
def test_a_filesystem_root_does_not_license_the_working_directory_a_listing_reads(
    make_repo: RepoFactory,
) -> None:
    source = "import os\n\ndef names() -> list[str]:\n    return os.listdir()\n"
    root = repo(make_repo, {"app/mod.py": source}, DOOR)
    assert {d.code.value for d in analysis_of(root).verdict} == {
        "ambient-read",
        "impure-call",
        "stale-root",
    }


# meiva:note
# The kind a root does not declare is not licensed by one it does: the exemption is the kind's, and a
# `cwd` declaration buys nothing for a file read.
def test_another_kind_does_not_license_a_filesystem_read(make_repo: RepoFactory) -> None:
    manifest = (
        '[purity]\n"mod.py" = "strict"\n'
        '[roots]\n"mod.py" = { kinds = ["cwd"], why = "the working directory" }\n'
    )
    root = repo(make_repo, {"app/mod.py": READ}, manifest)
    assert [d.code.value for d in diagnostics(root, "impure-call")] == ["impure-call"]


# meiva:note
# The same read recorded twice, filesystem face: the root licenses the read and `[debt.impure-call]`
# charges the same call again, so the purity rule leaves it unrecorded and reports the conflict
# against the entry `debt prune` drops. A counting correction, not a repayment: the code still reads.
def test_a_filesystem_read_a_root_licenses_and_debt_pays_for_is_a_conflict(
    make_repo: RepoFactory,
) -> None:
    manifest = (
        '[purity]\n"mod.py" = "strict-to-be"\n'
        '[roots]\n"mod.py" = { kinds = ["filesystem"], why = "the source tree door" }\n'
        '\n[debt.impure-call]\n"mod.py#Path.read_bytes" = 1\n'
    )
    root = repo(make_repo, {"app/mod.py": READ}, manifest)
    assert [d.code.value for d in diagnostics(root, "impure-call")] == []
    assert ("legal-read-recorded", "app/meizel.toml") in failures(root)


# meiva:note
# A declaration the code no longer needs: no acquisition records a read of a file, so `stale-root`
# has to read the purity items, or a root of this kind is stale the moment it is written.
def test_a_filesystem_root_that_reads_no_file_is_stale(make_repo: RepoFactory) -> None:
    root = repo(make_repo, {"app/mod.py": "def one() -> int:\n    return 1\n"}, DOOR)
    assert [d.code.value for d in diagnostics(root, "stale-root")] == ["stale-root"]


# meiva:note
# The reads the kind licenses, written out here by hand rather than derived from the table: adding or
# dropping a key fails until this list moves with it, which is the review the table needs -- a key
# that stops being charged is a permission over code nobody reads any more.
_READS = frozenset(
    {
        "Path.read_text",
        "Path.read_bytes",
        "Path.exists",
        "Path.is_file",
        "Path.is_dir",
        "Path.is_symlink",
        "Path.samefile",
        "Path.stat",
        "Path.lstat",
        "Path.readlink",
        "Path.iterdir",
        "Path.glob",
        "Path.rglob",
        "Path.resolve",
        "glob.glob",
        "glob.iglob",
        "inspect.getsource",
        "inspect.getsourcefile",
        "inspect.getsourcelines",
        "os.access",
        "os.lstat",
        "os.readlink",
        "os.stat",
        "os.statvfs",
        "os.walk",
        "os.path.exists",
        "os.path.getatime",
        "os.path.getctime",
        "os.path.getmtime",
        "os.path.getsize",
        "os.path.isdir",
        "os.path.isfile",
        "os.path.islink",
        "os.path.ismount",
        "os.path.lexists",
        "os.path.realpath",
        "os.path.samefile",
        "pkgutil.get_data",
    }
)


def test_the_table_licenses_exactly_the_reads_written_out_here() -> None:
    assert FILESYSTEM_READS == _READS


# meiva:note
# What the kind must never reach: the writes, renames and unlinks of the same door, a call on a
# descriptor, an `open` whose mode decides, and a listing that reads the working directory when it is
# given no path. Each is an effect or another kind, and licensing one would widen the declaration
# past what it says.
_EFFECTS = frozenset(
    {
        "Path.chmod",
        "Path.hardlink_to",
        "Path.lchmod",
        "Path.mkdir",
        "Path.open",
        "Path.rename",
        "Path.replace",
        "Path.rmdir",
        "Path.symlink_to",
        "Path.touch",
        "Path.unlink",
        "Path.write_bytes",
        "Path.write_text",
        "open",
        "os.chmod",
        "os.close",
        "os.fdopen",
        "os.fstat",
        "os.fwalk",
        "os.listdir",
        "os.mkdir",
        "os.makedirs",
        "os.open",
        "os.read",
        "os.remove",
        "os.rename",
        "os.replace",
        "os.rmdir",
        "os.scandir",
        "os.unlink",
        "os.write",
    }
)


def test_no_effect_is_among_the_reads() -> None:
    assert not FILESYSTEM_READS & _EFFECTS


# meiva:note
# Every call of a module the table licenses is one the purity rule charges by that spelling; a name it
# does not charge would license nothing and read like a permission that works.
def test_every_licensed_module_call_is_one_the_purity_rule_charges() -> None:
    licensed_calls = {key for key in FILESYSTEM_READS if not key.startswith("Path.")}
    assert licensed_calls <= DEFAULT_IMPURE_CALLS


# meiva:note
# The policy's reader table accepts the kind like every other one: a repository function that reads
# files and hands them on is listed under `[ambient-readers.filesystem]`, and a call to it is a read
# of that kind at the call site. The kind is therefore reachable as an acquisition too, which is what
# the diagnostic's own vocabulary has to describe.
def test_a_repository_reader_listed_under_the_kind_is_a_read(make_repo: RepoFactory) -> None:
    readers = (
        '[ambient-readers.filesystem]\n"app.source.read_source()" = "reads the tree it judges"\n'
    )
    root = make_repo(
        {
            "meizel.toml": ROOT,
            "meizel.policy.toml": render_policy({}, {}, {}) + "\n" + readers,
            "app/__init__.py": "",
            "app/source.py": READ,
            "app/mod.py": (
                "from app.source import read_source\n\n"
                "def load() -> bytes:\n    return read_source()\n"
            ),
        }
    )
    scan = analysis_of(root).scans["app/mod.py"]
    assert [(a.kind, a.target) for a in scan.ambient] == [("filesystem", "app.source.read_source")]
    assert [d.message for d in diagnostics(root, "ambient-read")] == [
        "reads the contents of files (`app.source.read_source`) outside a composition root"
    ]
