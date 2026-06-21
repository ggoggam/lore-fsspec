# Lore + fsspec: Design

This document specifies the `LoreFileSystem` class, its URL/ref grammar, and the
mapping from fsspec methods to `lore` API calls.

`lore` exposes a **genuine coroutine API** (`LoreExecutor.collect_async` /
`async_iter`, backed by native async entrypoints that resolve `asyncio` futures —
not a thread-pool shim). Per project decision, `LoreFileSystem` is therefore an
**`fsspec.asyn.AsyncFileSystem`**: we implement the underscore-prefixed
coroutines (`_info`, `_ls`, `_cat_file`, …) and let fsspec generate the sync API
automatically. We still take structural cues from
`fsspec.implementations.git.GitFileSystem` (URL/ref grammar, `ukey`, `MemoryFile`
reads) so the binding feels idiomatic.

## Package layout

```
src/lore_fsspec/
  __init__.py        # exports LoreFileSystem; registers protocol
  core.py            # LoreFileSystem (AsyncFileSystem): _info/_ls/_cat_file/...
  transaction.py     # LoreTransaction (fsspec Transaction): stage -> commit
  _lore.py           # async wrapper over the `lore` package: await a command,
                     #   collect events (collect_async), raise LoreError on failure
  _refs.py           # ref/revision parsing & resolution helpers
  errors.py          # LoreError and fsspec-friendly exception mapping
tests/
  test_filesystem.py
  test_refs.py
  conftest.py        # spins up / clones a throwaway fixture repo
```

Protocol registration (so `fsspec.filesystem("lore")` and `lore://` URLs work):

```toml
# pyproject.toml
[project.entry-points."fsspec.specs"]
lore = "lore_fsspec.core:LoreFileSystem"
```

## The `LoreFileSystem` class

```python
from fsspec.asyn import AsyncFileSystem

class LoreFileSystem(AsyncFileSystem):
    protocol = "lore"
    cachable = True            # instances cached by (path, ref) like GitFileSystem
    root_marker = ""

    def __init__(self, path=None, fo=None, ref=None, offline=False,
                 identity=None, asynchronous=False, loop=None, **kwargs):
        super().__init__(asynchronous=asynchronous, loop=loop, **kwargs)
        ...
```

### Async model & the event loop

`AsyncFileSystem` runs all coroutines on a dedicated background event-loop thread
(`fsspec.asyn.get_loop()`) and auto-synthesizes blocking methods via
`sync_wrapper`. This composes cleanly with `lore`: the native async functions call
`asyncio.get_running_loop()` at invocation time, which on fsspec's loop thread
returns fsspec's loop, and completion is signalled back to that same loop via
`call_soon_threadsafe`. We never create our own loop; we just `await
executor.collect_async()` inside our `_`-prefixed coroutines.

Implications:
- Sync callers get `fs.ls(...)`, `fs.cat(...)` for free (fsspec wraps the coros).
- Async callers use `LoreFileSystem(asynchronous=True)` and `await fs._ls(...)`,
  `await fs._cat_file(...)`, `fs.open_async(...)`.
- Concurrent reads (e.g. `_cat` fanning out over many paths) run concurrently on
  the loop instead of serializing — a real win for Lore's large-asset workloads.

### Constructor arguments

| arg | meaning | default |
|---|---|---|
| `path` | local Lore clone directory | cwd |
| `fo` | clone source: if `path` has no clone yet, `repository_clone` this server URL into it; otherwise an alias for `path` (mirrors `GitFileSystem`) | — |
| `ref` | default revision/branch for operations | current branch (`is_current`) |
| `offline` | pass `offline=True` in `LoreGlobalArgs` (local store only, no lazy fetch) | `False` |
| `identity` | Lore identity for `LoreGlobalArgs.identity` | none |

The constructor builds a reusable base `LoreGlobalArgs` (repository_path, identity,
offline) and a single `lore.Lore()` instance held as `self._lore`. The default
`ref` is resolved once via `branch_list` (the entry with `is_current=True`).

> **Mirror to Git:** `GitFileSystem` stores `self.repo = pygit2.Repository(path)`
> and defaults `ref="master"`. We store `self._lore` + base global args and
> default `ref` to the clone's current branch.

### Local vs. remote, and bootstrapping (`fo`)

Lore's docs confirm two facts that look contradictory but aren't (see
`01-overview.md`): the **server is the single source of truth** — *"the remote
holds the durable canonical state … and atomically advances latest pointers"* —
**and** every client *"holds working state on disk, … a fragment cache, and a
small local mutable store"* that most operations run against without contacting a
remote. "Source of truth" (durability/authority) is orthogonal to "where reads
execute" (the local clone). This is exactly Git's relationship — `git diff`
doesn't hit GitHub — only more so, because Lore is engineered for offline local
ops. **Therefore one `LoreFileSystem` covers both:** it always attaches to a local
clone, and "remote" is just lazy materialization through that clone, gated by
`offline` (see read semantics below). There is no second "remote" filesystem —
the `lore` binding only operates through a `repository_path`, and a server-only
read path doesn't exist without reimplementing the protocol (a non-goal).

The one place "remote" legitimately enters is the **first** time, when no clone
exists yet. We mirror `GitFileSystem`'s `fo` for this:

```python
# attach to an existing clone:
LoreFileSystem(path="/clones/proj", ref="main")
# bootstrap a clone that doesn't exist yet, then attach:
LoreFileSystem(path="/clones/proj", fo="lore://127.0.0.1:41337/proj", ref="main")
```

If `path` already holds a clone, `fo` is treated as a plain alias for `path`
(Git-compatible). If `path` has no clone and `fo` looks like a Lore server URL, we
`repository_clone(repository_url=fo, repository_path=path)` once, then attach.
After bootstrap the server URL lives in the clone's `.lore/config.toml` and `fo`
is irrelevant — so the server `host:port` is a one-time bootstrap input, never part
of addressing.

### Remote fetch semantics (`offline`) — how we differ from `GitFileSystem`

`GitFileSystem` **never** fetches or pulls: it reads only the local object
database and raises if a ref/object isn't already present (fetching is the user's
out-of-band job). Lore is strictly more capable on the read path — reading a file
at a cached revision can transparently pull just the **fragments** it needs from
the server-of-record. We lean into that:

- **Default `offline=False`** — reads may lazily fetch missing fragments. This is
  Lore's design intent and the thing that makes this binding better than the Git
  one. Trade-off: a "read" can hit the network and block.
- **`offline=True`** — restrict to the local store; a non-resident fragment errors
  instead of fetching. This recovers `GitFileSystem`'s predictable, no-surprise-
  stall behavior for callers who pre-materialize.
- **Refs are the hard edge regardless.** Switching to a branch/revision that was
  never materialized needs the server; in `offline=True` that's just an error,
  same as Git asking for an unfetched ref.

We do **not** implement fetch/pull ourselves (same as Git) — lazy materialization
covers the read path. As a deliberate `git fetch` analogue, we expose one explicit
sync helper, separate from reads:

```python
fs.fetch(ref=None)   # wraps revision_sync(revision=<resolved ref>): advance the
                     # local clone to a ref and make its tree/metadata local.
```

This keeps the network explicit when a caller wants it, while the read path stays
automatic by default.

> **Validated caveat.** `revision_sync` makes the ref's **tree/revision metadata**
> local (so `info`/`ls`/ref-resolution work without the network afterward), but it
> does **not** pull content **fragments** into the offline-readable local store —
> committed payloads stay in the server-of-record and are still fetched lazily on
> read. (Empirically, even an online lazy `storage_get` doesn't persist into the
> local immutable store: a subsequent offline `storage_get` of the same address
> still returns `ADDRESS_NOT_FOUND`.) The only way to make *content* local is to
> check it out to the working copy (`revision_sync(reset=True)`, which is
> destructive to local edits), so we don't fold that into `fetch`. Under
> `offline=True`, a read of never-materialized content therefore stays a clear
> `FileNotFoundError`.

## URL & ref grammar

We mirror `GitFileSystem` **verbatim**: the URL addresses a **local clone
directory**, and `<path>` is that local dir — exactly as `GitFileSystem`'s
`<path>` is a local repo dir.

```
lore://<local-clone-path>[:<ref>][@<inner-path>]
```

- `lore:///abs/clone:main@Content/Game.ini` → path=`/abs/clone`, ref=`main`,
  inner=`Content/Game.ini`
- `:` separates the clone path from ref; `@` separates ref from the in-repo path.
- A bare `lore://Content/Game.ini` resolves against the instance's `path`/`ref`.

> **The server host lives in config, not the URI.** The Lore server URL
> (`lore://127.0.0.1:41337/<repo>`) is **not** part of the fsspec URL. It is
> recorded in the clone's own `.lore/config.toml` when the repo is cloned/created,
> and the lib uses it automatically — validated in the spike, where `branch_push()`
> reached the server with only `repository_path=<clone>` set. This is why no
> `host:port` ever appears here, and why the `GitFileSystem` `:`/`@` grammar works
> cleanly (a local path has no `:port` to collide with the ref delimiter).
> Server selection / credentials are configuration (clone config, `identity`
> kwarg, Lore CLI auth), not addressing.

`_get_kwargs_from_urls(path)` extracts `{path, ref}` so `fsspec.open(url)`
constructs the right filesystem — same shape as `GitFileSystem._get_kwargs_from_urls`
(split off the clone `path` on the first `:`, then `ref` on `@`). `ref` precedence:
explicit method arg → URL ref → instance default.

### Refs

A Lore "ref" may be:
- a **branch name** (`main`, `feature/x`) — resolved through `branch_list` to the
  branch's tip revision (entry `.latest`),
- a **revision id** (full or hex prefix) — passed straight to `revision`,
- empty / the instance default → `""` (current working copy / branch tip).

> **Validated.** The `lore` `revision` field wants a **revision id**, not a branch
> name and not a decimal number: `revision="main"` errors `revision not found`,
> and `revision="2"` is matched as a *hex prefix* (so it finds a revision whose id
> starts `2`, not "revision number 2"). `_resolve_rev`/`_branch_tip` therefore map
> a known branch name → `branch_list` entry `.latest.hex()`, short-circuit the
> default ref to `""`, and pass anything else through as a revision id.
> `revision_find(number=N)` also returns a tip (`.signature`) if a number→id
> lookup is ever needed.

## Method mapping

We implement the `AsyncFileSystem` coroutine hooks (`_info`, `_ls`, `_cat_file`,
…); fsspec auto-generates the blocking `info`/`ls`/`cat` wrappers. Each coroutine
runs a Lore command via `await self._run(...)` (see backend wrapper).

> ### Validated against a live local server
> The mapping below was exercised end-to-end through the Python binding against a
> local `loreserver` (create → stage → commit → push → dump/info). Key behaviors
> that shaped the design:
>
> 1. **Paths are OS paths resolved against the process CWD.** The lib discovers
>    the repository from the path argument; the `LoreGlobalArgs.repository_path`
>    global does **not** reroute per-file path resolution. Passing a bare
>    `"hello.txt"` resolved to `<cwd>/hello.txt` and failed with
>    `invalid path`. **⇒ `LoreFileSystem` stores the clone root and passes
>    absolute paths (`os.path.join(root, inner)`) to every file op. We do NOT
>    `chdir` — that is global state and unsafe on the shared async loop.**
> 2. **`file_info` is per-path metadata only — it does not list a directory.**
>    A directory path yields one node for the directory itself (with an aggregate
>    `size`). Listing requires `repository_dump` (below).
> 3. **`repository_create(repository_url=…)` also initializes a local working
>    copy** at `repository_path`; no separate clone needed for a fresh repo.

### `async _ls(path, detail=True, ref=None)` — via `repository_dump`
Listing uses `repository_dump(LoreRepositoryDumpArgs(revision=ref, path=<abs>,
max_depth=1))`, which walks the tree and emits one
`LoreRepositoryStateDumpNodeEventData` per entry:
`name` (repo-relative, e.g. `sub/`, `sub/data.bin`), `type_data`
(`child <n>` for a directory, `addr <hash>-<context>` for a file), `size`,
`flags`. `max_depth=1` gives a single directory level; `path` scopes to a subtree.
This is also the basis for `find`/`walk`/`glob` (raise `max_depth` or recurse),
and it conveniently yields each file's content **address** inline.

### `async _info(path, ref=None)`
`await` `file_info(LoreFileInfoArgs(paths=[abs_path], revision=ref))`, take the
single matching `LoreFileInfoEventData`, and translate:

```python
{
    "name": path,
    "type": "directory" if ev.is_dir else "file",
    "size": ev.size,
    "hash": ev.hash,           # content address — used by ukey()
    "mode": ev.mode,
    "local_size": ev.local_size,   # bytes already materialized locally
}
```
Missing path → `FileNotFoundError`. (`_info` is also derivable from a
`repository_dump` node, avoiding a second call when we already listed.)

