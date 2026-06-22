"""``LoreFileSystem``: an fsspec ``AsyncFileSystem`` over Lore (Epic's VCS).

The Lore analogue of fsspec's built-in ``GitFileSystem``. It attaches to a local
Lore clone and reads its tree at a given ref; "remote" is just lazy
materialization through that clone (gated by ``offline``). ``lore`` exposes a
genuine coroutine API, so this is an ``AsyncFileSystem``: we implement the
underscore coroutines (``_ls``/``_info``/``_cat_file``/...) and let fsspec
synthesize the blocking API.

Path model (validated against a live server):

* In-fs paths are **repository-relative** (e.g. ``Content/Game.ini``), exactly
  like ``GitFileSystem``. The clone directory is a constructor arg, not part of
  the in-fs path.
* ``lore`` resolves OS file paths against the **process CWD**, not against
  ``LoreGlobalArgs.repository_path``. We therefore always pass **absolute** paths
  (``os.path.join(clone_root, inner)``) to file ops, and never ``chdir``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Self

import asyncio
import contextlib
import datetime
import io
from pathlib import Path

import anyio
import fsspec.asyn
import lore
from fsspec.asyn import AbstractAsyncStreamedFile, AsyncFileSystem
from fsspec.implementations.memory import MemoryFile
from lore.types import LoreAddress
from lore.types.args import (
    LoreBranchCreateArgs,
    LoreBranchListArgs,
    LoreBranchMergeAbortArgs,
    LoreBranchMergeStartArgs,
    LoreBranchPushArgs,
    LoreBranchSwitchArgs,
    LoreFileInfoArgs,
    LoreFileResetArgs,
    LoreFileStageArgs,
    LoreFileUnstageArgs,
    LoreGlobalArgs,
    LoreRepositoryCloneArgs,
    LoreRepositoryDumpArgs,
    LoreRepositoryInfoArgs,
    LoreRevisionCommitArgs,
    LoreRevisionSyncArgs,
    LoreStorageCloseArgs,
    LoreStorageGetArgs,
    LoreStorageGetItem,
    LoreStorageOpenArgs,
    LoreStorageRemoteConfig,
)
from lore.types.enums import LoreErrorCode
from lore.types.events import (
    LoreBranchListEntryEventData,
    LoreBranchMergeConflictFileEventData,
    LoreFileInfoEventData,
    LoreRepositoryDataEventData,
    LoreRepositoryStateDumpNodeEventData,
    LoreRevisionSyncTargetEventData,
    LoreStorageGetDataEventData,
    LoreStorageGetItemCompleteEventData,
    LoreStorageOpenedEventData,
)

from . import _lore, _refs
from .errors import LoreError
from .transaction import LoreTransaction


class LoreFileSystem(AsyncFileSystem):
    """An fsspec filesystem backed by a local Lore VCS clone."""

    protocol = "lore"
    cachable = True  # instances cached by (path, ref), like GitFileSystem
    root_marker = ""

    transaction_type = LoreTransaction

    def __init__(
        self,
        path: str | None = None,
        fo: str | None = None,
        ref: str | None = None,
        *,
        offline: bool = False,
        identity: str | None = None,
        writable: bool = False,
        asynchronous: bool = False,
        loop: object = None,
        **kwargs: object,
    ) -> None:
        """Attach to a local Lore clone, optionally cloning from ``fo`` first."""
        super().__init__(asynchronous=asynchronous, loop=loop, **kwargs)
        self._lore = lore.Lore()
        self.identity = identity
        self.offline = offline
        # Read-only by default (like GitFileSystem). Writes additionally require
        # an open transaction — see ``_require_write`` — so every mutation lands
        # as one atomic Lore revision.
        self.writable = writable
        # Lazily-resolved content-store state (see ``_storage_get``). The locks
        # guard the lazy one-time init so a concurrent read fan-out (``_cat`` /
        # ``cat_ranges``) can't open two handles / race two ``repository_info``
        # calls. ``asyncio.Lock`` binds to the running loop on first use, so it
        # is safe to build here even though ``__init__`` runs off the loop.
        self._repo_data: LoreRepositoryDataEventData | None = None
        self._store_handle: int | None = None
        self._repo_lock = asyncio.Lock()
        self._store_lock = asyncio.Lock()

        clone_root = str(Path(path or fo or Path.cwd()).resolve())
        # Bootstrap: if `path` has no clone yet and `fo` is a Lore server URL,
        # clone it once; otherwise `fo` is just an alias for `path`.
        if fo and not _is_lore_clone(clone_root):
            if str(fo).startswith(_refs.PREFIX):
                clone_root = str(Path(path or Path.cwd()).resolve())
                self._clone(fo, clone_root)
            else:
                clone_root = str(Path(fo).resolve())
        self.clone_root = clone_root

        self.ref = ref or self._default_ref()

    # ------------------------------------------------------------------ setup
    def _gargs(self) -> LoreGlobalArgs:
        """A fresh base ``LoreGlobalArgs`` for the clone (cheap to build)."""
        return LoreGlobalArgs(
            repository_path=self.clone_root,
            identity=self.identity or "",
            offline=self.offline,
        )

    def _clone(self, url: str, dest: str) -> None:
        Path(dest).mkdir(parents=True, exist_ok=True)
        gargs = LoreGlobalArgs(repository_path=dest, identity=self.identity or "")
        _lore.run_sync(
            self._lore.repository_clone,
            gargs,
            LoreRepositoryCloneArgs(repository_url=url),
        )

    def _default_ref(self) -> str:
        """Resolve the clone's current branch (the ``is_current`` entry)."""
        try:
            entries = _lore.run_sync(
                self._lore.branch_list,
                self._gargs(),
                LoreBranchListArgs(),
                entry_type=LoreBranchListEntryEventData,
            )
        except LoreError:
            return ""
        for e in entries:
            if getattr(e, "is_current", False):
                return e.name
        return ""

    # ---------------------------------------------------------------- helpers
    @classmethod
    def _strip_protocol(cls, path: str) -> str:
        path = super()._strip_protocol(path)
        return _refs.inner_path(path)

    @staticmethod
    def _get_kwargs_from_urls(path: str) -> dict:
        return _refs.split_url(path)

    def _abs(self, inner: str) -> str:
        """Map a repo-relative inner path to an absolute working-copy path."""
        inner = inner.strip("/")
        return str(Path(self.clone_root) / inner) if inner else self.clone_root

    async def _resolve_rev(self, ref: str | None) -> str:
        """Translate a ref into the ``revision`` arg the ``lore`` commands want.

        Lore's ``revision`` field accepts a **revision id** (full or hex prefix),
        *not* a branch name — passing ``"main"`` errors with ``revision not
        found``. So we resolve:

        * empty / the instance's default ref → ``""`` (current working copy /
          branch tip; the cheap no-resolution path),
        * a known branch name → that branch's tip revision (``latest``),
        * anything else → passed straight through (assumed a revision id/hex).
        """
        if not ref or ref == self.ref:
            return ""
        tip = await self._branch_tip(ref)
        return tip if tip is not None else ref

    async def _branch_tip(self, name: str) -> str | None:
        """Tip revision (hex) of branch ``name``, or ``None`` if no such branch.

        ``branch_list`` can report a branch twice (local + remote tracking); we
        prefer the ``is_current`` entry and fall back to the first name match.
        """
        entries = await self._run(
            self._lore.branch_list,
            LoreBranchListArgs(),
            entry_type=LoreBranchListEntryEventData,
        )
        match = None
        for e in entries:
            if e.name == name:
                if getattr(e, "is_current", False):
                    return e.latest.hex()
                if match is None:
                    match = e
        return match.latest.hex() if match is not None else None

    @staticmethod
    def _node_info(node: LoreRepositoryStateDumpNodeEventData, name: str) -> dict:
        """Translate a ``repository_dump`` node into an fsspec info dict.

        ``name`` is the full repository-relative path the caller computes;
        ``repository_dump`` reports ``node.name`` relative to the dumped path's
        parent, so we cannot use it as the fsspec path directly.
        """
        td = node.type_data or ""
        is_dir = td.startswith("child")
        info = {
            "name": name,
            "size": node.size,
            "type": "directory" if is_dir else "file",
        }
        if not is_dir and td.startswith("addr "):
            info["hash"] = td[len("addr ") :].split("-", 1)[0]
        return info

    # ------------------------------------------------------------------ reads
    async def _ls(
        self,
        path: str,
        *,
        detail: bool = True,
        ref: str | None = None,
        **_kwargs: object,
    ) -> list:
        inner = self._strip_protocol(path)
        abspath = self._abs(inner)
        rev = await self._resolve_rev(ref)
        nodes = await self._run(
            self._lore.repository_dump,
            LoreRepositoryDumpArgs(revision=rev, path=abspath, max_depth=2),
            entry_type=LoreRepositoryStateDumpNodeEventData,
        )
        if not nodes:
            raise FileNotFoundError(path)

        root = nodes[0]
        base = inner.strip("/")
        if not (root.type_data or "").startswith("child"):
            # `path` is a file: ls returns the file's own info.
            out = [self._node_info(root, base)]
        else:
            out = [
                self._node_info(n, _join(base, Path(n.name.rstrip("/")).name))
                for n in nodes
                if n.id != root.id and n.parent == root.id
            ]
        if detail:
            return out
        return sorted(i["name"] for i in out)

    async def _info(self, path: str, ref: str | None = None, **_kwargs: object) -> dict:
        inner = self._strip_protocol(path)
        if inner in ("", "/"):
            return {"name": "", "size": 0, "type": "directory"}
        abspath = self._abs(inner)
        rev = await self._resolve_rev(ref)
        evs = await self._run(
            self._lore.file_info,
            LoreFileInfoArgs(paths=[abspath], revision=rev),
            entry_type=LoreFileInfoEventData,
        )
        if not evs:
            raise FileNotFoundError(path)
        ev = evs[0]
        return {
            "name": inner,
            "size": ev.size,
            "type": "directory" if ev.is_dir else "file",
            "hash": ev.hash.hex(),
            "context": ev.context.hex(),
            "mode": ev.mode,
            "local_size": ev.local_size,
        }

    async def _cat_file(
        self,
        path: str,
        start: int | None = None,
        end: int | None = None,
        ref: str | None = None,
        **_kwargs: object,
    ) -> bytes:
        """Read file bytes.

        Two read paths, picked by what's cheapest and correct:

        * **Working-copy fast path** — when reading the checked-out ref and the
          file is fully materialized on disk (``local_size == size``), read the
          bytes straight off the clone. No network, no store handle.
        * **In-store path** (``storage_get``) — for any other ref, or content not
          on disk, read the content-addressed fragment from the store. With
          ``offline=False`` the store handle lazily fetches missing fragments
          from the server-of-record; under ``offline=True`` a non-resident
          fragment errors instead (then we fall back to a disk copy if present).
        """
        inner = self._strip_protocol(path)
        info = await self._info(path, ref=ref)
        if info["type"] != "file":
            raise IsADirectoryError(path)
        abspath = self._abs(inner)

        on_disk = await anyio.Path(abspath).is_file()
        if (
            await self._resolve_rev(ref) == ""
            and on_disk
            and info.get("local_size") == info.get("size")
        ):
            return (await anyio.Path(abspath).read_bytes())[start:end]

        hash_b = bytes.fromhex(info["hash"])
        if not any(hash_b):  # default/zero hash == empty content
            return b""
        try:
            data = await self._storage_get(hash_b, bytes.fromhex(info["context"]))
        except FileNotFoundError:
            if on_disk:
                return (await anyio.Path(abspath).read_bytes())[start:end]
            if self.offline:
                msg = (
                    f"{path!r}: content fragment is not resident locally and "
                    f"offline=True; run fs.fetch(...) or construct the filesystem "
                    f"with offline=False to allow lazy fetching"
                )
                raise FileNotFoundError(
                    msg,
                ) from None
            raise
        return data[start:end]

    # ----------------------------------------------------------- content store
    async def _repo_info(self) -> LoreRepositoryDataEventData:
        """Cache the repository metadata (id == storage partition; remote URL).

        Double-checked under ``_repo_lock`` so a concurrent read fan-out issues
        ``repository_info`` once, not once per in-flight read.
        """
        if self._repo_data is None:
            async with self._repo_lock:
                if self._repo_data is None:
                    evs = await self._run(
                        self._lore.repository_info,
                        LoreRepositoryInfoArgs(),
                        entry_type=LoreRepositoryDataEventData,
                    )
                    if not evs:
                        raise LoreError(None, "repository_info returned no data")
                    self._repo_data = evs[0]
        return self._repo_data

    async def _storage(self) -> int:
        """Open (once) and cache a content-store handle for this filesystem.

        Remote-capable unless ``offline``: committed payloads live in the
        server-of-record (the local immutable store often holds only metadata),
        so a local-only handle cannot serve them.

        Double-checked under ``_store_lock`` so concurrent first reads can't each
        open a handle and leak all but the last. The ``_repo_info`` lookup (its
        own lock) is done *before* taking ``_store_lock`` to avoid lock nesting.
        """
        if self._store_handle is None:
            url = None if self.offline else (await self._repo_info()).remote_url
            async with self._store_lock:
                if self._store_handle is None:
                    if self.offline:
                        args = LoreStorageOpenArgs(repository_path=self.clone_root)
                    else:
                        args = LoreStorageOpenArgs(
                            repository_path=self.clone_root,
                            remote_config=LoreStorageRemoteConfig(remote_url=url),
                            has_remote_config=True,
                        )
                    evs = await self._run(
                        self._lore.storage_open,
                        args,
                        entry_type=LoreStorageOpenedEventData,
                    )
                    if not evs:
                        raise LoreError(None, "storage_open returned no handle")
                    self._store_handle = evs[0].handle_id
        return self._store_handle

    async def _storage_get(self, hash_b: bytes, context_b: bytes) -> bytes:
        """Read one content-addressed fragment, reassembled into bytes.

        Raises ``FileNotFoundError`` when the address is not resident (and, when
        online, could not be fetched) so callers can fall back / surface clearly.
        """
        handle = await self._storage()
        partition = (await self._repo_info()).id
        item = LoreStorageGetItem(
            id=1,
            partition=partition,
            address=LoreAddress(hash=hash_b, context=context_b),
            streaming=False,
        )
        # check=False: inspect the per-item completion ourselves so a single
        # "not found" maps to FileNotFoundError instead of a blanket LoreError.
        events = await _lore.run(
            self._lore.storage_get,
            self._gargs(),
            LoreStorageGetArgs(handle=handle, items=[item]),
            check=False,
        )
        for ev in events:
            if isinstance(ev, LoreStorageGetItemCompleteEventData):
                code = LoreErrorCode(int(ev.error_code))
                if code == LoreErrorCode.ADDRESS_NOT_FOUND:
                    raise FileNotFoundError(hash_b.hex())
                if code != LoreErrorCode.NONE:
                    raise LoreError(code, "storage_get failed")
        buf = bytearray()
        for ev in events:
            if isinstance(ev, LoreStorageGetDataEventData):
                buf[ev.offset : ev.offset + len(ev.bytes)] = ev.bytes
        return bytes(buf)

    def ukey(self, path: str) -> str:
        """Stable cache key = the Lore content address (mirrors GitFileSystem)."""
        return self.info(path)["hash"]

    def modified(self, path: str) -> datetime.datetime:
        """Modification time of a path, as a UTC ``datetime``.

        Lore is content-addressed: a path's bytes are immutable for a given
        revision, so any value that changes when the content changes is correct
        for cache-invalidation callers (notably DuckDB's fsspec integration,
        which *requires* ``modified`` and otherwise raises ``NotImplementedError``).
        We report the working-copy file's mtime when the content is materialized
        on disk, and fall back to the epoch for content that lives only in the
        store.
        """
        abspath = self._abs(self._strip_protocol(path))
        try:
            ts = Path(abspath).stat().st_mtime
        except OSError:
            ts = 0.0
        return datetime.datetime.fromtimestamp(ts, tz=datetime.UTC)

    def _open(
        self,
        path: str,
        mode: str = "rb",
        _block_size: int | None = None,
        _autocommit: bool = True,  # noqa: FBT001, FBT002
        _cache_options: dict | None = None,
        ref: str | None = None,
        **_kwargs: object,
    ) -> MemoryFile | LoreBufferedWriter:
        """Open a file for reading or writing."""
        if mode == "rb":
            data = self.cat_file(path, ref=ref)
            return MemoryFile(self, path, data)
        if mode in ("wb", "xb"):
            self._require_write()
            if mode == "xb" and self.exists(path):
                raise FileExistsError(path)
            return LoreBufferedWriter(self, path)
        msg = f"unsupported mode {mode!r}; use 'rb' or 'wb'"
        raise NotImplementedError(msg)

    async def open_async(
        self,
        path: str,
        mode: str = "rb",
        ref: str | None = None,
        **_kwargs: object,
    ) -> LoreAsyncStreamedFile:
        """Async streaming reads — the large-asset path for Lore.

        Returns an :class:`AbstractAsyncStreamedFile` whose ``_fetch_range`` maps
        to ``_cat_file`` byte ranges, so a consumer can ``seek``/``read`` chunks
        of a multi-GB asset without buffering the whole blob (as ``_open`` /
        ``MemoryFile`` does). Read-only, mirroring ``_open``.
        """
        if mode != "rb":
            msg = (
                "open_async supports read-only 'rb'; writes go through "
                "fs.transaction(...) + pipe_file"
            )
            raise NotImplementedError(
                msg,
            )
        info = await self._info(path, ref=ref)
        if info["type"] != "file":
            raise IsADirectoryError(path)
        return LoreAsyncStreamedFile(self, path, info["size"], ref)

    # ----------------------------------------------------------------- writes
    def _require_writable(self) -> None:
        """Guard repo mutations that are atomic on their own (branch/merge).

        Read-only unless ``writable``. Unlike file writes these don't need an open
        transaction: ``create_branch``/``merge`` are each a single self-contained
        Lore operation, not a batch staged across several calls.
        """
        if not self.writable:
            msg = (
                "LoreFileSystem is read-only; construct it with writable=True to "
                "enable writes"
            )
            raise PermissionError(
                msg,
            )

    def _require_write(self) -> None:
        """Guard every file write: read-only unless ``writable`` + an open txn.

        Lore's write model is *stage → commit a revision*; a stage with no commit
        leaves the working copy in a half-applied state. We therefore require an
        open :class:`LoreTransaction` so the batch always lands as exactly one
        atomic revision (see ``LoreTransaction.complete``).
        """
        self._require_writable()
        if not getattr(self, "_intrans", False):
            msg = (
                "writes must occur inside a transaction so they commit as one "
                "atomic revision; use `with fs.transaction(message=...): ...`"
            )
            raise ValueError(
                msg,
            )

    def _stage(self, abspath: str) -> None:
        """Record an absolute path as touched by the current transaction."""
        if getattr(self, "_intrans", False) and self._transaction is not None:
            self._transaction.record_staged(abspath)

    async def _pipe_file(self, path: str, value: bytes, **_kwargs: object) -> None:
        """Author bytes into the working copy and stage them.

        Authoring is ordinary file I/O into the clone (Lore's ``file_write`` is
        for materializing store content, not authoring); we then ``file_stage``
        the absolute path. The commit happens on transaction exit.
        """
        self._require_write()
        inner = self._strip_protocol(path)
        abspath = self._abs(inner)
        parent = anyio.Path(abspath).parent
        if parent.name:
            await parent.mkdir(parents=True, exist_ok=True)
        await anyio.Path(abspath).write_bytes(value)
        await self._run(
            self._lore.file_stage,
            LoreFileStageArgs(paths=[abspath], scan=True),
        )
        self._stage(abspath)

    async def _put_file(
        self,
        lpath: str,
        rpath: str,
        _mode: str = "overwrite",
        **_kwargs: object,
    ) -> None:
        """Copy a local file into the working copy and stage it."""
        data = await anyio.Path(lpath).read_bytes()
        await self._pipe_file(rpath, data)

    async def _rm_file(self, path: str, **_kwargs: object) -> None:
        """Remove a file from the tree: delete on disk, then stage the deletion.

        Mirrors the working-copy model used everywhere else here — Lore's
        ``file_stage(scan=True)`` over the (now-absent) path records a deletion
        that the transaction's commit folds into the revision. (``file_obliterate``
        is a destructive store-level purge, not a tracked tree removal, so it is
        intentionally not used.)
        """
        self._require_write()
        inner = self._strip_protocol(path)
        abspath = self._abs(inner)
        if await anyio.Path(abspath).is_file():
            await anyio.Path(abspath).unlink()
        await self._run(
            self._lore.file_stage,
            LoreFileStageArgs(paths=[abspath], scan=True),
        )
        self._stage(abspath)

    def mv(
        self,
        path1: str,
        path2: str,
        _recursive: bool = False,  # noqa: FBT001, FBT002
        _maxdepth: int | None = None,
        **_kwargs: object,
    ) -> None:
        """Rename within the working copy, staged as part of the open transaction.

        Done as an on-disk ``os.rename`` plus a ``file_stage(scan=True)`` of both
        the old and new paths (so the scan records the removal and the addition),
        consistent with how ``_pipe_file``/``_rm_file`` work. Lore's
        ``file_dirty_move`` was rejected: it errors on repo-relative paths and
        silently no-ops on absolute ones.
        """
        fsspec.asyn.sync(self.loop, self._mv_async, path1, path2)

    async def _mv_async(self, path1: str, path2: str) -> None:
        self._require_write()
        src = self._abs(self._strip_protocol(path1))
        dst = self._abs(self._strip_protocol(path2))
        parent = anyio.Path(dst).parent
        if parent.name:
            await parent.mkdir(parents=True, exist_ok=True)
        await anyio.Path(src).rename(dst)
        await self._run(
            self._lore.file_stage,
            LoreFileStageArgs(paths=[src, dst], scan=True),
        )
        self._stage(src)
        self._stage(dst)

    def _commit_revision(
        self,
        message: str | None,
        metadata: dict | None = None,
    ) -> None:
        fsspec.asyn.sync(
            self.loop,
            self._commit_revision_async,
            message,
            metadata,
        )

    async def _commit_revision_async(
        self,
        message: str | None,
        _metadata: dict | None,
    ) -> None:
        await self._run(
            self._lore.revision_commit,
            LoreRevisionCommitArgs(message=message or ""),
        )
        if not self.offline:
            await self._run(self._lore.branch_push, LoreBranchPushArgs())

    def _reset_paths(self, paths: list[str]) -> None:
        if paths:
            fsspec.asyn.sync(self.loop, self._reset_async, paths)

    async def _reset_async(self, paths: list[str]) -> None:
        """Exact rollback of staged paths (transaction abort).

        ``file_reset`` errors on a *staged* node, so first ``file_unstage`` to
        drop the staging entries, then ``file_reset(purge=True)``: tracked files
        are restored to their committed content and newly-added files (absent
        from the revision) are purged from disk. Validated against the live
        server for both cases, including a mixed batch.
        """
        await self._run(self._lore.file_unstage, LoreFileUnstageArgs(paths=paths))
        await self._run(
            self._lore.file_reset,
            LoreFileResetArgs(paths=paths, purge=True),
        )

    # --------------------------------------------------------------- fetch
    def fetch(self, ref: str | None = None) -> list[int]:
        """Sync a ref's revision/tree from the server-of-record (``git fetch``).

        Wraps ``revision_sync``: it advances the local clone to the ref's
        revision and makes that revision's **tree/metadata** local, so
        ``info``/``ls``/ref-resolution work without the network afterward.

        Caveat (validated against the live server): ``revision_sync`` does **not**
        pull file *content* fragments into the offline-readable local store —
        committed payloads stay in the server-of-record and are still fetched
        lazily on read (so reads under ``offline=True`` of never-materialized
        content remain a ``FileNotFoundError``; materializing content means
        checking it out to the working copy). Returns the synced target revision
        numbers.
        """
        return fsspec.asyn.sync(self.loop, self._fetch, ref)

    async def _fetch(self, ref: str | None) -> list[int]:
        rev = await self._resolve_rev(ref)
        targets = await self._run(
            self._lore.revision_sync,
            LoreRevisionSyncArgs(revision=rev),
            entry_type=LoreRevisionSyncTargetEventData,
        )
        return [t.target_revision_number for t in targets]

    # --------------------------------------------------------------- branches
    def branches(self) -> list[str]:
        """Names of the clone's branches (de-duplicated, sorted).

        ``branch_list`` can report a branch twice (local + remote tracking); we
        collapse those, since callers want the set of branch names.
        """
        entries = fsspec.asyn.sync(
            self.loop,
            self._run,
            self._lore.branch_list,
            LoreBranchListArgs(),
            entry_type=LoreBranchListEntryEventData,
        )
        return sorted({e.name for e in entries})

    def create_branch(self, name: str, *, checkout: bool = False) -> None:
        """Create a branch at the current branch tip (like ``git branch``).

        Repo topology is deliberately *not* a transaction concern (transactions
        batch file writes into one revision); branching is its own atomic op,
        exposed as a method like :meth:`fetch`. With ``checkout=True`` the new
        branch is also switched to, so subsequent writes/commits land on it.
        """
        self._require_writable()
        fsspec.asyn.sync(self.loop, self._create_branch, name, checkout=checkout)

    async def _create_branch(self, name: str, *, checkout: bool) -> None:
        await self._run(self._lore.branch_create, LoreBranchCreateArgs(branch=name))
        if checkout:
            await self._run(self._lore.branch_switch, LoreBranchSwitchArgs(branch=name))
            self.ref = name

    def switch_branch(self, name: str) -> None:
        """Check out branch ``name`` (updates the working copy and the fs's ref)."""
        self._require_writable()
        fsspec.asyn.sync(self.loop, self._switch_branch, name)

    async def _switch_branch(self, name: str) -> None:
        await self._run(self._lore.branch_switch, LoreBranchSwitchArgs(branch=name))
        self.ref = name

    def merge(self, source: str, message: str | None = None) -> None:
        """Merge branch ``source`` into the current branch as one revision.

        A clean merge is committed (and pushed when online) atomically — Lore's
        ``branch_merge_start`` performs the merge commit itself. A merge with
        conflicts is **aborted and raised** with the conflicting paths: we never
        auto-resolve, because Lore merge resolution is an explicit, stateful flow
        (start → resolve(-mine/-theirs) → commit) that has no safe default. Use
        the ``lore`` CLI to resolve, then re-run.
        """
        self._require_writable()
        fsspec.asyn.sync(self.loop, self._merge, source, message)

    async def _merge(self, source: str, message: str | None) -> None:
        msg = message or f"Merge {source} into {self.ref}"
        evs = await self._run(
            self._lore.branch_merge_start,
            LoreBranchMergeStartArgs(branch=source, message=msg),
        )
        conflicts = [
            e.path for e in evs if isinstance(e, LoreBranchMergeConflictFileEventData)
        ]
        if conflicts:
            await self._run(self._lore.branch_merge_abort, LoreBranchMergeAbortArgs())
            raise LoreError(
                None,
                f"merge of {source!r} into {self.ref!r} has conflicts in "
                f"{conflicts}; resolve with the lore CLI and re-run (auto-merge "
                f"is intentionally not performed)",
            )
        if not self.offline:
            await self._run(self._lore.branch_push, LoreBranchPushArgs())

    # ------------------------------------------------------------- lifecycle
    def close(self) -> None:
        """Release the cached content-store handle, if one was opened."""
        if self._store_handle is not None:
            handle, self._store_handle = self._store_handle, None
            fsspec.asyn.sync(self.loop, self._close_async, handle)

    async def _close_async(self, handle: int) -> None:
        await self._run(self._lore.storage_close, LoreStorageCloseArgs(handle=handle))

    def __enter__(self) -> Self:
        """Enter context manager."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Exit context manager and flush writes."""
        self.close()

    def __del__(self) -> None:
        """Destructor: close the store handle on GC."""
        with contextlib.suppress(Exception):
            self.close()

    # -------------------------------------------------------------- internals
    async def _run(
        self,
        command: object,
        cmd_args: object,
        *,
        entry_type: type | None = None,
    ) -> list:
        return await _lore.run(command, self._gargs(), cmd_args, entry_type=entry_type)


