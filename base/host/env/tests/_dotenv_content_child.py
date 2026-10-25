# meiva:note
# Import the content door before any test harness can preload its resource shell.

from __future__ import annotations

import json
import sys

from base.host.env import dotenv_content

for name in (
    "base.config",
    "base.host.env.dotenv_file",
    "base.host.private_storage",
    "base.native_process",
):
    assert not any(module == name or module.startswith(name + ".") for module in sys.modules), name
assert dotenv_content.render_env_upsert(["A=old"], {"A": "new"}) == b"A=new\n"
sys.stdout.write(json.dumps({"source": dotenv_content.__file__}) + "\n")
