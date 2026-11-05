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


def test_sample_reports_machine_memory_and_capped_units(tmp_path: Path) -> None:
    root = tmp_path / "user.slice"
    _write(root / "memory.current", "1000\n")
    _write(root / "app.slice" / "run-a.scope" / "memory.current", "400\n")
    _write(root / "app.slice" / "run-a.scope" / "memory.max", "1024\n")
    _write(root / "app.slice" / "run-a.scope" / "memory.peak", "900\n")
    _write(root / "app.slice" / "run-a.scope" / "memory.events", "max 3\noom_kill 1\n")
    _write(root / "free.scope" / "memory.current", "10\n")
    _write(root / "free.scope" / "memory.max", "max\n")
    _write(
        tmp_path / "meminfo",
        "MemTotal: 1000 kB\nMemAvailable: 400 kB\nSwapTotal: 200 kB\nSwapFree: 50 kB\n",
    )
    _write(tmp_path / "machine" / "memory.current", "555\n")
    _write(
        tmp_path / "machine" / "memory.stat",
        "anon 300\nfile 200\nslab 45\nslab_reclaimable 40\nslab_unreclaimable 5\nkernel 9\n",
    )

    payload = sampler.sample(
        pressure=tmp_path / "absent",
        cgroup_root=root,
        limit=3,
        meminfo=tmp_path / "meminfo",
        machine_root=tmp_path / "machine",
    )

    assert payload["machine"] == {
        "charged": 555,
        "unreclaimable": 300 + 9 - 40,
        "anon": 300,
        "file": 200,
        "slab": 45,
        "slab_reclaimable": 40,
        "slab_unreclaimable": 5,
        "kernel": 9,
        "total": 1024 * 1000,
        "available": 1024 * 400,
        "swap_total": 1024 * 200,
        "swap_free": 1024 * 50,
    }
    assert [unit["path"] for unit in payload["capped"]] == ["app.slice/run-a.scope"]
    assert payload["capped"][0]["max"] == 1024
    assert payload["capped"][0]["peak"] == 900
    assert payload["capped"][0]["max_events"] == 3
    assert payload["capped"][0]["oom_kill"] == 1


def test_unreclaimable_does_not_double_count_the_slab_inside_kernel() -> None:
    stats = {
        "anon": 300,
        "file": 200,
        "slab": 45,
        "slab_reclaimable": 40,
        "slab_unreclaimable": 5,
        "kernel": 9,
    }

    # meiva:note
    # kernel already contains every slab, so the reclaimable part is subtracted once; a reader who
    # adds slab_unreclaimable on top gets a different, larger number, and this assertion is what
    # makes that difference visible instead of plausible.
    naive_double_count = stats["anon"] + stats["kernel"] + stats["slab_unreclaimable"]

    assert sampler.unreclaimable(stats) == 300 + 9 - 40
    assert sampler.unreclaimable(stats) != naive_double_count


def test_unreclaimable_is_unknown_without_the_columns_it_needs() -> None:
    assert sampler.unreclaimable({"anon": 300}) is None
    assert sampler.unreclaimable({}) is None


def test_machine_totals_sum_top_level_groups_when_the_root_has_no_counter(tmp_path: Path) -> None:
    machine = tmp_path / "cgroup"
    _write(machine / "user.slice" / "memory.current", "700\n")
    _write(machine / "user.slice" / "memory.stat", "anon 400\nfile 300\n")
    _write(machine / "system.slice" / "memory.current", "300\n")
    _write(machine / "system.slice" / "memory.stat", "anon 100\nfile 200\n")
    _write(machine / "init.scope" / "memory.current", "5\n")
    _write(machine / "cpu.pressure", "some avg10=0.00\n")

    charged, stats = sampler.machine_totals(machine)

    assert charged == 1005
    assert stats["anon"] == 500
    assert stats["file"] == 500


def test_missing_kernel_files_degrade_to_empty_values(tmp_path: Path) -> None:
    payload = sampler.sample(
        pressure=tmp_path / "absent",
        cgroup_root=tmp_path / "absent-root",
        limit=3,
        meminfo=tmp_path / "no-meminfo",
        machine_root=tmp_path / "no-cgroup",
    )

    assert payload["pressure"] == {}
    assert payload["events"] == {}
    assert payload["units"] == []
    assert payload["slice"]["current"] is None
    assert payload["machine"]["charged"] is None
    assert payload["machine"]["unreclaimable"] is None
    assert payload["machine"]["total"] is None
    assert payload["capped"] == []


def test_append_line_rotates_at_the_size_cap(tmp_path: Path) -> None:
    output = tmp_path / "logs" / "host-memory.jsonl"

    sampler.append_line(output, "first", max_bytes=1)
    sampler.append_line(output, "second", max_bytes=1)

    assert (tmp_path / "logs" / "host-memory.jsonl.1").read_text(encoding="utf-8") == "first\n"
    assert output.read_text(encoding="utf-8") == "second\n"
