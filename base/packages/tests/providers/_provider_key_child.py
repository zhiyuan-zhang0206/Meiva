# meiva:note
# Load normally, then corrupt only the fallback file before the process reads its provider keys.

import sys
from pathlib import Path

from base.lm.factory import process_provider_keys
from base.packages.plugin_providers import build_model_catalog
from base.paths import meiva_home


def main(home: Path, keys_all_live: bool) -> None:
    catalog = build_model_catalog()
    assert meiva_home() == home
    (home / ".env").write_bytes(b"DEEPSEEK_API_KEY=\xff\n")
    try:
        keys = process_provider_keys(catalog)
    except UnicodeDecodeError:
        assert not keys_all_live, "live keys must bypass the malformed fallback"
    else:
        assert keys_all_live, "an absent key must read the fallback"
        assert all(keys.present(name) for name in catalog.key_envs)


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2] == "present")
