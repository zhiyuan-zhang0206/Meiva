from pathlib import Path

import psycopg
import pytest

import meiva
import meiva.skills as skills_mod
from base.agents.context.clients import ClientSet
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import DbConfig
from base.log import logger
from meiva.tests.fixtures.pin_agent import pin_agent
from meiva.tests.skills._skills_helpers import AuditedAgent, write_skill
from meiva.tests.skills._skills_helpers import audited_agent as audited_agent
from meiva.tests.skills._skills_helpers import fake_skills_dir as fake_skills_dir

pytestmark = pytest.mark.usefixtures("unit_home")


def _alpha_beta_guide(root: Path) -> None:
    write_skill(root, "alpha", "name: alpha\ndescription: a", body="# A\n")
    d = root / "grp"
    d.mkdir()
    write_skill(d, "beta", "name: beta\ndescription: b", body="# B\n")
    guide = root / "guide"
    guide.mkdir()
    (guide / "SKILL.md").write_text(
        "---\nname: guide\ndescription: g\n---\n\n# Guide body\n", encoding="utf-8"
    )
    write_skill(guide, "sub", "name: sub\ndescription: s", body="# Sub\n")


def test_all_for_meiva_is_the_live_top_level_index(fake_skills_dir: Path) -> None:
    write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a")
    d = fake_skills_dir / "grp"
    d.mkdir()
    write_skill(d, "beta", "name: beta\ndescription: b")

    assert skills_mod.__all_for_meiva__ == ["alpha", "grp"]
    assert meiva.agent_visible_names(skills_mod) == ["alpha", "grp"]


