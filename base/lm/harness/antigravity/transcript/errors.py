from __future__ import annotations

# meiva:note
# A value of a known field the Antigravity record reader has no reading for; turned into an `Anomaly`
# for the row rather than leaving the reader.


class UnrecognisedError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail
