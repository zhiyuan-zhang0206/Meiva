import ctypes
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TypeGuard

RESULT_WORDS = {0: "granted", 1: "denied", 2: "not-determined"}

CORE_FOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
TCC_FRAMEWORK = "/System/Library/PrivateFrameworks/TCC.framework/Versions/A/TCC"


# meiva:note
# What a foreign function or a decoder answers, read as `object`: `ctypes` and `json` type their
# answers `Any`, and a parameter typed `Callable[..., object]` takes the function without naming it.
# This file is a standalone child process, so it carries its own copy instead of importing the
# repository's `object_of`.
def _answer(function: Callable[..., object], *args: object) -> object:
    return function(*args)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_string_list(value: object) -> TypeGuard[list[str]]:
    return _is_object_list(value) and all(isinstance(item, str) for item in value)


class Libraries:
    def __init__(self) -> None:
        self.core_foundation = ctypes.CDLL(CORE_FOUNDATION)
        self.core_foundation.CFStringCreateWithCString.restype = ctypes.c_void_p
        self.core_foundation.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        self.tcc = ctypes.CDLL(TCC_FRAMEWORK)
        self.tcc.TCCAccessPreflight.restype = ctypes.c_int
        self.tcc.TCCAccessPreflight.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    def preflight(self, service: str) -> int:
        cf_service = _answer(
            self.core_foundation.CFStringCreateWithCString, None, service.encode(), 0x08000100
        )
        if not isinstance(cf_service, int):
            raise TypeError("CFStringCreateWithCString did not answer a pointer")
        code = _answer(self.tcc.TCCAccessPreflight, ctypes.c_void_p(cf_service), None)
        if not isinstance(code, int):
            raise TypeError("TCCAccessPreflight did not answer an integer")
        return code


def main() -> None:
    libraries = Libraries()
    services = _answer(json.loads, sys.argv[1])
    if not _is_string_list(services):
        raise TypeError("the services argument must be a JSON list of strings")
    results: dict[str, dict[str, object]] = {}
    for service in services:
        code = libraries.preflight(service)
        results[service] = {"code": code, "result": RESULT_WORDS.get(code, "unknown")}
    with Path(sys.argv[2]).open("w") as handle:
        json.dump(results, handle, indent=2)


if __name__ == "__main__":
    main()
