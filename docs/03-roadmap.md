# Lore + fsspec: Roadmap

Phased plan. Each phase is independently shippable; v1 target is **read-only
parity with `GitFileSystem`**.

## Phase 0 — Spike & validate the backend (no public API yet)
Resolve the open questions in `02-design.md` against a real local clone.

Validated so far (against `lore-vcs==0.8.3`, arm64 macOS):
- [x] `lore` coroutine API runs on **both** a vanilla asyncio loop and fsspec's
      dedicated loop thread (`fsspec.asyn.sync(get_loop(), ...)`) — `AsyncFileSystem`
      composition confirmed, no "no running loop"/hang.
- [x] Args are **kwarg-constructed** (`LoreGlobalArgs(repository_path=..., offline=True)`);
      attribute names are read-only properties, not fluent setters.
- [x] Event protocol: every command ends with `LoreComplete(status)` + `LoreEnd`;
      failures add `LoreErrorEventData(error_type: LoreErrorCode, error_inner)`.
- [x] Read model: `file_dump` writes to a disk `path` (no inline bytes); in-memory
      bytes come from `storage_get` → `LoreStorageGetDataEventData(offset, bytes)`.

Validated against a **local `loreserver`** (zero-config, auth disabled,
`lore://127.0.0.1:41337/<name>`), driven entirely through the Python binding:
- [x] `repository_create(repository_url=…)` creates the server repo **and** inits a
      local working copy at `repository_path`.
- [x] Write→commit→push works: author bytes with normal file I/O →
      `file_stage(scan=True)` → `revision_commit(message=…)` → `branch_push()`.
- [x] **Path model:** file ops resolve paths against the process **CWD**, not
      `repository_path`; pass **absolute** paths (relative paths get silently
      ignored on stage / rejected on info). No `chdir`.
- [x] **Listing:** `repository_dump(revision, path, max_depth)` streams tree nodes
      (`name`, `type_data` dir/`addr <hash>-<context>`, `size`, `flags`).
      `file_info` is per-path metadata only.
- [x] **Read:** `file_dump` returns metadata only; bytes come from
      `storage_open` (→ `handle_id`) + `storage_get` against `LoreAddress(hash, context)`.

Remaining (carried into Phase 1/3):
- [ ] `storage_get` item encoding — got `INVALID_ARGUMENTS`; find missing field
      (`partition`/`local_cache`/address form). Disk-materialization fallback works.
- [ ] Branch→revision resolution format (`revision`/`branch` args; `revision_find`).
- [ ] `offline` behavior on un-materialized fragments; `storage_close` lifecycle.

**Exit:** ✅ list a dir (`repository_dump`) and round-trip write→commit→read from
Python, end to end, against a local server. In-memory `storage_get` read pending
one arg detail.

## Phase 1 — Read-only `LoreFileSystem` (MVP, AsyncFileSystem) ✅
- [x] async `_lore.run()` wrapper (`collect_async`) + `run_sync()` for init-time
      calls; `errors.LoreError` mapping (`ADDRESS_NOT_FOUND`→`FileNotFoundError`,
      `INVALID_ARGUMENTS`→`ValueError`).
- [x] `__init__` (path/fo/ref/offline/identity/asynchronous/loop), `self._lore`,
      default-ref resolution via `branch_list` (`is_current`). `offline=False`
      default; `fo` = clone-on-init when `path` has no clone (`repository_clone`),
      else alias.
- [x] `_strip_protocol`, `_get_kwargs_from_urls` (`lore://path:ref@inner`) — mirror
      `GitFileSystem` verbatim (incl. its degenerate-URL quirks, see `_refs.py`).
