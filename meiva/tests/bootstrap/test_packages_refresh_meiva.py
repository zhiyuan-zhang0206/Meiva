"""The skill scan drops packages the host contract blocks."""

from __future__ import annotations

import json
from pathlib import Path

from base.packages.extensions import install_registry as reg
from base.paths.home import resolve_meiva_home


def _manifest_for(name: str, **extra: object) -> dict[str, object]:
    data: dict[str, object] = {"apiVersion": 2, "name": name, "version": "1.0.0"}
    data.update(extra)
    return data


def test_scan_drops_host_blocked_packages(unit_home: Path) -> None:
    from base.packages.extensions.install_registry import loadable_skill_names
    from meiva.skills.skill_index import scan_tree

    home = resolve_meiva_home()
    for name in ("ok", "blocked"):
        d = home / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n", encoding="utf-8")
        reg.register(reg.InstalledPackage(name=name, type="skill", enabled=True))
    (home / "skills" / "blocked" / "meiva-plugin.json").write_text(
        json.dumps(_manifest_for("blocked", engines={"meiva": ">=2099"})), encoding="utf-8"
    )

    tree = scan_tree(home / "skills", loadable_skill_names(), [])
    assert "ok" in tree and "blocked" not in tree