class LoreAsyncStreamedFile(AbstractAsyncStreamedFile):
    """Read-only async file streaming bytes from Lore's content store.

    ``size`` is passed in so the base class never does a sync ``info`` lookup on
    the loop thread; ``cache_type="none"`` keeps it a true stream (each ``read``
    pulls exactly its range via ``_fetch_range`` → ``_cat_file``).
    """

    def __init__(
        self,
        fs: LoreFileSystem,
        path: str,
        size: int,
        ref: str | None,
    ) -> None:
        """Initialize with filesystem, path, known size, and optional ref."""
        super().__init__(fs, path, mode="rb", size=size, cache_type="none")
        self._ref = ref

    async def _fetch_range(self, start: int, end: int) -> bytes:
        return await self.fs._cat_file(  # noqa: SLF001
            self.path,
            start=start,
            end=end,
            ref=self._ref,
        )


class LoreBufferedWriter(io.BytesIO):
    """Write-mode file object: buffer in memory, ``pipe_file`` on close.

    Lore authors content as ordinary file I/O followed by a stage, so a write
    is naturally whole-buffer: we accumulate the bytes and, on ``close``, hand
    them to ``fs.pipe_file`` (which stages them into the open transaction).
    """

    def __init__(self, fs: LoreFileSystem, path: str) -> None:
        """Initialize with a filesystem and the destination path."""
        super().__init__()
        self._fs = fs
        self._path = path
        self._committed = False

    def close(self) -> None:
        """Flush buffered bytes to the filesystem and close."""
        if not self._committed and not self.closed:
            self._committed = True
            self._fs.pipe_file(self._path, self.getvalue())
        super().close()

    def __enter__(self) -> Self:
        """Enter context manager."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Exit context manager and flush writes."""
        self.close()


def _is_lore_clone(path: str) -> bool:
    return (Path(path) / ".lore").is_dir()


def _join(base: str, leaf: str) -> str:
    """Join an fsspec (POSIX) repo-relative dir and a leaf name."""
    return f"{base}/{leaf}" if base else leaf
