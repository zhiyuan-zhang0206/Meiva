from __future__ import annotations

from base.native_process.child_env import filter_daemon_env


def test_daemon_env_keeps_the_process_essentials() -> None:
    essentials = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/operator",
        "TMPDIR": "/home/operator/tmp",
        "TZ": "UTC",
        "LANG": "C.UTF-8",
    }
    env = filter_daemon_env(
        {
            **essentials,
            "MEIVA_API_TOKEN": "gateway-api-token-" + "t" * 32,
        }
    )

    assert {name: env[name] for name in essentials} == essentials
    assert "MEIVA_API_TOKEN" not in env
