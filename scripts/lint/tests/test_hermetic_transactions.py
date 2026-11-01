# meiva:note
# Unit tests for hermetic database transactions linter.

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from meizel import lint_common
from scripts.lint.diagnostics import hermetic_transactions as _lint


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    for rel in (*lint_common.FRAMEWORK_DIRS, "scripts", "meizel"):
        (tmp_path / rel).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("snippet", "expected_phrase"),
    [
        (
            """
            from base.db.transaction import write_transaction
            import httpx

            with write_transaction(pool) as conn:
                httpx.get("https://example.com")
            """,
            "Network/HTTP call (httpx.get)",
        ),
        (
            """
            from base.db.transaction import write_transaction
            from httpx import post

            with write_transaction(pool) as conn:
                post("https://example.com/api", json={})
            """,
            "Network/HTTP call (httpx.post)",
        ),
        (
            """
            from base.db.transaction import write_transaction
            import requests

            with write_transaction(pool) as conn:
                requests.post("https://example.com")
            """,
            "Network/HTTP call (requests.post)",
        ),
        (
            """
            from base.db.transaction import async_write_transaction
            import aiohttp

            async def f():
                async with async_write_transaction(pool) as conn:
                    session = aiohttp.ClientSession()
            """,
            "Network/HTTP call (aiohttp.ClientSession)",
        ),
        (
            """
            with conn.transaction():
                urllib.request.urlopen("https://example.com")
            """,
            "Network/HTTP call (urllib.request.urlopen)",
        ),
        (
            """
            from urllib.request import urlopen

            with conn.transaction():
                urlopen("https://example.com")
            """,
            "Network/HTTP call (urllib.request.urlopen)",
        ),
        (
            """
            with conn.transaction():
                socket.create_connection(("1.1.1.1", 80))
            """,
            "Network/HTTP call (socket.create_connection)",
        ),
        (
            """
            with conn.transaction():
                http_client.get("/health")
            """,
            "HTTP client call (http_client.get)",
        ),
        (
            """
            from base.db.transaction import write_transaction
            import time

            with write_transaction(pool) as conn:
                time.sleep(1)
            """,
            "Sleep call (time.sleep)",
        ),
        (
            """
            from base.db.transaction import async_write_transaction
            import asyncio

            async def f():
                async with async_write_transaction(pool) as conn:
                    await asyncio.sleep(0.5)
            """,
            "Sleep call (asyncio.sleep)",
        ),
        (
            """
            from time import sleep

            with conn.transaction():
                sleep(2)
            """,
            "Sleep call (time.sleep)",
        ),
        (
            """
            from base.db.transaction import write_transaction
            import subprocess

            with write_transaction(pool) as conn:
                subprocess.run(["echo", "hello"])
            """,
            "Subprocess call (subprocess.run)",
        ),
        (
            """
            from subprocess import Popen

            with conn.transaction():
                proc = Popen(["ls"])
            """,
            "Subprocess call (subprocess.Popen)",
        ),
        (
            """
            from base.db.transaction import async_write_transaction
            import asyncio

            async def f():
                async with async_write_transaction(pool) as conn:
                    proc = await asyncio.create_subprocess_exec("git", "status")
            """,
            "Subprocess call (asyncio.create_subprocess_exec)",
        ),
        (
            """
            with conn.transaction():
                os.system("rm -rf /tmp/foo")
            """,
            "Subprocess call (os.system)",
        ),
        (
            """
            from base.db.transaction import async_write_transaction

            async def f():
                async with async_write_transaction(pool) as conn:
                    result = await llm.ainvoke([prompt])
            """,
            "LLM / Runnable async invocation (llm.ainvoke)",
        ),
        (
            """
            from base.db.transaction import write_transaction

            with write_transaction(pool) as conn:
                result = model.invoke([prompt])
            """,
            "LLM invocation (model.invoke)",
        ),
        (
            """
            with conn.transaction():
                result = chain.invoke({"input": "query"})
            """,
            "LLM invocation (chain.invoke)",
        ),
        (
            """
            with conn.transaction():
                client.chat.completions.create(model="gpt-4", messages=[])
            """,
            "LLM client create call (client.chat.completions.create)",
        ),
        (
            """
            with conn.transaction():
                client.messages.create(model="claude-3-opus", messages=[])
            """,
            "LLM client create call (client.messages.create)",
        ),
        (
            """
            import openai

            with conn.transaction():
                openai.chat.completions.create(model="gpt-4o", messages=[])
            """,
            "LLM library client call (openai.chat.completions.create)",
        ),
        (
            """
            from anthropic import Anthropic

            with conn.transaction():
                client = Anthropic()
            """,
            "LLM library client call (anthropic.Anthropic)",
        ),
    ],
)
def test_violations_detected(snippet: str, expected_phrase: str) -> None:
    errors = _lint.scan_source(textwrap.dedent(snippet))
    assert len(errors) == 1
    assert expected_phrase in errors[0][1]


@pytest.mark.parametrize(
    "snippet",
    [
        """
        import requests
        from base.db.transaction import write_transaction

        res = requests.get("https://example.com")
        with write_transaction(pool) as conn:
            conn.execute("INSERT INTO responses VALUES (%s)", (res.text,))
        """,
        """
        with conn.transaction():
            conn.execute("UPDATE agents_meta SET status = 'active'")
            cur = conn.cursor()
            rows = cur.fetchall()
            conn.commit()
        """,
        """
        from langchain_core.messages import SystemMessage, HumanMessage
        from base.db.transaction import async_write_transaction

        async def f():
            async with async_write_transaction(pool) as conn:
                msg = SystemMessage(content="system instructions")
                hmsg = HumanMessage(content="user query")
                dump = msg.model_dump()
        """,
        """
        with conn.transaction():
            data = {"key": 1, "items": [1, 2, 3]}
            length = len(data["items"])
            text = str(length)
        """,
        """
        import time

        with conn.transaction():
            time.sleep(0.01)  # hermetic-tx-ok: deliberate lock contention test
        """,
        """
        import requests

        with conn.transaction():
            requests.get("https://localhost:8000")  # tx-external-io-ok: bootstrap loopback probe
        """,
    ],
)
def test_allowed_cases_clean(snippet: str) -> None:
    errors = _lint.scan_source(textwrap.dedent(snippet))
    assert errors == []


def test_main_clean_on_clean_target(fake_repo: Path) -> None:
    clean_file = _write(
        fake_repo,
        "base/clean.py",
        """
        with conn.transaction():
            conn.execute("SELECT 1")
        """,
    )
    assert _lint.main([str(clean_file)], repo_root=fake_repo) == 0


def test_main_fails_on_violation(fake_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad_file = _write(
        fake_repo,
        "base/bad.py",
        """
        import time

        with conn.transaction():
            time.sleep(1)
        """,
    )
    ret = _lint.main([str(bad_file)], repo_root=fake_repo)
    assert ret == 1
    captured = capsys.readouterr()
    assert "base/bad.py:5: Sleep call (time.sleep)" in captured.out
    assert "DDIA Violation (Cascading Connection Pool Exhaustion)" in captured.err


def test_main_skips_test_files_by_default(fake_repo: Path) -> None:
    _write(
        fake_repo,
        "base/tests/test_contention.py",
        """
        import time

        with conn.transaction():
            time.sleep(0.1)
        """,
    )
    assert _lint.main([], repo_root=fake_repo) == 0


def test_main_rejects_missing_path(capsys: pytest.CaptureFixture[str]) -> None:
    assert _lint.main(["nonexistent_path_xyz.py"]) == 1
    assert "error: target path(s) not found" in capsys.readouterr().err
