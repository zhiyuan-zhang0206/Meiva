# meiva:note
# Which of a composition root's kinds licenses one purity item: the join between a `[roots]`
# declaration and the call the purity layer charged.
#
# A call is licensed in one of three ways. On its line: the scan records an acquisition of a
# licensed kind on the line the purity rule recorded the call on, which is the same call in
# whatever language judged it. By its name: the ambient table judges the recorded call a reader of a
# licensed kind, and the call is unconditional -- one with an `unless` clause reads nothing once it
# is handed the value. By the filesystem: a read of a file derives no acquisition (the purity
# layer charges it `impure-call`, see `AmbientKind`), so the kind licenses the call by its name,
# and `FILESYSTEM_READS` is the whole of what a declaration of it reaches.

from __future__ import annotations

from collections.abc import Iterable

from meizel.graph.ambient_surface import judged
from meizel.graph.model import Acquisition, AmbientKind, KindName, PurityItem

__all__ = ["FILESYSTEM_READS", "licensed"]

# meiva:note
# The calls a `filesystem` root licenses: the ones whose name decides that it reads. A read of a
# file is an effect in the purity layer, whose keys these are, so this table is the one place the
# `filesystem` kind reaches; a new key belongs here only with a call that layer charges, and the
# table's test pins the set: the module calls against `DEFAULT_IMPURE_CALLS`, every key by hand.
#
# Three families stay out, and each is a boundary rather than an omission. An `open` whose mode
# decides whether it writes (`open`, `builtins.open`, `io.open`, `Path.open`) is an effect the name
# cannot tell from a read. A call on a descriptor (`os.open`, `os.fdopen`, `os.read`, `os.fstat`,
# `os.fwalk`) reads whatever the descriptor is, not a path. A listing given no path (`os.listdir`,
# `os.scandir`) reads the working directory, which is the `cwd` kind and is recorded as an
# acquisition on its line. Every write, rename, unlink and chmod is an effect of the same door and
# stays `impure-call` under any root.
#
# `resolve` and `os.path.realpath` are in it: every one of these reads anchors a relative path in
# the working directory, which the purity layer does not separate out, so a `filesystem` root reads
# the working directory as far as the paths it is handed reach -- the same reach every read here has.
FILESYSTEM_READS: frozenset[str] = frozenset(
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


# meiva:note
# The purity rule keeps the short spelling a call canonicalises to (`datetime.now` for the table's
# `datetime.datetime.now`), so the table's own spelling is a second candidate -- the same name, not
# a second rule -- and a call reached only when given no value reads nothing once it is handed the
# value, so an `unless` clause names no kind here. The answer is the kinds that license the item,
# empty when the declaration does not reach it.
def licensed(
    kinds: frozenset[KindName], acquisitions: Iterable[Acquisition], item: PurityItem
) -> frozenset[KindName]:
    on_line = frozenset(
        acquisition.kind
        for acquisition in acquisitions
        if acquisition.line == item.line and acquisition.kind in kinds
    )
    if on_line or "#" in item.key:
        return on_line
    if item.key in FILESYSTEM_READS:
        return frozenset({AmbientKind.FILESYSTEM}) & kinds
    reading = judged(item.key)
    if reading is None and "." in item.key:
        head, _, _ = item.key.partition(".")
        reading = judged(f"{head}.{item.key}")
    if reading is None or reading.judgment.unless is not None:
        return frozenset()
    return frozenset(reading.judgment.kinds) & kinds
