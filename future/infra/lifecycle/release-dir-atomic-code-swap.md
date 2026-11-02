# Sealed release directories behind a `source` pointer

> Status (2026-11-02): **in progress.** Phase 1 (the release format, sealing,
> verification and identity) is being built; phases 2 and 3 are planned below.
> Supersedes the 2026-06-04 research note of the same name, which deferred the
> mechanism until the footgun "bites often enough". It now does.

## The failures this prevents

Every unit runs from `$MEIVA_HOME/source`, a full Git clone on a detached HEAD that
`cli.fleet_update` switches in place (`git checkout --detach NEW`, `uv sync`) after
every service has stopped. Each mechanism below names the failure it prevents
([postmortem 0009](../../../docs/postmortems/0009-complexity-must-name-the-failure-it-prevents.md)):

1. **Agents use the production tree as a workspace.** On 2026-11-02 the operator's
   own host held 77 local branches and 2 stashes in `~/.meiva/source`, created by
   agents despite the
   [modification-layers rule](../../../meiva_builtins/skills/platform/meiva-guide/modification-layers/SKILL.md).
   A Git checkout invites `checkout`, `branch`, `worktree`, `stash`.
2. **In-place edits break every agent on the box.** The 2026-08-28 fleet-wide outage
   (a half-installed plugin) and the editable-pointer outage of
   [postmortem 0006](../../../docs/postmortems/0006-an-editable-install-is-a-cross-checkout-pointer.md).
   Today the tree is writable and the only guard is a `git status` on the health-probe
   cadence, after the damage.
3. **Kept services run over swapped files.** `pty-sessions` and `browser` survive an
   update (`--keep-service`), so their lazy imports can load NEW code into an OLD process.
4. **The outage window includes the checkout and `uv sync`.** Both run after the stop.
5. **No quick code rollback.** Going back is another checkout and sync.

Not in scope: schema coordination. Migrations stay forward-only and cluster-wide
([source-mode decision](../../../docs/decisions/runtime/updates/release/2026-09-30-networked-cluster-stays-on-source-updates.md));
a pointer switch back is a code rollback only when the window carried no migration, and
`CodeBehindSchema` refuses the rest at start.

## Shape

```
$MEIVA_HOME/
  source-releases/
    <code_version>-<sha12>/        sealed release tree, no .git
      .meiva-release.json          commit, code_version, committed_at
      .venv/                       built at this final path (venvs do not move)
      ...                          `git archive` of the commit, plus build outputs
    <code_version>-<sha12>.manifest.json   outside the tree, sealed too
  source -> source-releases/<code_version>-<sha12>
```

The directory is `source-releases/`, not `releases/`: hosts may still hold a `releases/`
left by the deleted image path
([decision](../../../docs/decisions/runtime/updates/release/2026-09-30-remove-release-image-path.md)),
which nothing reads.

- **No `.git` in a release.** Every Git command fails there, so the workspace misuse
  ends at the first command instead of at the next outage. History questions go to a
  host-local bare mirror (phase 3), which is not the running code.
- **Identity comes from `.meiva-release.json`.** `LoadedCommit.capture` reads the commit
  and `CodeVersion` reads the first-parent count from the record, computed where the
  full history exists. The code-version gate keeps its meaning; a tree that is neither a
  release nor a Git checkout still fails fast.
- **The pointer is for people and entry points only.** A process started through
  `source/.venv/bin/python` gets an unresolved `sys.prefix`, so its site-packages would
  follow the pointer after a switch while `meiva` itself resolves through the editable
  `.pth` to the real path. Every launch resolves the pointer once and execs real paths
  (phase 2), so a process is pinned to the release it started from.

## Integrity

Agents run with the operator's uid. Any protection that uid can undo with one command is
a convention, not a boundary
([postmortem 0004](../../../docs/postmortems/0004-isolation-one-command-can-undo.md)), so
the goal is: a careless write fails, and any change is detected. A hostile process with
the operator's uid can already rewrite `.env` and the secrets; defending the tree alone
against it buys nothing.

1. **Seal (the main line).** After the build, every file and directory loses its write
   bits and, on macOS, gains `UF_IMMUTABLE` (`chflags uchg`). `sed -i`, an editor save,
   `pip install -e`, `uv sync` into the release and `rm -rf` all fail with `EPERM`.
   Undoing it takes a deliberate `chflags nouchg` + `chmod`.
2. **Detect.** The manifest records each entry's kind, mode, size, inode, `mtime_ns`,
   `ctime_ns`, symlink target and file SHA-256, captured after sealing. The kernel sets
   `ctime` on every write, `chmod` or flag change and no unprivileged call can set it
   back, so comparing `lstat` against the manifest finds every change without reading
   content. Measured on the operator's host with a release of `main` staged by phase 1
   (82,240 entries, 1.1 GB, 160 s to stage): verifying by `lstat` takes about 4 s,
   rehashing every file about 48 s. So `meiva start` and the health probe compare `lstat`
   only, and a full content check runs at sealing and on demand.
3. **A real boundary (deferred).** Releases owned by root or a dedicated user would stop
   the operator's uid outright, at the price of `sudo` on every host for every update
   and a root-owned installer (a sudoers rule naming a user-writable script is a
   privilege escalation). Stop-loss trigger: the first time an agent unseals a release.

Linux caveat: uv links from its cache by hardlink there, so sealing would change the
cache's inode and other links would move `ctime`; staging uses `--link-mode=copy` on
Linux. macOS clones by default and needs nothing.

## The five questions

1. **Boundary.** Where a home's code lives, how it is sealed and verified, and how the
   home selects one release. Not schema coordination, not the update orchestration
   (`cli.fleet_update` keeps stop, switch, start), not a security boundary against the
   operator's uid.
2. **Prior art.** Release directories behind a `current` symlink are the Capistrano
   pattern (rung 1: the filesystem). A tree manifest with digests and stat keywords is
   BSD `mtree`; file-integrity monitors (AIDE, Tripwire) do the same with a database and
   a daemon. Read-only roots are what container images and ostree give a whole OS.
3. **Simplest option.** The symlink and `chmod`/`chflags` are platform commands. For the
   manifest, macOS's `/usr/sbin/mtree` records `ctime` and `inode` and can verify, but
   Linux ships no mtree verifier by default (bsdtar only writes the format; NetBSD's port
   is a separate package), and AIDE and Tripwire are installed on neither. The manifest is
   therefore a JSON file of about 200 lines of code (rung 4), kept readable by anyone.
4. **Failure prevented.** The five failures listed at the top. What we now own: a
   manifest format, the stat-versus-content trade-off, and unsealing for removal.
5. **Limit signal and exit.** A second consumer of tree manifests, or the format needing
   more than its eight fields, moves it to the mtree spec format (written by `bsdtar`,
   verified by `mtree` where present). A real attacker in scope moves releases to a
   separate owner (the deferred third layer).

Owner sign-off needed (technology-selection gates): two new persisted formats, the
release record (`meiva-source-release/v1`) and the manifest (`meiva-sealed-tree/v1`).

## Phases

**Phase 1 (this change).** Production does not switch yet.

- `base/native_process/loaded_commit.py` owns the release record; `capture` reads it and
  `CodeVersion` uses its count.
- `base/deploy/sealed_release/` seals, unseals, writes the manifest and verifies a tree
  (`lstat`, or full content); its `stage` entry stages a commit from a full clone into
  `source-releases/<id>/` (archive, record, `uv sync --frozen --compile-bytecode`,
  `compileall`, seal, manifest), switches the pointer atomically, and verifies; usable by
  hand on one host. A phase-1 release has no frontend build, so it cannot yet serve a
  gateway.
- The health probe's source guard verifies the manifest when `source` points into
  `source-releases/`, and keeps the `git status` guard for a checkout.

**Phase 2 (fleet update).** `cli.fleet_update down` stages NEW on every host before the
first stop (the stored-schedule check then runs from the staged release's own venv,
replacing its `git worktree add` in the production checkout); after the stops it switches
the pointer; `up` is unchanged. Launches resolve the pointer once (`meiva start` and
meiva-root unit `exec` use real paths, so the release id enters the launch digest).
The frontend build moves into staging on the gateway: its key binds the home's gateway
port and browser origin, so a configuration change that alters the key restages the same
commit. The firewall manifest's `~/.meiva/source/.venv` glob moves to
`~/.meiva/source-releases/*/.venv`. Release GC keeps the current and two previous releases and
never removes one a live process was born from. A runbook covers the one-time cutover:
the old `source/` directory, with its stray branches and stashes, is renamed
`source.legacy-<date>`, never deleted.

**Phase 3 (Git leaves the running tree).** A host-local bare mirror (`$MEIVA_HOME/git/meiva.git`)
serves what still needs history: the core content channel's fetch (today into the
checkout's `.git`), `requires_commit` ancestry, the self-evolution log, and runner fetches
(today from the gateway's checkout). `host_version` reads `committed_at` from the record.
The fleet update's host probe reads the record instead of `git rev-parse`/`status`.

## Choices made without a ruling yet

These defaults are open for the owner to overturn:

- Same-uid sealing plus detection, no root-owned releases (see Integrity).
- The frontend is built during staging on the host that serves it, not shipped prebuilt.
- The core content channel keeps its own cadence and moves to the bare mirror.
- Agents develop in their own clones (optionally `--reference` the bare mirror), never
  under `source-releases/`.