### `async _cat_file(path, start=None, end=None, ref=None)`
The primary read primitive for `AsyncFileSystem`. **Spike findings:** `file_dump`
does *not* return bytes — its event carries only `address`/`size_content` and it
neither wrote to its `path` arg in testing. Bytes come from the content-addressed
store. The read path:

1. Resolve the file's **address** = `LoreAddress(hash, context)` from a
   `file_info` (or `repository_dump`) event's `hash`/`context` bytes.
2. `storage_open(repository_path=root)` → `LoreStorageOpenedEventData.handle_id`.
3. `storage_get(handle, items=[LoreStorageGetItem(address=…, streaming=True)])`
   → stream `LoreStorageGetHeaderEventData(size_content)` + N ×
   `LoreStorageGetDataEventData(offset, bytes)`; reassemble (honor `start`/`end`
   via `offset`). `storage_get_file(items=[… path=tmp])` is the dump-to-disk variant.

> **Resolved (Phase 2).** The `LoreStorageGetItem` needs `partition` = the
> **repository id** (`repository_info` → `LoreRepositoryDataEventData.id`;
> `file_info` carries only `hash`/`context`), and the `storage_open` handle must be
> **remote-capable** (`has_remote_config=True` + `LoreStorageRemoteConfig(remote_url=…)`)
> because committed payloads live in the server-of-record. With those, `storage_get`
> succeeds (`STORAGE_GET_HEADER` + `STORAGE_GET_DATA`, `error_code 0`). `_cat_file`
> keeps a working-copy disk fast path for the checked-out ref and uses the in-store
> path otherwise (lazily fetching when `offline=False`).

`_cat`/`cat_ranges` (concurrent multi-path reads) come from the base class and now
genuinely run concurrently on the loop. The `storage_open` handle should be opened
once per filesystem (cached) and reused across reads, then `storage_close`d on
teardown.

### File objects: `_open` and `open_async`
- **Sync** `_open(path, "rb", ...)`: return a `MemoryFile` around the bytes from a
  blocking `cat_file` (mirrors `GitFileSystem`'s `MemoryFile` blob reads).
- **Async** `open_async(path, "rb")`: return an `AbstractAsyncStreamedFile` whose
  `_fetch_range(start, end)` maps to `storage_get` with a bounded byte range, so
  large assets stream without buffering the whole file.

### `async _ukey(path, ref=None)` / `ukey`
Return `info(...)["hash"]` (Lore content address) — stable cache key, exactly how
`GitFileSystem.ukey` returns the blob hex.

### Writes via `LoreTransaction`
Writes are not auto-committed per file. They go through `LoreTransaction` (next
section), which batches staging and finalizes with a single atomic
`revision_commit`. Default posture is read-only; writing requires entering a
transaction (and a `writable=True` filesystem).

## `LoreTransaction`

Lore's native write model — *write content → stage → commit a revision* — is a
transaction. We surface it as fsspec's transaction primitive so it reads like any
other fsspec write, but commits **atomically as one Lore revision**.

```python
from fsspec.transaction import Transaction

class LoreTransaction(Transaction):
    def __init__(self, fs, message=None, metadata=None, **kwargs):
        super().__init__(fs, **kwargs)
        self.message = message
        self.metadata = metadata or {}

    def start(self):
        self.fs._intrans = True
        self._staged = []          # paths staged in this txn

    def complete(self, commit=True):
        # files written during the txn have already been staged on .commit()
        if commit:
            self.fs._commit_revision(self.message, self.metadata)   # revision_commit
        else:
            self.fs._reset_paths(self._staged)                      # file_unstage/reset
        self.fs._intrans = False
        self._staged = []
```

Wiring:

```python
class LoreFileSystem(AsyncFileSystem):
    transaction_type = LoreTransaction

    def transaction(self, message=None, metadata=None):
        """Enter a write transaction that commits one Lore revision on exit."""
        self._transaction = LoreTransaction(self, message=message, metadata=metadata)
        return self._transaction
```

Usage:

```python
with fs.transaction(message="Import baked lighting"):
    fs.pipe_file("Content/Lighting/Baked.bin", data)  # write-to-disk + file_stage
    fs.pipe_file("Content/Config/Game.ini", ini_bytes)
# -> single revision_commit (+ branch_push) here; on exception -> reset, no revision
```

Mechanics (validated against the local server):
- **Authoring bytes = ordinary file I/O into the working copy.** `file_write` is
  *not* for authoring (its args are `address`/`path`/`output`; it materializes
  store content to a file). The spike confirmed the CLI pattern: write the file to
  `<root>/<inner>` on disk, then stage. So `_pipe_file`/`_put_file`/writable
  `.commit()` writes bytes to the absolute working-copy path and runs
  `file_stage(paths=[<abs>], scan=True)`, recording the path in `self._staged`.
  (Staging a non-absolute path silently ignores it — see the path-model finding.)
- `complete(commit=True)` issues exactly one `revision_commit(message=…)` → one
  revision regardless of file count, then `branch_push()` to the server (both
  succeeded in the spike). `commit=False` (exception in the `with`) reverts via
  `file_unstage`/`file_reset`.
- **Async note:** fsspec `Transaction.complete` is synchronous; it drives the
  underlying coroutines through `fsspec.asyn.sync(self.fs.loop, ...)`, so it works
  from both sync and async callers.
- `revision_commit` takes `message=`; extra tags via `revision_metadata_set`.
  Author identity comes from `LoreGlobalArgs.identity` (optional on the local
  auth-disabled server).

Open question (validate in Phase 3): exact binding for passing `message`/`metadata`
into the transaction given fsspec's `transaction` property contract (may warrant a
small `start_transaction(...)` helper), and whether `branch_push` should be
optional (offline commits without push).

