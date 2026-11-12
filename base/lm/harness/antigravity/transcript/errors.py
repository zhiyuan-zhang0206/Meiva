from __future__ import annotations

# meiva:note
# The two ways a row can fail the parser. `MissingError` is a field the projection needs that is absent
# or of the wrong type; `UnrecognisedError` is a value of a known field the parser has no reading for.
# The parser turns each into one `Anomaly` for the row and goes on; neither leaves the parser.


class MissingError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class UnrecognisedError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail
