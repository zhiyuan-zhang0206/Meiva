---
type: doc
title: Meiva Code runtime state and notes
description: Host-side state deltas, after-exec messages and logical cwd recovery.
tags: [plugins, runtime, contracts]
---

# Runtime state and notes

The agent-runtime face is loaded for host-side hooks. The SDK face is owned by
the plugin entry and its state-access helpers; hooks receive graph state rather
than reading the SDK execution slot. The plugin overview owns registration and
the separation from context-file injection.

The hook reads the supplied state through the state handle's view and returns
writes through its delta. Context-file injection runs separately in the exec path.

The file-read wrapper supplies the current OS user home to the context-file
walker as a required path. The walker uses that path and the repository root
to choose its traversal boundary; it does not read the process home itself.

SDK file, shell, understanding and UI wrappers resolve their paths against the
logical cwd. Bare Python file operations and subprocess calls retain the
process's startup cwd. Initialization uses the invocation's explicit runtime
identity and distinguishes an absent field from a deliberately persisted value.

Fork copies the source agent's whole graph state, so the inherited cwd channel
is the source's. Re-homing is limited to the workspaces tree: a fork that
inherits a repo or checkout the source was working in keeps it, because that
path is the work's context, not the source agent's identity.

## Context-file discovery

`find_context_files_along_path(target, home=...)` starts at a target directory or
a file's parent. The read wrapper supplies the required user-home path. Among
the repository root and supplied home that are ancestors of the target, the
walker chooses the farthest ancestor. It returns no files if neither boundary
is on that path or the starting directory does not exist. A missing target file
can still discover context from its existing parent.

The walk visits every directory from that boundary through the target's starting
directory, collecting `AGENTS.md` before `CLAUDE.md` at each level. It returns
resolved file paths in that order, removes duplicate resolved paths, and skips
missing context files and directories with those filenames. Global instructions
therefore precede local ones.

For example, a target `/Users/me/proj/sub/foo.py` with repository root
`/Users/me/proj` and supplied home `/Users/me` scans `/Users/me`,
`/Users/me/proj`, then `/Users/me/proj/sub`. The home is the shallower boundary
even though the target also belongs to a repository.
