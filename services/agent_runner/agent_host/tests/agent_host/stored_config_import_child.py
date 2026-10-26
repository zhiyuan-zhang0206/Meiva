# meiva:note
# Import the production wake reader in a fresh interpreter so earlier host tests
# cannot hide whether its row model initializes runtime construction.
from __future__ import annotations

import json
import sys

from services.agent_runner.agent_host.scheduling import wake_screening
from services.agent_runner.agent_host.scheduling.stored_config import StoredConfig

observers = (
    "services.agent_runner.agent_host.runtime",
    "agent.process_boot",
    "base.lm.harness.wiring",
    "langchain_core.language_models.chat_models",
)
sys.stdout.write(
    json.dumps(
        {
            "same_identity": wake_screening.StoredConfig is StoredConfig,
            "loaded_observers": [name for name in observers if name in sys.modules],
        }
    )
    + "\n"
)