@pytest.mark.usefixtures("sdk_model_owner")
def test_help_on_skills_module_is_index_only(
    fake_skills_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_skill(
        fake_skills_dir,
        "alpha",
        "name: alpha\ndescription: Alpha desc",
        body="# Alpha body\n\nSECRET_BODY_MARKER\n",
    )
    meiva.help(meiva.skills)
    out = capsys.readouterr().out
    assert "## meiva.skills.alpha\n\nAlpha desc" in out
    assert "SECRET_BODY_MARKER" not in out


@pytest.mark.usefixtures("sdk_model_owner")
def test_resolution_is_not_consumption(fake_skills_dir: Path, audited_agent: AuditedAgent) -> None:
    _alpha_beta_guide(fake_skills_dir)

    leaf = meiva.skills.alpha
    _ = leaf._description, leaf.path, leaf.name
    dir(leaf)
    repr(leaf)
    ns = meiva.skills.guide
    _ = ns._description, ns.path
    dir(ns)
    assert audited_agent.invoked() == []

    _ = leaf.__doc__
    assert audited_agent.invoked() == [("alpha", "loaded")]
    _ = leaf.__doc__
    assert audited_agent.invoked() == []

    meiva.help(meiva.skills.guide)
    assert audited_agent.invoked() == [("guide", "loaded")]


@pytest.mark.usefixtures("sdk_model_owner")
def test_index_render_records_no_loaded_attribution(
    fake_skills_dir: Path, audited_agent: AuditedAgent
) -> None:
    _alpha_beta_guide(fake_skills_dir)

    meiva.help(meiva.skills)
    assert audited_agent.invoked() == []
    meiva.help(meiva.skills.grp)
    assert audited_agent.invoked() == []
    meiva.help(meiva.skills.guide)
    assert audited_agent.invoked() == [("guide", "loaded")]

    meiva.help(meiva.skills.alpha)
    assert audited_agent.invoked() == [("alpha", "loaded")]


def test_files_read_skill_md_records_consumption(
    fake_skills_dir: Path, audited_agent: AuditedAgent
) -> None:
    write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a", body="# A\n")
    (fake_skills_dir / "alpha" / "notes.md").write_text("# notes\n", encoding="utf-8")

    out = meiva.files.read(meiva.skills.alpha.path + "/SKILL.md")
    assert out.endswith("# A\n") and "name: alpha" in out
    assert audited_agent.invoked() == [("alpha", "loaded")]

    meiva.files.read(meiva.skills.alpha.path + "/SKILL.md", start=1, end=1)
    assert audited_agent.invoked() == [("alpha", "loaded")]


@pytest.mark.usefixtures("sdk_model_owner")
def test_files_read_other_files_do_not_record(
    fake_skills_dir: Path, audited_agent: AuditedAgent
) -> None:
    write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a", body="# A\n")
    skill_dir = fake_skills_dir / "alpha"
    (skill_dir / "notes.md").write_text("# notes\n", encoding="utf-8")

    meiva.files.read(str(skill_dir / "notes.md"))
    assert audited_agent.invoked() == []

    stray = fake_skills_dir.parent / "stray"
    stray.mkdir()
    (stray / "SKILL.md").write_text("---\nname: stray\ndescription: s\n---\n", encoding="utf-8")
    meiva.files.read(str(stray / "SKILL.md"))
    assert audited_agent.invoked() == []

    meiva.help(meiva.skills)
    assert audited_agent.invoked() == []


@pytest.mark.usefixtures("sdk_model_owner")
def test_every_consumption_records_one_event_and_nothing_is_remembered(
    fake_skills_dir: Path, audited_agent: AuditedAgent
) -> None:
    write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a", body="# A\n")

    path = meiva.skills.alpha.path + "/SKILL.md"
    meiva.files.read(path)
    meiva.files.read(path)
    meiva.help(meiva.skills.alpha)

    assert audited_agent.invoked() == [("alpha", "loaded")] * 3
    assert not hasattr(skills_mod, "_recorded_skill_invocations")


def test_files_read_skill_md_silent_outside_agent(db_url: str, fake_skills_dir: Path) -> None:
    pin_agent(None)
    write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a", body="# A\n")
    out = meiva.files.read(meiva.skills.alpha.path + "/SKILL.md")
    assert out.endswith("# A\n")


def test_proxy_doc_consumes_and_records(fake_skills_dir: Path, audited_agent: AuditedAgent) -> None:
    write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a", body="# A\n")
    d = fake_skills_dir / "grp"
    d.mkdir()
    write_skill(d, "beta", "name: beta\ndescription: b", body="# B\n")

    out = meiva.skills.alpha.__doc__
    assert out is not None
    assert out.endswith("# A\n")
    assert "alpha" in out
    assert audited_agent.invoked() == [("alpha", "loaded")]

    nested = meiva.skills.grp.beta.__doc__
    assert nested is not None
    assert nested.endswith("# B\n")
    assert audited_agent.invoked() == [("beta", "loaded")]


def test_consuming_a_skill_lands_a_skill_invoked_row(
    fake_skills_dir: Path, db_conn: psycopg.Connection, audited_agent: AuditedAgent
) -> None:
    write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a", body="# A\n")

    assert meiva.skills.alpha.__doc__
    assert meiva.skills.alpha.__doc__

    rows = db_conn.execute(
        "SELECT source, attributes FROM audit_events "
        "WHERE agent_id = %s AND event_name = 'skill_invoked'",
        (audited_agent.id,),
    ).fetchall()
    assert (
        rows
        == [("self", {"skill": "alpha", "identifier": "alpha", "invocation_depth": "loaded"})] * 2
    )


# meiva:note
# The database is down: the process's clients dial a port nobody listens on.
def test_a_failed_write_is_reported_and_does_not_fail_the_read(
    fake_skills_dir: Path, database_gate: ProcessDbGate, audited_agent: AuditedAgent
) -> None:
    unreachable = Database(
        DbConfig(
            db_url="postgresql://nobody@127.0.0.1:1/none",
            db_sslmode="disable",
            db_pool_min_size=1,
            db_pool_max_size=1,
            pgbouncer_enabled=False,
        ),
        gate=database_gate,
    )
    pin_agent(audited_agent.id, clients=ClientSet(database=lambda: unreachable))
    write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a", body="# A\n")

    captured: list[str] = []
    sink = logger.add(lambda m: captured.append(str(m)), level="ERROR", diagnose=False)
    try:
        body = meiva.skills.alpha.__doc__
        assert body is not None
        assert body.endswith("# A\n")
    finally:
        logger.remove(sink)

    # meiva:note
    # loguru's sink is process-wide, so a concurrent producer (a telemetry queue
    # full report, a background worker) lands here too; assert this event's own
    # report exactly once rather than every ERROR the worker happened to emit.
    (report,) = [m for m in captured if "skill_invoked could not be recorded" in m]
    assert "skill_invoked could not be recorded" in report
    assert audited_agent.invoked() == []
