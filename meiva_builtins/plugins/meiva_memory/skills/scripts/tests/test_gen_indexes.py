# meiva:note
# `gen_indexes.py` is a script run as its own process: it resolves the Meiva home at its entry and
# writes the directory indexes of that home's pool.
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "gen_indexes.py"


def test_the_script_writes_the_directory_indexes_of_the_pool_under_its_home(
    tmp_path: Path,
) -> None:
    pool = tmp_path / "memory"
    (pool / "topics").mkdir(parents=True)
    (pool / "topics" / "retry.md").write_text(
        "---\ntitle: Retry rule\ndescription: retries above three hurt\n---\n", encoding="utf-8"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEIVA_")}
    env["MEIVA_HOME"] = str(tmp_path)

    res = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )

    assert res.returncode == 0, res.stderr
    assert "* [Retry rule](retry.md) - retries above three hurt" in (
        pool / "topics" / "index.md"
    ).read_text(encoding="utf-8")
    assert "* [topics/](topics/) - 1 notes" in (pool / "index.md").read_text(encoding="utf-8")
