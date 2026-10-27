from __future__ import annotations

import os
import shutil
import stat
import struct
import tempfile
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

from base.db import Database
from base.paths import meiva_home
from services.desktop.computer import mcp_daemon
from services.desktop.computer import ocr as ocr_mod
from services.desktop.computer.config import ComputerUseConfig


def computer_use_config(**overrides: Any) -> ComputerUseConfig:
    return replace(mcp_daemon.computer_use_config(), **overrides)


def short_sock_dir() -> tuple[Path, Path, Any]:
    d = Path(tempfile.mkdtemp(prefix="meiva-cmcp-", dir="/tmp"))
    return d, d / "computer-mcp.sock", shutil.rmtree


def write_png(path: str | Path, width: int, height: int) -> None:
    Path(path).write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x06\x00\x00\x00"
    )


class AuditRow(dict[str, Any]):
    @property
    def event_name(self) -> str:
        return str(self["event_type"])

    @property
    def attributes(self) -> dict[str, Any]:
        return dict(self["payload"])


class AuditLog:
    def __init__(self, db: Database) -> None:
        self._db = db

    def __hash__(self) -> int:
        return id(self)

    def _rows(self) -> list[AuditRow]:
        with self._db.connect() as conn:
            cur = conn.execute(
                "SELECT event_name, agent_id, source, target_agent_id, attributes "
                "FROM audit_events ORDER BY ts, event_uid"
            )
            return [
                AuditRow(
                    event_type=row[0],
                    agent_id=row[1],
                    source=row[2],
                    target_agent_id=row[3],
                    payload=dict(row[4]),
                )
                for row in cur.fetchall()
            ]

    def clear(self) -> None:
        with self._db.write_transaction() as conn:
            conn.execute("DELETE FROM audit_events")

    def __iter__(self) -> Iterator[AuditRow]:
        return iter(self._rows())

    def __len__(self) -> int:
        return len(self._rows())

    def __getitem__(self, index: int) -> AuditRow:
        return self._rows()[index]

    def __bool__(self) -> bool:
        return bool(self._rows())

    def __eq__(self, other: object) -> bool:
        return self._rows() == other

    def __repr__(self) -> str:
        return repr(self._rows())


class FakeOcr:
    def __init__(
        self,
        boxes: list[dict[str, float | str]] | None = None,
        *,
        error: str | None = None,
    ) -> None:
        self._binary = ocr_mod.bin_path(home=meiva_home())
        self._binary.parent.mkdir(parents=True, exist_ok=True)
        self._tsv_path = self._binary.with_name("ocr.tsv")
        self._err_path = self._binary.with_name("ocr.err")
        self._calls_path = self._binary.with_name("ocr.calls")
        for p in (self._tsv_path, self._err_path, self._calls_path):
            p.unlink(missing_ok=True)
        self._boxes: list[dict[str, float | str]] = []
        self._error: str | None = None
        self.boxes = list(boxes or [])
        self.error = error
        script = (
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$1" >> "$0.calls"\n'
            'if [ -f "$0.err" ]; then\n'
            '    cat "$0.err" >&2\n'
            "    exit 1\n"
            "fi\n"
            'cat "$0.tsv"\n'
        )
        self._binary.write_text(script)
        self._binary.chmod(self._binary.stat().st_mode | stat.S_IXUSR)
        fresh = max(time.time(), ocr_mod.SOURCE.stat().st_mtime) + 60.0
        os.utime(self._binary, (fresh, fresh))

    @property
    def boxes(self) -> list[dict[str, float | str]]:
        return self._boxes

    @boxes.setter
    def boxes(self, value: list[dict[str, float | str]]) -> None:
        self._boxes = list(value)
        lines = [
            f"{item['text']}\t{item.get('x', 0.0)}\t{item.get('y', 0.0)}\t"
            f"{item.get('w', 10.0)}\t{item.get('h', 10.0)}"
            for item in self._boxes
        ]
        self._tsv_path.write_text("\n".join(lines) + ("\n" if lines else ""))

    @property
    def error(self) -> str | None:
        return self._error

    @error.setter
    def error(self, value: str | None) -> None:
        self._error = value
        if value is None:
            self._err_path.unlink(missing_ok=True)
        else:
            self._err_path.write_text(value)

    @property
    def calls(self) -> int:
        return len(self.called_paths)

    @property
    def called_paths(self) -> list[str]:
        if not self._calls_path.exists():
            return []
        return self._calls_path.read_text().splitlines()


