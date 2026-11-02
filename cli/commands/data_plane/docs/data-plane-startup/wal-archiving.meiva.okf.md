---
type: doc
title: WAL archiving launch arguments
description: How `MEIVA_WALG_CONFIG_FILE` reaches the postmaster's launch arguments, and when the archive settings take effect.
tags:
- cli
- postgres
- walg
---

# WAL archiving launch arguments

When `MEIVA_WALG_CONFIG_FILE` is set (`DataPlaneConfig.walg_config_file`), `start_pg` appends `archive_mode`,
`archive_timeout` and `archive_command` (`services.backup.walg.archive.archive_pg_args`)
to the postmaster's `-c` list; unset adds nothing. The settings live only in the
launch arguments, and a retained postmaster is reloaded, not relaunched, so they
take effect at the next new launch. After Postgres is ready, `warn_archive_inactive`
prints a warning when the running Postgres reports other archive settings
([[services/backup/walg/docs/walg.meiva.okf.md|WAL-G]]).
