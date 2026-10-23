from __future__ import annotations

from base.config import ConfigBoot
from base.packages.composer_commands import CommandSources


class CommandInputs:
    def __init__(self, config: ConfigBoot, locations: CommandSources) -> None:
        self.config = config
        self.locations = locations
        self.source_reads = 0

    def enabled(self) -> bool:
        return self.config.view.agent.commands_enabled

    def sources(self) -> CommandSources:
        self.source_reads += 1
        return self.locations
