from __future__ import annotations

from meizel.tests.synthetic import RepoFactory, diagnostics


def test_wide_public_surface_treats_a_subclass_of_a_same_module_data_class_as_data(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/a.py": (
                "from pydantic import BaseModel\n\n"
                "class _Base(BaseModel):\n    agent_id: int\n\n"
                "class One(_Base):\n    x: int\n\n"
                "class Two(One):\n    y: int\n\n"
                "class Three(_Base):\n    z: int\n"
            ),
            "pkg/meizel.toml": 'purity = "strict-to-be"\n',
            "meizel.policy.toml": '[purity]\nmax-public-symbols = ["2"]\n',
        }
    )
    assert diagnostics(root, "wide-public-surface") == []
