from __future__ import annotations

# meiva:note
# What a parser raises, internally only, when a row lacks what the projection needs: `MissingFieldError`
# for an absent or mistyped field and `UnknownValueError` for an enum value the parser does not know.
# Every public parse function catches both and returns an `Anomaly` entry instead, so no raw row can
# make a parser raise.
# meiva:link base/lm/harness/docs/transcript-projection.md


class MissingFieldError(Exception):
    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


class UnknownValueError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail
