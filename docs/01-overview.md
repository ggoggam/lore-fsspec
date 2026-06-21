# Lore + fsspec: Overview

## Goal

Provide an [`fsspec`](https://filesystem-spec.readthedocs.io/) `AbstractFileSystem`
implementation for [Lore](https://epicgames.github.io/lore), Epic Games' new
open-source version control system — mirroring what
[`fsspec.implementations.git.GitFileSystem`](https://github.com/fsspec/filesystem_spec/blob/master/fsspec/implementations/git.py)
does for Git.

The result lets any fsspec-aware tool (pandas, pyarrow, zarr, dask, `fsspec.open`,
`universal_pathlib`, etc.) read files out of a Lore repository at an arbitrary
revision/branch using a `lore://` URL, without shelling out to the CLI.

```python
import fsspec

fs = fsspec.filesystem("lore", path="/path/to/clone", ref="main")
fs.ls("Content/Maps")
with fs.open("Content/Maps/Demo.umap", "rb") as f:
    data = f.read()

# or the URL form
with fsspec.open("lore:///path/to/clone:main@Content/Config/Game.ini", "rt") as f:
    print(f.read())
```

## What is Lore?

Lore is an MIT-licensed VCS written in Rust, built for game/entertainment teams
working with very large binary assets. It began as "Unreal Revision Control" and
ships inside Unreal Editor for Fortnite.

Distinguishing properties vs. Git (all relevant to this binding):

| Concept | Lore | Git equivalent |
|---|---|---|
| Server | Centralized **server-of-record** for durability, ACLs, conflict resolution | Decentralized, no required server |
| Storage | **Content-addressed**, **fragment-level dedup** (FastCDC + zstd) | Content-addressed objects (whole-blob) |
| Working copy | **Sparse, lazy** — materializes only what you ask for | Full checkout |
| Local ops | Staging, commit, branch, diff are **offline / no network round-trip** | Same |
| Versioning | **Revisions** (numbered) on **branches**, free branching | Commits (hashes) on branches |

Two consequences shape the design:

1. **A clone is the unit we attach to.** Local read/inspect operations run against
   a local repository path (the working copy + local store), so the filesystem is
   constructed around a local clone — exactly like `GitFileSystem` attaches to a
   local repo directory.
2. **Lazy materialization is a feature, not a bug.** Reading a file at a revision
   may fetch fragments from the remote store on demand. We can run `offline` when
   the caller guarantees everything needed is local.

## The `lore` Python package (our backend)

The PyPI dist `lore-vcs` installs the importable package `lore`. It wraps the
Rust `liblore` shared library via cffi and exposes a fluent API.

Execution model (verified against `lore-vcs==0.8.3`):

```python
import lore
from lore.types.args import LoreGlobalArgs, LoreFileInfoArgs

L = lore.Lore()                      # fluent entry point
# args are constructed with keyword arguments (the attribute names are read-only
# properties, NOT fluent setters):
gargs = LoreGlobalArgs(repository_path="/path/to/clone", offline=True)
args  = LoreFileInfoArgs(paths=["Content"], revision="main")

executor = L.file_info(gargs, args)  # returns a LoreExecutor (lazy)
events   = executor.collect()        # runs; returns a list of typed event objects
```

Every command follows the same shape:

```
L.<command>(global_args: LoreGlobalArgs, args: Lore<Command>Args) -> LoreExecutor
```

- **`LoreGlobalArgs`** — repository path, identity, and execution flags
  (`offline`, `local`, `in_memory`, `dry_run`, `force`, `cache`, `remote`, …).
- **`Lore<Command>Args`** — per-command inputs; constructed with **keyword
  arguments** (`LoreFileInfoArgs(paths=[...], revision="...")`). The public
  attribute names are read-only properties, not fluent setters.
- **`LoreExecutor`** — lazy handle with both a **sync** and a genuine **async**
  driver:
  - sync: `.collect()` (all events), `.wait()` (side effects only).
  - async: `await .collect_async()`, `await .wait_async()`, and `async for ev in
    .async_iter()` for streaming.

  The async path is real coroutine I/O, not a thread shim: each command's native
  `lore_<cmd>_async` starts work on `liblore`'s own threads, returns immediately,
  and resolves an `asyncio` future via `loop.call_soon_threadsafe(...)`. Awaiting
  therefore yields to the event loop while native work runs in the background.
  **Because of this, the binding is implemented as an fsspec `AsyncFileSystem`**
  (see `02-design.md`).

Commands **stream typed events**. For example `branch_list` emits
`LoreBranchListBeginEventData`, N × `LoreBranchListEntryEventData`, then
`LoreBranchListEndEventData`. We consume the relevant entry events.

### Methods that matter for a filesystem

| fsspec need | Lore method | Returns / event |
|---|---|---|
| stat a path (`info`) | `file_info` | `LoreFileInfoEventData(path, hash, is_file, is_dir, mode, size, local_size, local_hash, …)` |
| list a dir (`ls`) | `file_info` over a directory path | one event per child |
| read bytes (`_cat_file`) | `storage_get` (in-mem) / `file_dump` (to disk) | `storage_get` streams `LoreStorageGetDataEventData(offset, bytes)` chunks; `file_dump` materializes to a disk `path` |
| list branches/refs | `branch_list` | `LoreBranchListEntryEventData(name, id, latest, is_current, …)` |
| resolve / list revisions | `revision_history`, `revision_info`, `revision_find` | revision metadata |
| working-copy state | `repository_status` | staged / dirty / conflicted files |
| clone (bootstrap) | `repository_clone` | — |
| write (optional) | `file_write` → `file_stage` → `revision_commit` | — |
| raw content store (CAS) | `storage_open` / `storage_get` / `storage_get_file` | content-addressed buffers by hash |

There is exactly **one** public filesystem:

- **`LoreFileSystem`** — the path/revision view (the Git-parity binding).

A content-addressed (`storage_*`, blob-by-hash) view was considered and dropped: a
hash-keyed namespace doesn't fit fsspec's path-oriented model and is redundant for
the goal here. The `storage_*` / `file_dump(address=…)` APIs are still used
**internally** as an optimization (e.g. streaming reads in `open_async`), just not
surfaced as a second filesystem.

Writes are exposed through **`LoreTransaction`**, an fsspec transaction that maps
onto Lore's native stage→commit flow (see `02-design.md`).

## Non-goals (initial)

- Re-implementing Lore in Python — we delegate to `liblore` via the `lore` package.
- A network/server protocol implementation — we rely on a local clone; remote
  fetch happens through Lore's own lazy materialization.
- Full read/write parity in v1 — writes are a later milestone behind a flag.

## References

- Lore docs: <https://epicgames.github.io/lore>
- Lore system design: <https://epicgames.github.io/lore/explanation/system-design/>
- Lore source: <https://github.com/EpicGames/lore>
- fsspec `GitFileSystem`: <https://github.com/fsspec/filesystem_spec/blob/master/fsspec/implementations/git.py>
- Coverage / background:
  [The Register](https://www.theregister.com/devops/2026/06/17/git-good-with-epic-games-new-open-source-vcs-lore/),
  [Phoronix](https://www.phoronix.com/news/Epic-Games-Lore-VCS)
