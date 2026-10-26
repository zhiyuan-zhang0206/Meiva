---
type: doc
title: Exec Memory Guard — Critical Pressure Kills the Largest Exec
description: 'At critical system memory pressure the agent host kills the one exec process domain with the largest footprint when that kill can relieve anything, and that exec''s result tells its agent why.'
tags:
- agent
- exec
- memory
---

# Exec memory guard

A runaway `execute_code` can grow until the operating system reclaims memory from a process of its own choosing, the agent host included. The guard (`services/agent_runner/agent_host/exec_memory_guard.py`, and the same rule in `services/agent_runner/exec_host/memory_guard.py` for the standalone exec host) is the host's answer: relieve the pressure with the smallest possible kill, and say so.

**It takes down a runaway; it does not free memory.** A kill is worth the work it destroys only when the victim is large, so the largest domain at or below the relief floor is left running and recorded instead — see [Relief floor](#relief-floor).

## Mechanism

- One sequential loop, the agent host background task `exec_memory_guard`, ticks every few seconds. Each tick reads the pressure level the OS itself reports (`base/host/memory_pressure.py`; macOS `kern.memorystatus_vm_pressure_level`). No memory threshold of ours is involved.
- Only at `critical` does it look at the exec process domains: the host's descendants that lead a session running an exec entry module, with the agent id and result path read from the leader's launch environment. It picks the one with the largest footprint (macOS `phys_footprint`, which counts compressed and swapped pages that `ps` RSS leaves out; a domain's footprint is the sum over its process group).
- It records the reason beside that run's result file (`base/native_process/exec_kill_notice.py`) and SIGKILLs the session leader. The owning run sees the root exit as for any external SIGKILL: its domain-close owner kills the rest of the group and reaps, then reads and consumes the notice.
- After a kill the loop sleeps one interval and re-reads the level. A recovered system is left alone; a system still critical loses the next largest exec, one at a time.

## Relief floor

A machine can sit at `critical` for hours without any exec being the cause: on 2026-10-26 macmini reported `critical` in 39% of that day's pressure samples, and the guard killed a 0.1 GiB exec while the level stayed critical fifty seconds later — the work was lost and nothing was relieved. So the kill now has a floor, `settings.agent.exec_memory_guard_floor_mb` (default 1024 MiB, `MEIVA_EXEC_MEMORY_GUARD_FLOOR_MB`):

- Footprint **above** the floor: the kill proceeds exactly as described above.
- Footprint **at or below** it: nothing is killed, and the guard emits `exec_memory_guard_skipped` (agent id, pid, footprint, floor, running count, pressure, free swap). The skip is reported once per episode and again only when the largest exec changes, because the loop ticks every few seconds while the level can stay critical all day.
- No exec running at all: nothing to do, and nothing is reported.

The floor belongs in configuration because the machine decides it: a runaway passes 1 GiB within seconds on any node, while ordinary work on a small node can look big. Setting it to 0 restores the old always-kill behaviour.

## What the agent sees

The crashed result carries the partial output plus a line stating that the host memory guard killed this exec at critical memory pressure, how large it was (GiB) and how many execs were running, with the advice to batch the work and stream or chunk reads. The kill emits the `exec_memory_guard_killed` event (agent id, footprint, running count, pressure).

The notice says why an exec died; it cannot make that exec's writes atomic. An exec that must not leave a truncated artifact writes a temporary file and renames it — with this guard in place that is a requirement, not an optimization (312's `.tmp` + `mv` on 2026-10-26 is the positive example: the killed run left no half-written report).

## Platforms

macOS only. Linux exposes pressure as rates and counters (PSI, cgroup events), not a state the kernel declares critical, so `host_memory_source()` returns None there and the guard does not run.
