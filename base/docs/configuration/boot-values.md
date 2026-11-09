# Config boot takes values

The boot reads nothing on its own. `base/host/env/bootstrap.py` and `dotenv_boot.py` take the environment
mapping (`ConfigBoot` hands in `os.environ`), the home `Path`, the `.env` values and `now` as arguments;
the pure decisions (flag spelling, connect backoff, payload shape, fetch tuning) live in the strict
`boot_policy.py`. `base.paths.home.home_from_env(env, user_home=...)` is the pure home resolver and
`resolve_meiva_home()` its ambient door for a composition root. A test passes dicts and a `tmp_path`;
the gateway it fetches from is a real loopback listener.

## Related owners

- [Configuration and Bootstrap](configuration.md) - Settings composition and bootstrap distribution.
- [`$MEIVA_HOME` Layout](../../paths/docs/paths.md) - the home and its layout.
