import hashlib
import os
import stat
from html.parser import HTMLParser
from pathlib import Path
from typing import override
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from mdit_py_plugins.footnote import footnote_plugin

from base.agents.to_user.errors import ToUserFileTooLarge
from base.agents.to_user.limits import ToUserLimits
from base.agents.to_user.models import DOCUMENT_FILENAME, ToUserResource


class DocumentMarkdown(MarkdownIt):
    @override
    def validateLink(self, url: str) -> bool:
        return urlsplit(url).scheme == "file" or super().validateLink(url)


def local_target(document: Path, href: str) -> Path | None:

    url = urlsplit(href)
    if url.scheme not in ("", "file") or url.netloc not in ("", "localhost"):
        return None
    if not url.path or href.startswith("//"):
        return None
    value = unquote(url.path)
    if "\x00" in value:
        raise ValueError("A document reference contains a null byte")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else document.parent / path).resolve()


class _RawHtmlReferences(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attribute = "src" if tag == "img" else "href" if tag == "a" else None
        if attribute is None:
            return
        for name, value in attrs:
            if name == attribute and value:
                self.hrefs.append(value)
                return


def referenced_files(document: Path, content: str, *, limits: ToUserLimits) -> dict[str, Path]:

    parser = DocumentMarkdown("commonmark", {"html": True}).enable("table").use(footnote_plugin)
    max_references = limits.references
    pending = list(parser.parse(content))
    references: dict[str, Path] = {}

    def add(href: str) -> None:
        path = local_target(document, href)
        if path is None:
            return
        references[href] = path
        if len(references) > max_references:
            raise ValueError(f"To user exceeds the {max_references} local reference limit")

    while pending:
        token = pending.pop()
        pending.extend(token.children or [])
        if token.type in ("html_inline", "html_block"):
            scanner = _RawHtmlReferences()
            scanner.feed(token.content)
            for href in scanner.hrefs:
                add(href)
            continue
        href = token.attrGet("href") if token.type == "link_open" else token.attrGet("src")
        if isinstance(href, str):
            add(href)
    return references


# meiva:note
# One file, one readable name: its path from the workspace root, or absolute when the document
# reaches outside it. Only the second form starts with `/`, so the two cannot collide, and one
# file keeps one name however its link was spelled (`./plot.png`, `plot.png` and `../w/plot.png`
# agree).
def resource_path(workspace: Path, target: Path) -> str:
    try:
        return target.relative_to(workspace).as_posix()
    except ValueError:
        return target.as_posix()


# meiva:note
# The digest a link used to be keyed by. A panel URL written before 2026-11-07 carries it, and a
# shared link outlives the spelling of the address bar, so a chain element still matches it.
def reference_id(href: str) -> str:
    return hashlib.sha256(href.encode()).hexdigest()


def resource_descriptions(
    document: Path, content: str, *, workspace: Path, limits: ToUserLimits
) -> dict[str, ToUserResource]:
    result: dict[str, ToUserResource] = {}
    for href, path in referenced_files(document, content, limits=limits).items():
        try:
            info = path.stat()
            stamp = (
                f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}"
            )
            version = hashlib.sha256(stamp.encode()).hexdigest()
        except OSError:
            version = None
        result[href] = ToUserResource(
            file=resource_path(workspace, path),
            name=path.name,
            markdown=path.suffix.lower() in (".md", ".markdown"),
            version=version,
        )
    return result


def read_regular_file(path: Path, limit: int) -> tuple[bytes, os.stat_result]:

    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("To user references must be regular files")
        if info.st_size > limit:
            raise ToUserFileTooLarge(f"To user file exceeds the {limit} byte limit")
        raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ToUserFileTooLarge(f"To user file exceeds the {limit} byte limit")
    return raw, info


def resolve_file(workspace: Path, files: list[str], *, limits: ToUserLimits) -> Path:

    if workspace.is_symlink():
        raise ValueError("To user workspace must not be a symlink")
    max_depth = limits.chain_depth
    if not files or len(files) > max_depth:
        raise ValueError(f"A resource requires between one and {max_depth} file names")
    path = workspace / DOCUMENT_FILENAME
    for name in files:
        if path.suffix.lower() not in (".md", ".markdown"):
            raise ValueError("Only Markdown documents can link further resources")
        raw, _ = read_regular_file(path, limits.document_bytes)
        targets = referenced_files(path, raw.decode("utf-8"), limits=limits)
        matches = [
            target
            for href, target in targets.items()
            if resource_path(workspace, target) == name or reference_id(href) == name
        ]
        if not matches:
            raise FileNotFoundError("The document no longer references this resource")
        path = matches[0]
    return path


def read_resource(workspace: Path, files: list[str], *, limits: ToUserLimits) -> bytes:
    path = resolve_file(workspace, files, limits=limits)
    return read_regular_file(path, limits.resource_bytes)[0]