class FakeDesktopHelper:
    def ping(self) -> Any:
        return {
            "pong": True,
            "ax_tree_v1": True,
            "ax_act_v1": True,
            "ax_act_v2": True,
            "ax_target_v1": True,
        }

    def frontmost_app(self) -> Any:
        return {"app": "Finder"}

    def cursor_position(self) -> Any:
        return {"x": 0.0, "y": 0.0}

    def screen_size(self) -> Any:
        return {"x": 0.0, "y": 0.0, "w": 1512.0, "h": 982.0, "scale": 2.0}

    def screencapture_region(self, x: int, y: int, w: int, h: int, path: str) -> Any:
        del x, y, w, h
        write_png(path, 3024, 1964)
        return {"path": path, "bytes": 42}

    def click(
        self,
        x: float,
        y: float,
        *,
        double: bool = False,
        button: str = "left",
        click_count: int | None = None,
        modifiers: list[str] | None = None,
        duration_ms: float | None = None,
    ) -> Any:
        del button, click_count, modifiers, duration_ms
        return {"clicked": {"x": x, "y": y}, "double": double}

    def move(self, x: float, y: float, *, modifiers: list[str] | None = None) -> Any:
        del modifiers
        return {"moved": {"x": x, "y": y}}

    def drag(
        self,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        *,
        button: str | None = None,
        modifiers: list[str] | None = None,
    ) -> Any:
        del button, modifiers
        return {"start": {"x": start_x, "y": start_y}, "end": {"x": end_x, "y": end_y}}

    def mouse_down(
        self,
        x: float | None = None,
        y: float | None = None,
        *,
        button: str = "left",
        modifiers: list[str] | None = None,
    ) -> Any:
        del x, y, modifiers
        return {"held": button}

    def mouse_up(
        self,
        x: float | None = None,
        y: float | None = None,
        *,
        button: str = "left",
    ) -> Any:
        del x, y
        return {"released": button}

    def mouse_release_all(self) -> Any:
        return {"released": []}

    def type_text(self, text: str) -> Any:
        return {"typed": len(text)}

    def key(
        self,
        code: int,
        *,
        cmd: bool = False,
        modifiers: list[str] | None = None,
        duration_ms: float | None = None,
    ) -> Any:
        del modifiers, duration_ms
        return {"key": code, "cmd": cmd}

    def scroll(
        self,
        x: float,
        y: float,
        dy: int,
        *,
        dx: int | None = None,
        modifiers: list[str] | None = None,
    ) -> Any:
        del x, y, dx, modifiers
        return {"scrolled": dy}

    def clipboard_read(self) -> Any:
        return {"text": None, "types": []}

    def clipboard_write(self, text: str) -> Any:
        return {"length": len(text)}

    def indicator_stop(self) -> Any:
        return {"stopped": False}

    def list_apps(self) -> Any:
        return {"apps": []}

    def list_windows(self, app: str | None = None) -> Any:
        del app
        return {"windows": []}

    def focus_app(self, target: dict[str, object]) -> Any:
        del target
        return {"focused": True, "pid": 1}

    def screencapture_window(self, pid: int, window_id: int, path: str) -> Any:
        del pid, window_id
        write_png(path, 800, 600)
        return {
            "path": path,
            "width": 800,
            "height": 600,
            "origin": {"x": 0.0, "y": 0.0},
            "scale": 2.0,
        }

    def ax_window_info(self, app: str) -> Any:
        return {"app": app, "x": 0.0, "y": 0.0, "w": 800.0, "h": 600.0}

    def window_info(self, owner: str) -> Any:
        return {"owner": owner, "x": 0.0, "y": 0.0, "w": 800.0, "h": 600.0}

    def session_info(self) -> Any:
        return {"locked": False, "on_console": True}

    def ax_tree(
        self,
        app: str | None = None,
        *,
        target: dict[str, object] | None = None,
        scope: int | None = None,
        scope_fp: str | None = None,
        max_nodes: int = 600,
        max_depth: int = 14,
        budget_ms: int = 1500,
        timeout_ms: int = 400,
        enable_ax: bool = True,
    ) -> Any:
        del app, target, scope, scope_fp, max_nodes, max_depth, budget_ms, timeout_ms, enable_ax
        raise NotImplementedError

    def ax_act(
        self,
        app: str,
        raw_id: int,
        action: str,
        *,
        pid: int | None = None,
        value: str | None = None,
        native_action: str | None = None,
        text: str | None = None,
        prefix: str | None = None,
        suffix: str | None = None,
        selection_type: str | None = None,
        timeout_ms: int = 2000,
    ) -> Any:
        del (
            app,
            raw_id,
            action,
            pid,
            value,
            native_action,
            text,
            prefix,
            suffix,
            selection_type,
            timeout_ms,
        )
        raise NotImplementedError
