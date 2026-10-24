# meiva:note
# Commit in a fresh process so each request owns a genuinely different timezone snapshot.
import json
import sys
from dataclasses import asdict

import meiva
from meiva_builtins.plugins.meiva_fleet import task_registry

parent, default_interval = int(sys.argv[1]), int(sys.argv[2])
key = sys.argv[3]
try:
    result = task_registry.commit_creation(
        meiva.context,
        f"timezone {key}",
        "work",
        parent=parent,
        operation_key=key,
        default_remind_interval_seconds=default_interval,
    )
    print(json.dumps({"task": asdict(result.task), "publish": result.publication is not None}))
finally:
    meiva.context.clients.close()
