# meiva:note
# Per-model SDK help: the profile a model reads, and the doc variants a target declares.
from __future__ import annotations

import ast
import contextlib
import io
import types
from pathlib import Path
from typing import Any

import pytest

from base.lm.registry import ModelSpec
from meiva.sdk_surface.help import ModelHelpProfile, doc_variants, render

pytestmark = pytest.mark.usefixtures("sdk_model_owner")


def _render_text(target: Any, *, profile: ModelHelpProfile) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        render(target, profile=profile)
    return buf.getvalue()


def _greet(name: str) -> str:
    return name


_greet.__doc__ = "Greet someone by name."


def _fake_namespace() -> Any:
    mod: Any = types.ModuleType("fakemod")
    mod.__doc__ = "Fake namespace."
    mod.__all_for_meiva__ = ["greet"]
    mod.greet = doc_variants(terse="Say hi.", detailed="Greet the person by name.")(_greet)
    return mod


class _Thing:
    pass


_Thing.__doc__ = "A documented thing with a longer contract."


def test_doc_variants_replace_the_function_text_for_the_named_style() -> None:
    mod = _fake_namespace()

    plain = _render_text(mod, profile=ModelHelpProfile())
    terse = _render_text(mod, profile=ModelHelpProfile(doc_style="terse"))

    assert '"""Greet someone by name."""' in plain
    assert '"""Say hi."""' in terse
    assert "Greet someone by name." not in terse


def test_a_style_the_target_does_not_declare_keeps_the_docstring() -> None:
    mod = _fake_namespace()

    rendered = _render_text(mod, profile=ModelHelpProfile(doc_style="blunt"))

    assert '"""Greet someone by name."""' in rendered


def test_a_variant_can_restate_the_namespace_docstring() -> None:
    mod = _fake_namespace()
    doc_variants(terse="Short namespace.")(mod)

    rendered = _render_text(mod, profile=ModelHelpProfile(doc_style="terse"))

    assert '"""Short namespace."""' in rendered
    assert "Fake namespace." not in rendered


def test_a_variant_can_replace_a_class_docstring() -> None:
    mod: Any = types.ModuleType("fakemod")
    mod.__doc__ = "Fake namespace."
    mod.__all_for_meiva__ = ["Thing"]
    mod.Thing = doc_variants(terse="A thing.")(_Thing)

    rendered = _render_text(mod, profile=ModelHelpProfile(doc_style="terse"))

    assert "A thing." in rendered
    assert "longer contract" not in rendered


def test_the_profile_hides_the_members_it_names() -> None:
    mod = _fake_namespace()

    rendered = _render_text(
        mod, profile=ModelHelpProfile(hidden_members=frozenset({"fakemod.greet"}))
    )

    assert "greet" not in rendered


def test_the_profile_merges_the_capability_gate_with_the_declaration(
    model_catalog: Any, add_models: Any
) -> None:
    catalog = add_models(
        model_catalog,
        {
            "text-only": ModelSpec(provider="probe"),
            "declared": ModelSpec(
                provider="probe",
                media_types=frozenset({"image"}),
                sdk_hidden_members=frozenset({"meiva.shell.sessions.deep"}),
                sdk_doc_style="terse",
            ),
        },
    )

    text_only = ModelHelpProfile.for_model("text-only", catalog=catalog)
    declared = ModelHelpProfile.for_model("declared", catalog=catalog)

    assert text_only.hidden_members == frozenset({"meiva.self.attach"})
    assert text_only.doc_style is None
    assert declared.hidden_members == frozenset({"meiva.shell.sessions.deep"})
    assert declared.doc_style == "terse"


def test_current_is_empty_without_an_agent_context() -> None:
    assert ModelHelpProfile.current() == ModelHelpProfile()


def _declared_styles(repo_root: Path) -> set[str]:
    styles: set[str] = set()
    for root in (repo_root / "meiva", repo_root / "meiva_builtins/plugins"):
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "doc_variants":
                    styles.update(keyword.arg for keyword in node.keywords if keyword.arg)
    return styles


def test_every_style_a_model_names_is_declared_by_some_target(model_catalog: Any) -> None:
    repo_root = Path(__file__).resolve().parents[4]
    wanted = {spec.sdk_doc_style for spec in model_catalog.models.values() if spec.sdk_doc_style}
    if not wanted:
        pytest.skip("no model declares a help doc style yet")

    assert wanted <= _declared_styles(repo_root)
