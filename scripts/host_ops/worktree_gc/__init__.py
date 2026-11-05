# meiva:note
# The door this package exports: one entry point, and the settings it runs on. Everything else in
# here is internal to a round.

from .cli import main
from .contracts import Settings

__all__ = ["Settings", "main"]
