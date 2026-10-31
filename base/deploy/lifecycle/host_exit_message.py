# meiva:note
# Host-local exit message staged for `agent-host`'s graceful SIGTERM drain.
#
# `meiva stop`, `meiva restart` and `agent-host`'s own plugin-change watcher write (or clear)
# `run/host-exit-message.json` under the home before signalling `agent-host`. `GracefulExit`
# consumes and unlinks the record once at the start of its drain so the explanation reaches
# interrupted turns and idle agents on that machine without leaking into a later stop.
# meiva:link services/agent_runner/agent_host/lifecycle/docs/graceful-exit.meiva.okf.md

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TypeGuard

from base.host.atomic_io import SYSTEM_FS, AtomicWriter
from base.host.verified_file.reader import regular_bytes
from base.native_process.os_platform import file_lock
from base.paths import run_dir

__all__ = [
    "HOST_RESTART_SOURCE",
    "PLUGINS_RESTART_MESSAGE",
    "clear_host_exit_message",
    "consume_host_exit_message",
    "message_path",
    "write_host_exit_message",
]

_log = logging.getLogger("base.deploy.lifecycle.host_exit_message")
_MESSAGE_FILENAME = "host-exit-message.json"
_LOCK_FILENAME = "host-exit-message.lock"
_WRITER = AtomicWriter(SYSTEM_FS)

HOST_RESTART_SOURCE = "system:restart"
PLUGINS_RESTART_MESSAGE = "external plugins changed under $MEIVA_HOME/plugins"


def _is_str_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def message_path(home: Path) -> Path:
    return run_dir(home) / _MESSAGE_FILENAME


def _lock_path(home: Path) -> Path:
    return run_dir(home) / _LOCK_FILENAME


def write_host_exit_message(home: Path, message: str | None) -> None:
    if message is not None and not message.strip():
        raise ValueError("host exit message cannot be empty; pass None to clear")
    target = message_path(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(_lock_path(home)):
        if message is None:
            target.unlink(missing_ok=True)
            return
        body = json.dumps({"message": message.strip()}, ensure_ascii=False)
        _WRITER.write_text(target, body, encoding="utf-8", mode=0o600, prefix=".host-exit-message-")


def clear_host_exit_message(home: Path) -> None:
    write_host_exit_message(home, None)


def consume_host_exit_message(home: Path) -> str | None:
    target = message_path(home)
    if not target.exists():
        return None
    with file_lock(_lock_path(home)):
        try:
            raw = regular_bytes(target)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            _log.warning("[host-exit-message] discarding unreadable exit message: %s", exc)
            target.unlink(missing_ok=True)
            return None
        target.unlink(missing_ok=True)
    try:
        loaded: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        _log.warning("[host-exit-message] discarding malformed exit message: %s", exc)
        return None
    if not _is_str_dict(loaded):
        return None
    value = loaded.get("message")
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
