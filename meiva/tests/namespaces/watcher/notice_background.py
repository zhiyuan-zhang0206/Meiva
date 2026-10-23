from dataclasses import dataclass
from pathlib import Path

from meiva.shell import background


@dataclass(frozen=True)
class NoticeBackground:
    cli: Path
    log_dir: Path | None = None

    def validate_notify(self, notify: str | None) -> str | None:
        return background.validate_notify(notify)

    def allocate_output_path(self, session_id: int, name: str) -> Path:
        if self.log_dir is None:
            return background.allocate_output_path(session_id, name)
        path = self.log_dir / f"{session_id}_{name}.log"
        path.touch()
        return path

    def notified_line(
        self,
        cmd: str,
        *,
        agent_id: int,
        label: str,
        source: str,
        output_path: Path,
        keep: bool,
        notify: str | None,
    ) -> str:
        return background.notified_line(
            cmd,
            agent_id=agent_id,
            label=label,
            source=source,
            output_path=output_path,
            keep=keep,
            notify=notify,
            cli=self.cli,
        )

    def write_launch_file(self, line: str, path: Path) -> str:
        return background.write_launch_file(line, path)
