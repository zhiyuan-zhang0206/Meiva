# meiva:note
# Load normally, then corrupt only the fallback file before provider validation.

import sys
from pathlib import Path

from base.lm.factory import validate_model_config
from base.lm.provider_api import provider_key_present
from base.packages.plugin_providers import build_model_catalog
from base.paths import meiva_home


def main(home: Path, key_present: bool) -> None:
    catalog = build_model_catalog()
    assert meiva_home() == home
    assert provider_key_present("DEEPSEEK_API_KEY") is key_present
    (home / ".env").write_bytes(b"DEEPSEEK_API_KEY=\xff\n")
    try:
        model = validate_model_config(model="deepseek-flash", catalog=catalog, llm_override="")
    except UnicodeDecodeError:
        assert not key_present, "a live key must bypass the malformed fallback"
    else:
        assert key_present, "a missing live key must read the fallback"
        assert model == "deepseek-flash"


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2] == "present")
