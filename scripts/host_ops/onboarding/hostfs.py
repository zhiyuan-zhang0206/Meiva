from __future__ import annotations

import base64
import json
import os
import re
import shutil
import stat as stat_module
import sys
import time
from pathlib import Path

_DEFAULTED = re.compile(r"\$\{(\w+):-([^}]*)\}")


# meiva:note
# This program runs on the machine being operated on, not on the operator's: a
# credential path means nothing anywhere else. The request arrives as a literal
# appended to this source, so no secret reaches an argv (visible in `ps`) or a
# temporary file, and one request is one process.
# meiva:link scripts/host_ops/docs/harness-onboarding.meiva.okf.md
# meiva:note
# A credential path names variables this machine's shell would expand, and this
# program runs on the target rather than inside the application, so there is no
# settings owner to route through.
# meiva:link scripts/host_ops/docs/harness-onboarding.meiva.okf.md
def _env_value(name: str, fallback: str) -> str:
    return os.environ.get(name, fallback)


def resolve(raw: str) -> Path:
    with_default = _DEFAULTED.sub(lambda match: _env_value(match.group(1), match.group(2)), raw)
    return Path(os.path.expandvars(with_default)).expanduser()


def describe(raw: str) -> dict[str, object]:
    path = resolve(raw)
    try:
        info = path.stat()
    except OSError:
        return {"raw": raw, "path": str(path), "exists": False}
    return {
        "raw": raw,
        "path": str(path),
        "exists": True,
        "is_dir": path.is_dir(),
        "mode": format(stat_module.S_IMODE(info.st_mode), "o"),
        "size": info.st_size,
        "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(info.st_mtime)),
    }


def op_stat(request: dict[str, object]) -> dict[str, object]:
    return {"files": [describe(str(raw)) for raw in _paths(request)]}


def op_read(request: dict[str, object]) -> dict[str, object]:
    path = resolve(_one_path(request))
    data = path.read_bytes()
    info = path.stat()
    return {
        "path": str(path),
        "mode": format(stat_module.S_IMODE(info.st_mode), "o"),
        "b64": base64.b64encode(data).decode("ascii"),
    }


def op_write(request: dict[str, object]) -> dict[str, object]:
    path = resolve(_one_path(request))
    path.parent.mkdir(parents=True, exist_ok=True)
    data = base64.b64decode(str(request["b64"]))
    path.write_bytes(data)
    path.chmod(int(str(request.get("mode", "600")), 8))
    return {"path": str(path), "bytes": len(data)}


def op_backup(request: dict[str, object]) -> dict[str, object]:
    path = resolve(_one_path(request))
    if not path.exists():
        return {"backed_up": False, "path": str(path)}
    destination = resolve(str(request["dir"]))
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / path.name
    if target.exists():
        target = destination / f"{path.name}.{time.strftime('%H%M%S')}"
    shutil.copy2(path, target)
    target.chmod(0o600)
    return {"backed_up": True, "source": str(path), "backup": str(target)}


def op_append(request: dict[str, object]) -> dict[str, object]:
    path = resolve(_one_path(request))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(request["record"], sort_keys=True) + "\n")
    return {"path": str(path)}


def _paths(request: dict[str, object]) -> list[object]:
    paths = request.get("paths")
    if not isinstance(paths, list):
        raise TypeError("request is missing a 'paths' list")
    return paths


def _one_path(request: dict[str, object]) -> str:
    return str(request["path"])


OPERATIONS = {
    "stat": op_stat,
    "read": op_read,
    "write": op_write,
    "backup": op_backup,
    "append": op_append,
}


def main() -> int:
    request = globals().get("__REQUEST__")
    if not isinstance(request, dict):
        print(json.dumps({"error": "no __REQUEST__ literal was appended"}))
        return 1
    operation = OPERATIONS.get(str(request.get("op")))
    if operation is None:
        print(json.dumps({"error": f"unknown op {request.get('op')!r}"}))
        return 1
    try:
        print(json.dumps(operation(request)))
    except OSError as error:
        print(json.dumps({"error": f"{type(error).__name__}: {error}"}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
