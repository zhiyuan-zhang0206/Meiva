from __future__ import annotations

import json
import sys
from pathlib import Path

source = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(source))
case = sys.argv[2]
if case == "ownership":
    from base.native_process import ownership as subject
elif case == "loaded-commit":
    from base.native_process import loaded_commit as subject
else:
    raise ValueError(f"Unknown native identity import case: {case!r}")
assert (
    subject.__name__
    == {
        "ownership": "base.native_process.ownership",
        "loaded-commit": "base.native_process.loaded_commit",
    }[case]
)

origins: dict[str, str] = {}
for name, module in tuple(sys.modules.items()):
    if name == "base" or name.startswith("base."):
        assert module.__file__ is not None, name
        origin = Path(module.__file__).resolve()
        assert origin.is_relative_to(source), (name, origin, source)
        assert module.__spec__ is not None and module.__spec__.origin is not None, name
        assert Path(module.__spec__.origin).resolve() == origin, name
        origins[name] = str(origin)

package = sys.modules["base.native_process"]
sys.stdout.write(
    json.dumps(
        {
            "origins": origins,
            "pydantic": any(
                name == "pydantic" or name.startswith("pydantic.") for name in sys.modules
            ),
            "kernel": "base.native_process.kernel_identity" in sys.modules,
            "legacy_exports": [
                name
                for name in ("pid_starttime_ticks", "native_boot_id", "process_birth_key")
                if hasattr(package, name)
            ],
        }
    )
    + "\n"
)
