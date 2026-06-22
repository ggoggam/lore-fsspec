## Context Transfer

### Summary
Phase 1 of `lore-fsspec` (an fsspec `AsyncFileSystem` over Epic's Lore VCS, the Lore analogue of `GitFileSystem`) is implemented, committed (`f4b1a8e` on branch `lore/init`), and opened as PR #126 (`lore/init` → `main` on `ggoggam/workspace`). Read-only MVP + a validated write transaction work end-to-end; 28 tests pass.

### Key Decisions
- `_cat_file` reads bytes directly from the **disk-materialized working copy** (`<clone_root>/<inner>`), not via `storage_get`. Reason: `storage_get` has a binding-level bug (see Traps); files report `local_size == size` so disk read is correct for a non-sparse working copy.
- The PR carries the **whole `lore-fsspec` project** (the prior `6a5538e` init/scaffolding commit + the new `f4b1a8e` Phase 1 commit), since `lore/init` branched off daytwo's `main` and both commits are cleanly scoped to `lore-fsspec/` only.
- `_ls` rebuilds each child's fsspec path as `<requested-dir>/<basename(node.name)>` rather than trusting `repository_dump` node names (see Traps).
- Rollback in `LoreTransaction` only **unstages** (no disk delete / no `file_reset`); exact restore semantics deferred to Phase 3 per design doc.
- URL/ref grammar mirrors `GitFileSystem` **verbatim**, including its parser quirks (deliberate — user said "stick with whatever Git has").

### Traps to Avoid
- **`storage_get` is NOT just a missing field.** Supplying `partition` clears the `invalid arguments:` classification, but `LoreStorageGetItemCompleteEventData` always echoes an EMPTY `LoreAddress(_hash=LoreHash(), _context=LoreContext())` regardless of the `hash`/`context` bytes passed in → the address is not marshalling across the FFI boundary. Do not waste time trying more `LoreStorageGetItem` field combos; the fix is in how `LoreAddress`/`LoreHash`/`LoreContext` wrap `input_` bytes vs. how the item array serializes them. Deferred to Phase 2.
- **`repository_dump` node `name` is relative to the dumped path's PARENT, not the repo root.** It only *looks* repo-relative for depth-1 dirs (e.g. `sub/`). At depth 2 you get `Config/Game.ini` for `Content/Config`. Always rebuild the path from the requested dir + node basename.
- **`repository_dump` shows the working tree incl. untracked/unstaged files**, so a rolled-back txn's file still appears in `ls`. Verify "not committed" via `revision_history` count, never via `ls`.
- **`lore` resolves file paths against process CWD, not `repository_path`.** Always pass absolute paths; never `chdir` (unsafe on fsspec's shared loop thread).
- Lore args are kwarg-constructed (`LoreGlobalArgs(repository_path=...)`); public attr names are read-only properties, not fluent setters.
- `LoreRepositoryDumpArgs(revision=, path=, max_depth=)` — `max_depth` is a constructor kwarg even though it's not a dataclass field.
- URL parser quirks (mirrored from Git): a ref is only captured as `:ref@inner` (a `:ref` with no `@` is dropped); a bare clone URL with no `:` yields `{}` from `_get_kwargs_from_urls`. Tests assert this behavior intentionally — don't "fix" it.

### Working Agreements
- User authorizes coding with brief "go"/"Yes" but expects clarification when a question is genuinely ambiguous (used AskUserQuestion to disambiguate "Yes").
- Commit/push only when asked; PR was explicitly requested. Workspace rule: PRs to `main` are always **squash-merged**.
- Leave unrelated dirty files outside `lore-fsspec/` alone (`../mise.*`, `../.claude/skills/...`).
- Validate against the live local `loreserver` during development.

### Relevant Files
- `lore-fsspec/src/lore_fsspec/core.py` — `LoreFileSystem(AsyncFileSystem)`: `__init__` (path/fo clone-on-init/ref/offline/identity), `_strip_protocol`/`_get_kwargs_from_urls`, `_ls` (repository_dump, max_depth=2, parent-id filter, basename rebuild), `_info` (file_info), `_cat_file` (disk read), `ukey`, `_open` (MemoryFile), `_pipe_file`, `transaction()`, `_commit_revision`/`_reset_paths`, `_run`. Module-level `_is_lore_clone`, `_join`.
- `lore-fsspec/src/lore_fsspec/transaction.py` — `LoreTransaction(Transaction)`: stage-on-write, one `revision_commit` (+ `branch_push` unless offline) on `complete(commit=True)`, unstage on rollback.
- `lore-fsspec/src/lore_fsspec/_lore.py` — `run` (async, `collect_async`) + `run_sync` (sync `collect`, for `__init__`-time clone/default-ref).
- `lore-fsspec/src/lore_fsspec/errors.py` — `LoreError`/`LoreFileNotFoundError`/`LoreInvalidArguments`; `raise_for_events` maps `LoreErrorCode` (ADDRESS_NOT_FOUND→FileNotFoundError, INVALID_ARGUMENTS→ValueError) + nonzero `LoreCompleteEventData.status`.
- `lore-fsspec/src/lore_fsspec/_refs.py` — `split_url` (`_get_kwargs_from_urls`), `inner_path` (`_strip_protocol`), `PROTOCOL`/`_PREFIX`.
- `lore-fsspec/src/lore_fsspec/__init__.py` — exports + `register_implementation("lore", ...)`.
- `lore-fsspec/pyproject.toml` — `fsspec.specs` entry point, hatchling build, pytest `integration` marker.
- `lore-fsspec/tests/conftest.py` — `lore_server`/`fixture_repo` fixtures (gated on liblore + health check), builds scratch repo with hello.txt/sub/data.bin/Content/Config/Game.ini.
- `lore-fsspec/tests/test_filesystem.py` — integration (ls/info/cat/find/url round-trip/transaction); `_revision_count` helper.
- `lore-fsspec/tests/test_refs.py`, `tests/test_errors.py` — unit (no server).
- `lore-fsspec/docs/02-design.md`, `docs/03-roadmap.md` — Phase 1 marked done, `storage_get` finding recorded; Phase 2/3 still open.

### Open Work
- PR #126 is open and awaiting review/merge (squash-merge). Not yet merged.
- Phase 2 has NOT been started. It depends on nothing being merged first but the user was deciding whether to branch it off `lore/init` now or off `main` after merge. Phase 2 scope: in-store `storage_get` read path (the FFI address-marshalling bug above), branch→revision resolution (numeric revision vs branch name in `revision`/`branch` args; possibly `revision_find`), `open_async`→`AbstractAsyncStreamedFile` streaming, `storage_open` handle reuse + `storage_close`/`shutdown` lifecycle, `fs.fetch(ref)` helper, caching/ukey-across-refs review.
- Phase 3 (richer write ergonomics: `rm`/`mv`, writable `_open("wb")`, exact rollback `file_reset`/metadata) and Phase 4 (packaging/PyPI) untouched.
- A linter reformatted several files post-commit (multi-line signatures in `_lore.py`/`transaction.py`, import grouping in tests, etc.) — these are uncommitted working-tree changes; whether to commit them is undecided.

### Prompt for New Chat
You are continuing work on `lore-fsspec`, an fsspec `AsyncFileSystem` for Epic Games' Lore VCS, located at `/Users/ggoggam/workspace/lore-fsspec/` (a subdirectory of the `ggoggam/workspace` meta-repo whose `origin` is `git@github.com:ggoggam/workspace.git`). Phase 1 (read-only MVP + write transaction) is complete, committed as `f4b1a8e` on branch `lore/init`, and opened as PR #126 against `main`; it is not yet merged. The backend is the `lore` Python package (PyPI `lore-vcs`, imports as `lore`), FFI bindings over Rust `liblore`. A local zero-config `loreserver` is the test dependency (ports 41337 gRPC/QUIC + 41339 HTTP; health at `http://127.0.0.1:41339/health_check`; repos at `lore://127.0.0.1:41337/<name>`); integration tests skip when it's unreachable. The design and roadmap live in `lore-fsspec/docs/`. Phase 2 (in-store `storage_get`, branch→revision resolution, `open_async` streaming, lifecycle) has not been started, and there is an open question of whether to branch it off `lore/init` or off `main` after PR #126 merges. A linter has reformatted some source/test files in the working tree since the commit.

Before responding, use the Read tool to read every file listed in "Relevant Files" above. Do not summarize, paraphrase, or claim you already have context. Actually read each file. Treat all claims in this handoff as context to verify against the code, not facts to trust blindly. Then wait for my instructions before taking any action.