## Backend wrapper (`_lore.py`)

Single async helper so the rest of the code never touches cffi/event plumbing
directly. It awaits the coroutine driver (`collect_async`):

```python
async def run(command, global_args, args, *, entry_type=None):
    """Execute a Lore command on the running loop, return collected events.
    If entry_type is given, filter to those event objects;
    map any LoreErrorCode in the stream to a LoreError."""
    executor = command(global_args, args)
    events = await executor.collect_async()
    # inspect for error events / LoreErrorCode, raise LoreError
    if entry_type is not None:
        return [e for e in events if isinstance(e, entry_type)]
    return events
```

`LoreFileSystem` exposes a small bound `self._run(command, args, *, entry_type)`
that injects the base `LoreGlobalArgs`. Because every `_`-method already runs on
fsspec's loop thread, awaiting here is safe and concurrent. (A streaming variant
built on `async_iter` is used by `open_async`'s `_fetch_range`.)

This isolates the streaming/event-tag details (`LoreEventTag`, `*EventData`
classes) and the error mapping (`LoreErrorCode` → `errors.LoreError`).

## Error handling (`errors.py`)

**Spike findings on the event stream.** Every command ends with
`LoreCompleteEventData(status)` (`0` success, `1` failure) followed by
`LoreEndEventData`. Failures additionally emit
`LoreErrorEventData(error_type, error_inner)`, where `error_type` is a
`LoreErrorCode` (e.g. `ADDRESS_NOT_FOUND`, `INVALID_ARGUMENTS`, `SLOW_DOWN`,
`INTERNAL`, `NONE`).

`_lore.run()` therefore:
- scans collected events for `LoreErrorEventData` / a non-zero
  `LoreCompleteEventData.status`, and raises `LoreError` carrying the
  `error_type` + `error_inner`.
- Maps codes to fsspec-friendly exceptions: `ADDRESS_NOT_FOUND` (and path-not-found
  variants) → `FileNotFoundError`; auth/permission → `PermissionError`;
  `INVALID_ARGUMENTS` → `ValueError`.
- A failed remote fetch while `offline=True` → clear message pointing at the
  `offline` flag / lazy-materialization behavior.

## Open questions

Resolved in the Phase 0 spike:
- ~~Directory listing~~ → `repository_dump` (tree-node stream); `file_info` is
  per-path metadata only.
- ~~`file_dump` addressing / read bytes~~ → bytes come from `storage_get` against
  a `LoreAddress(hash, context)`; `file_dump` returns metadata only.
- ~~Path model~~ → OS paths resolved against CWD; pass absolute paths, never
  `chdir`.
- ~~Async loop composition~~ → works on fsspec's loop.

