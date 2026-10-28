---
type: doc
title: "meizel/ — The Home a Path Names"
description: "When a `.expanduser()` read is allowed: a `~` in a path the caller named expands the caller's own home by convention and needs no root; a path the function builds itself is the process's home and records; one derivation deep, names only."
tags:
  - locality
  - tool
---

# meizel/ — The Home a Path Names

`.expanduser()` reads the home every time it is called. What the rule decides is whether that read
is allowed: one the caller already named is, one the function makes for itself is not.

It is allowed when the expanded path is built from the enclosing function's caller — a bare
parameter, a local assigned from one, a loop or comprehension variable over one (`Path(path)`,
`Path(str(path))`, `Path(f"{path}~")`, `for p in paths: Path(p)`). The `~` there names the caller's
own home by convention: the caller wrote it, and no caller can hand the home in instead without
re-implementing `expanduser` (`~user`, an unset `HOME`). Such a read needs no root and is recorded
nowhere.

It is not allowed when the function builds the path itself — a literal, a module global,
`sys.argv[...]`, or a name the function rebinds from a value it builds. Then the function asks which
home this process runs under, and the answer is a root's to read.

## What the rule does not reach

- **One derivation, not a chain.** `p = Path(path); q = p; q.expanduser()` is the function's own
  read and is recorded. The analysis is a single pass, and its misses fall on the side of the world.
- **Attributes are not followed.** `self.x.expanduser()` is recorded: what a constructor stored on
  `self` is booked where the constructor read it, and `self`/`cls` are not caller names.
- **`os.path.expanduser(x)` is recorded either way.** It is read as a name, where the argument is
  not visible to the recorder.
- **A `~` from a file the process read is recorded.** `parts = line.split()` of a matched line is
  not a parameter, however plainly it names the caller's home.

The rule this refines is [ambient reads](ambient.meiva.okf.md); `expanduser` is the one
standard-library read whose argument decides whether it is allowed.

Parent: [[meizel/docs/checker/checker.meiva.okf.md|the checker]].
