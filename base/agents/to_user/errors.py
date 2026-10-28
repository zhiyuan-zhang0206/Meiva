from enum import StrEnum


# meiva:note
# A refused panel read is told apart by what a reader can do next, and only that
# difference earns a class: `gone` and `unreadable` answer the same way for the
# same document version, so nothing retries them, while `too_large` names a byte
# budget the file exceeds and ends when the file shrinks.
# meiva:link gateway/agents/docs/to-user-read-failures.meiva.okf.md
class ToUserReadFailure(StrEnum):
    GONE = "gone"
    TOO_LARGE = "too_large"
    UNREADABLE = "unreadable"


# meiva:note
# The one failure a reader distinguishes by type rather than by message: a byte
# budget, not a filesystem answer. Every other OSError and ValueError a read raises
# is classified by its own kind, so a raise site only names this class where the
# budget is the reason. The name states the state, not an error class, because the
# panel tells the reader what it could not show.
# meiva:link gateway/agents/docs/to-user-read-failures.meiva.okf.md
class ToUserFileTooLarge(ValueError):  # noqa: N818
    pass


# meiva:link gateway/agents/docs/to-user-read-failures.meiva.okf.md
def read_failure(exc: OSError | ValueError) -> ToUserReadFailure:
    if isinstance(exc, ToUserFileTooLarge):
        return ToUserReadFailure.TOO_LARGE
    if isinstance(exc, FileNotFoundError):
        return ToUserReadFailure.GONE
    return ToUserReadFailure.UNREADABLE