Resolved in Phase 2:
1. ~~**`storage_get` item encoding.**~~ **Resolved — there is no FFI marshalling
   bug.** The empty echoed `LoreAddress` was just the failure echo; on success the
   real address comes back. The working recipe: `partition` = repository id
   (`repository_info` → `LoreRepositoryDataEventData.id`; `file_info` never carries
   the partition), `address` = `LoreAddress(hash, context)` from `file_info`, and a
   **remote-capable** `storage_open` (`has_remote_config=True` +
   `LoreStorageRemoteConfig(remote_url=…)`). Committed payloads live in the
   server-of-record (local immutable store often holds only metadata), so a
   local-only handle returns `ADDRESS_NOT_FOUND`; the remote-capable handle lazily
   fetches. `_cat_file` now uses this (with a disk fast path for the checked-out
   ref); the store handle is opened once, cached, and `storage_close`d on `close()`.

Resolved (Phase 2, continued):
1. ~~**Ref resolution.**~~ **Resolved.** `revision` takes a **revision id** (full or
   hex prefix), not a branch name (`"main"` → `revision not found`) and not a
   decimal number (`"2"` is matched as a hex *prefix*). `_resolve_rev`/`_branch_tip`
   resolve a branch name → its tip via `branch_list` `.latest`, short-circuit the
   default ref to `""`, and pass a revision id straight through.
   `repository_dump`/`file_info` both accept the resolved `revision`.
2. ~~**Lazy fetch under `offline`.**~~ **Resolved.** Default `offline=False` (reads
   lazily fetch missing fragments — Lore's design intent, and how we beat the Git
   binding); `offline=True` restricts to the local store and a non-resident
   fragment raises a clear `FileNotFoundError` (verified). Explicit `fs.fetch(ref)`
   wraps `revision_sync(revision=…)` — the `git fetch` analogue — and syncs the
   ref's **tree/revision metadata** only; it does **not** materialize content
   fragments into the offline-readable local store (validated: even an online lazy
   `storage_get` doesn't persist there). Making content local means a working-copy
   checkout (`revision_sync(reset=True)`, destructive), which `fetch` deliberately
   does not do.
3. ~~**Instance lifecycle.**~~ **Done:** the cached `storage_open` handle is
   `storage_close`d via `close()` / `__exit__` / a guarded `__del__`; opened lazily
   on the first in-store read and reused. Whether `lore.Lore()` itself needs a
   `shutdown()` is still open (no leak observed in tests).

## Testing strategy

> **Spike finding — Lore is server-backed.** Both `repository_create` and
> `repository_clone` require a `repository_url`; there is no serverless `init`
> (`service_start` is only the local background agent). So integration tests need
> a reachable Lore server + identity. Tests must therefore be gated on a
> `LORE_TEST_REPOSITORY_URL` (and credentials) env var and skipped otherwise —
> alongside the existing skip-when-no-`liblore` guard. Pure-unit tests
> (`_strip_protocol`, ref parsing, error mapping) need neither.

**Local server for fixtures (validated).** A self-hosted `loreserver` runs with
zero config (auth disabled, ephemeral self-signed cert + temp store), listening on
`41337` (QUIC/gRPC) and `41339` (HTTP); repos are addressed
`lore://127.0.0.1:41337/<name>`:

```bash
# one-time: install CLI + server (already present at ~/.local/bin/ here)
curl -fsSL https://raw.githubusercontent.com/EpicGames/lore/main/scripts/install.sh | bash -s -- --server
~/.local/bin/loreserver &                                   # start
curl -fsS http://127.0.0.1:41339/health_check               # 200 when ready
```

- `conftest.py` builds a fixture **when a server is configured** (env
  `LORE_TEST_REPOSITORY_URL`, default `lore://127.0.0.1:41337/...`): create a
  scratch repo (which also inits a local working copy at `repository_path`), write
  a couple of files **with ordinary file I/O**, `file_stage` (absolute paths) +
  `revision_commit` + `branch_push`; otherwise mark integration tests skipped.
- Unit tests (no server): ref parsing (`_strip_protocol`, `_get_kwargs_from_urls`),
  error mapping (`LoreErrorCode` → exception).
- Integration tests (server): `repository_dump`-based `ls`, `_info`, `_cat_file`
  read, transaction commit; round-trip through `fsspec.open("lore://…")` and
  `fsspec.filesystem("lore")`.
- Skip-marker when `liblore` for the current platform isn't available.
