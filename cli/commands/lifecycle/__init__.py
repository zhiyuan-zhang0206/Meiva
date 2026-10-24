# meiva:note
# The local lifecycle commands: `start`, `stop` and `status`. `_exclusive_resources`
# is private to this directory (it is plumbing, not a command), so the lock decorator
# it defines is re-exported here: a sibling component that drives a start
# imports this door rather than reaching into an underscore module.

from cli.commands.lifecycle._exclusive_resources import exclusive_resources

__all__ = ["exclusive_resources"]
