# meiva:note
# Every input follows the same CLI normalization, nested normalization and real authority pass.

import json
import os

from base.host.env import dotenv_boot
from base.paths.home import resolve_meiva_home
from cli.main import normalize_process_profile


def main() -> None:
    db_url = os.environ["MEIVA_DB_URL"]
    redis_url = os.environ["MEIVA_REDIS_URL"]
    normalize_process_profile()
    profile = os.environ.get("MEIVA_PROCESS_PROFILE")
    recorded = os.environ.get(dotenv_boot.LAUNCHER_PROFILE_ENV_KEY)
    normalize_process_profile()
    print(
        json.dumps(
            {
                "profile": profile,
                "recorded": recorded,
                "nested_profile": os.environ.get("MEIVA_PROCESS_PROFILE"),
                "nested_recorded": os.environ.get(dotenv_boot.LAUNCHER_PROFILE_ENV_KEY),
            }
        ),
        flush=True,
    )
    dotenv_boot.enforce_cluster_env_authority(os.environ, resolve_meiva_home())
    print(
        json.dumps(
            {
                "db_present": "MEIVA_DB_URL" in os.environ,
                "db_kept": os.environ.get("MEIVA_DB_URL") == db_url,
                "redis_present": "MEIVA_REDIS_URL" in os.environ,
                "redis_kept": os.environ.get("MEIVA_REDIS_URL") == redis_url,
                "app_port_present": "MEIVA_APP_PORT" in os.environ,
            }
        )
    )


if __name__ == "__main__":
    main()
