# meiva:note
# The refusal of a runtime-config write whose `.env` changed after the patch was validated.
#
# `runtime_config.write_fields` raises it when the digest the caller validated against no
# longer matches the file; the gateway answers it with a retryable 409 and the host's config
# op with a per-field "changed concurrently" verdict. One type, so no caller compares messages.

from __future__ import annotations

__all__ = ["EnvChangedError"]


# meiva:note
# The `.env` changed between validation and write; the write was refused and the caller may
# revalidate and retry.
class EnvChangedError(RuntimeError):
    def __init__(self) -> None:
        super().__init__(".env changed before owned runtime-config write")
