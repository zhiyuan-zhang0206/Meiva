# meiva:note
# Process-level checks for the read-only host memory sampler: unit ranking,
# unbounded-limit decoding and log rotation, plus the degrade path when the
# kernel files are absent.
# meiva:link scripts/host_ops/docs/host-memory-sample.meiva.okf.md
from __future__ import annotations

from pathlib import Path

from scripts.host_ops import host_memory_sample as sampler


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_sample_ranks_units_and_treats_max_as_unbounded(tmp_path: Path) -> None:
    root = tmp_path / "user.slice"
    _write(root / "memory.current", "1000\n")
    _write(root / "memory.max", "max\n")
    _write(root / "memory.swap.current", "10\n")
    _write(root / "memory.swap.max", "20\n")
    _write(root / "memory.events", "low 0\nhigh 3\nmax 1\noom 1\noom_kill 1\n")
    _write(root / "a.scope" / "memory.current", "500\n")
    _write(root / "a.scope" / "memory.max", "max\n")
    _write(root / "b.scope" / "memory.current", "900\n")
    _write(root / "b.scope" / "memory.max", "1024\n")
    _write(root / "c.service" / "memory.current", "700\n")
    _write(root / "app.slice" / "memory.current", "999999\n")
    _write(
        tmp_path / "pressure",
        "some avg10=1.50 avg60=0.50 avg300=0.10 total=1234\n"
        "full avg10=0.00 avg60=0.00 avg300=0.00 total=99\n",
    )

    payload = sampler.sample(pressure=tmp_path / "pressure", cgroup_root=root, limit=2)

    assert [unit["unit"] for unit in payload["units"]] == ["b.scope", "c.service"]
    assert payload["units"][0]["max"] == 1024
    assert payload["units"][1]["max"] is None
    assert payload["slice"] == {
        "current": 1000,
        "max": None,
        "swap_current": 10,
        "swap_max": 20,
    }
    assert payload["events"]["oom_kill"] == 1
    assert payload["pressure"]["some"]["avg10"] == 1.5
    assert payload["ts"].endswith("+00:00")


def test_missing_kernel_files_degrade_to_empty_values(tmp_path: Path) -> None:
    payload = sampler.sample(
        pressure=tmp_path / "absent",
        cgroup_root=tmp_path / "absent-root",
        limit=3,
    )

    assert payload["pressure"] == {}
    assert payload["events"] == {}
    assert payload["units"] == []
    assert payload["slice"]["current"] is None


def test_append_line_rotates_at_the_size_cap(tmp_path: Path) -> None:
    output = tmp_path / "logs" / "host-memory.jsonl"

    sampler.append_line(output, "first", max_bytes=1)
    sampler.append_line(output, "second", max_bytes=1)

    assert (tmp_path / "logs" / "host-memory.jsonl.1").read_text(encoding="utf-8") == "first\n"
    assert output.read_text(encoding="utf-8") == "second\n"