- [x] Coroutines `_info` (via `file_info`), `_ls` (via `repository_dump`,
      `max_depth=2`; child names rebuilt from the requested dir + node basename
      since dump names are relative to the dumped path's **parent**), `_cat_file`
      (disk-materialized read), `ukey`; `_open` (MemoryFile) for the sync path.
- [x] Entry point registration in `pyproject.toml` (`fsspec.specs`).
- [x] Tests: ref parsing + error mapping (unit, no server); ls/info/cat/find/url
      round-trip + transaction (integration, gated on `loreserver` health). 28 pass.

**Done:** `fsspec.open("lore://…")`, `fsspec.filesystem("lore").ls(...)`, ranged
`cat_file`, `find`, and a validated write transaction all work end-to-end against
a local `loreserver`. Phase 1 shipped a disk-materialized `_cat_file`; the in-store
`storage_get` path was pinned down in Phase 2 (see below).

> **`storage_get` finding — RESOLVED in Phase 2 (the Phase 1 diagnosis was
> wrong).** There is **no FFI address-marshalling bug.** The empty-echoed
> `LoreAddress` is simply what a *failed* item-complete returns; on success the
> real address is echoed back. The actual Phase 1 failure was two missing inputs
> plus a wrong store handle:
>
> 1. **`partition` = the repository id** (`repository_info` →
>    `LoreRepositoryDataEventData.id`). `file_info` gives only `hash`/`context`,
>    never the partition; the zero/default partition rejects with
>    `INVALID_ARGUMENTS`.
> 2. **`address` = `LoreAddress(hash, context)`** from `file_info` — marshalls
>    fine once (1) is correct.
> 3. **The store handle must be remote-capable** (`storage_open` with
>    `has_remote_config=True` + `LoreStorageRemoteConfig(remote_url=…)`).
>    Committed payloads live in the **server-of-record**; the local immutable
>    store often holds only metadata (`immutable_query` shows `payload=0 remote=0`
>    locally, `payload=1 remote=1`). A local-only handle therefore returns
>    `ADDRESS_NOT_FOUND` (error_code 2). A remote-capable handle lazily fetches
>    and the read succeeds — exactly the `offline=False` design intent.

## Phase 2 — Robustness & ergonomics
- [x] **In-store `storage_get` read path.** `_cat_file` now reads content-addressed
      fragments from the store (`repository_info` partition + `file_info` address +
      remote-capable `storage_open`), reassembling `STORAGE_GET_DATA` payloads.
      Enables sparse / other-ref reads with no materialized working copy; under
      `offline=False` it lazily fetches. A working-copy fast path still serves the
      checked-out ref straight from disk; `offline=True` falls back to a disk copy
      or raises a clear "not resident" error. Verified: read after deleting the
      working-copy file returns the committed bytes.
- [x] Recursive `ls`/`find`/`walk`/`glob` correctness on nested trees (child names
      rebuilt from requested dir + node basename; `find` verified).
- [x] Ranged reads (`cat_file` start/end) verified with a random-access pattern
      (the pyarrow/zarr access shape: multiple non-contiguous slices + `cat_ranges`)
      over the in-store path. pyarrow/zarr themselves aren't installed in the local
      `.venv`, so the verification is synthetic, not a real parquet/zarr round-trip.
      (Ranged slicing works today; the store still returns whole content sliced
      client-side — true byte-range requests are a later optimization.)
- [x] `open_async` → `LoreAsyncStreamedFile(AbstractAsyncStreamedFile)` whose
      `_fetch_range(start, end)` maps to `_cat_file` byte ranges (large-file
      streaming, no whole-file buffering; `size` passed in, `cache_type="none"`).
- [x] Concurrent `_cat`/`cat_ranges` fan-out validated under the async loop. The
      lazy store-handle / `repository_info` init is now double-checked under
      `asyncio.Lock`s (`_store_lock`, `_repo_lock`) so a read fan-out can't
      race-open multiple handles (handle leak) or duplicate `repository_info`.
- [x] Lifecycle: context manager + `close()` / `storage_close()` cleanup (handle
      opened lazily on first in-store read, cached, released on close / `__exit__`).
- [x] Caching review. Instance `cachable` keyed by `(path, ref)`; `ukey` returns
      the Lore content address (`info["hash"]`), which is ref-independent for a
      given blob and matches `GitFileSystem.ukey` (ref-pinned per instance). No
      change needed — recorded as reviewed.
- [x] Ref resolution. `revision` accepts a **revision id** (full or hex prefix),
      **not** a branch name (`"main"` → `revision not found`) or a decimal number.
      A branch name is resolved to its tip via `branch_list` → entry `.latest`
      (`_resolve_rev`/`_branch_tip`); empty / the default ref → `""` (working
      copy). `revision_find(number=N)` also yields a tip (`.signature`) if needed.
- [x] Explicit `fs.fetch(ref)` → `revision_sync(revision=<resolved>)`, the
      `git fetch` analogue. **Finding (validated):** `revision_sync` advances the
      local clone and makes the ref's **tree/metadata** local, but does **not**
      pull content fragments into the offline-readable local store — even an
      online lazy `storage_get` doesn't persist into the local immutable store
      (offline `storage_get` of un-checked-out content still returns
      `ADDRESS_NOT_FOUND`). The only way to make *content* local is to check it
      out to the working copy (`revision_sync(reset=True)`, which is destructive).
      So `fetch` is documented as a tree/revision sync; offline content
      pre-materialization via fragments is not offered (would need working-copy
      checkout). `offline=True` reads of non-resident content remain a clear
      `FileNotFoundError` (verified).

## Phase 3 — Write support via `LoreTransaction` (opt-in) ✅
- [x] `LoreTransaction(Transaction)` with `start`/`complete(commit)`, recording the
      paths staged in the transaction (`_staged`).
- [x] `transaction_type = LoreTransaction`; `with fs.transaction(message=, metadata=)`.
      **Finding:** fsspec exposes `fs.transaction` as a *property* (lazily builds the
      `Transaction` so `open(..., "wb")` can reach `.files`), so we could not also
      define `transaction` as a method without shadowing it and breaking
      `open("wb")` (`self.transaction.files` → `AttributeError`). Resolved by making
      `LoreTransaction` **callable**: the property returns it, then `(message=…)`
      records the message/metadata and returns `self` as the context manager.
- [x] Writable `_open("wb")` (→ `LoreBufferedWriter`, an in-memory buffer that
      `pipe_file`s its bytes on close) / `_pipe_file` / `_put_file`. **Authoring is
      ordinary file I/O into the working copy + `file_stage(scan=True)`** — Lore's
      `file_write` is for *materializing store content to disk*, not authoring (its
      args are `address`/`path`/`output`), so it is not used for writes.
- [x] `complete(commit=True)` → single `revision_commit` (+ `branch_push` unless
      `offline`); `commit=False` → **exact rollback**: `file_unstage` then
      `file_reset(purge=True)` over the staged paths. **Finding:** `file_reset`
      errors on a *staged* node ("Failed to reset staged node"), so you must
      `file_unstage` first; then `file_reset(purge=True)` restores edited tracked
      files to committed content **and** purges newly-added files. Validated for
      tracked-only, new-only, and mixed batches. Drives coros via `fsspec.asyn.sync`.
- [x] `rm`/`mv` mapped to working-copy ops + `file_stage(scan=True)`: `_rm_file` =
      `os.remove` + stage (a staged deletion); `mv` = `os.rename` + stage of both
      old & new paths. **Finding:** the seemingly-matching Lore ops were rejected —
      `file_obliterate` is a destructive store-level purge (not a tracked tree
      removal), and `file_dirty_move` errors on repo-relative paths and silently
      no-ops on absolute ones. The disk-op + scan approach is consistent with how
      `_pipe_file` already authors content, and commits removals/renames as part of
      the same revision.
- [x] Guardrails: default read-only (`writable=False`, like `GitFileSystem`); every
      mutation goes through `_require_write()`, which raises `PermissionError`
      unless `writable=True` and `ValueError` unless a transaction is open (so writes
      always land as exactly one atomic revision).
- [x] Tests (against the live server): multi-file commit = one revision; exception
      path leaves no revision and restores tracked / purges new files; `open("wb")`,
      `put_file`, `rm`, `mv`; read-only and no-transaction guardrails. 47 pass.

## Phase 4 — Packaging & docs
- [ ] README quickstart + usage examples.
- [ ] Publish to PyPI (`lore-fsspec`); platform notes for `liblore` wheels.
- [ ] Optional `universal_pathlib` registration.
- [ ] Upstream-friendliness: shape API to ease a future contribution to
      `fsspec/filesystem_spec` if desired.

## Considered & dropped
- **`LoreStorageFileSystem`** (content-addressed, hash-keyed view over
  `storage_*`): redundant with `LoreFileSystem` and a poor fit for fsspec's
  path-oriented model. The `storage_*` APIs are reused internally for streaming
  reads instead of being exposed as a second filesystem.

## Risks / watch items
- `liblore` is platform-specific (only arm64 macOS dylib present locally); CI
  must guard on availability.
- `lore-vcs` is young (0.8.x) — event shapes/args may shift; isolate all of it
  behind `_lore.py`.
- Lazy materialization means "read" can hit the network; surface `offline`
  behavior clearly to avoid surprising stalls/errors.
