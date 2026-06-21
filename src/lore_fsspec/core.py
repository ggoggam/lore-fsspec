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

import asyncio
import os

import fsspec.asyn
from fsspec.asyn import AbstractAsyncStreamedFile, AsyncFileSystem
from fsspec.implementations.memory import MemoryFile

import lore
from lore.types import LoreAddress
from lore.types.args import (
    LoreBranchListArgs,
    LoreBranchPushArgs,
    LoreFileInfoArgs,
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
    protocol = "lore"
    cachable = True  # instances cached by (path, ref), like GitFileSystem
    root_marker = ""

    transaction_type = LoreTransaction

    def __init__(
        self,
        path: str | None = None,
        fo: str | None = None,
        ref: str | None = None,
        offline: bool = False,
        identity: str | None = None,
        asynchronous: bool = False,
        loop=None,
        **kwargs,
    ):
        super().__init__(asynchronous=asynchronous, loop=loop, **kwargs)
        self._lore = lore.Lore()
        self.identity = identity
        self.offline = offline
        # Lazily-resolved content-store state (see ``_storage_get``). The locks
        # guard the lazy one-time init so a concurrent read fan-out (``_cat`` /
        # ``cat_ranges``) can't open two handles / race two ``repository_info``
        # calls. ``asyncio.Lock`` binds to the running loop on first use, so it
        # is safe to build here even though ``__init__`` runs off the loop.
        self._repo_data: LoreRepositoryDataEventData | None = None
        self._store_handle: int | None = None
        self._repo_lock = asyncio.Lock()
        self._store_lock = asyncio.Lock()

        clone_root = os.path.abspath(path or fo or os.getcwd())
        # Bootstrap: if `path` has no clone yet and `fo` is a Lore server URL,
        # clone it once; otherwise `fo` is just an alias for `path`.
        if fo and not _is_lore_clone(clone_root):
            if str(fo).startswith(_refs._PREFIX):
                clone_root = os.path.abspath(path or os.getcwd())
                self._clone(fo, clone_root)
            else:
                clone_root = os.path.abspath(fo)
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
        os.makedirs(dest, exist_ok=True)
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
        except Exception:
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
        return os.path.join(self.clone_root, inner) if inner else self.clone_root

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
    async def _ls(self, path, detail=True, ref=None, **kwargs):
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
                self._node_info(n, _join(base, os.path.basename(n.name.rstrip("/"))))
                for n in nodes
                if n.id != root.id and n.parent == root.id
            ]
        if detail:
            return out
        return sorted(i["name"] for i in out)

    async def _info(self, path, ref=None, **kwargs):
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

    async def _cat_file(self, path, start=None, end=None, ref=None, **kwargs):
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

        on_disk = os.path.isfile(abspath)
        if (
            await self._resolve_rev(ref) == ""
            and on_disk
            and info.get("local_size") == info.get("size")
        ):
            with open(abspath, "rb") as f:
                return f.read()[start:end]

        hash_b = bytes.fromhex(info["hash"])
        if not any(hash_b):  # default/zero hash == empty content
            return b""
        try:
            data = await self._storage_get(hash_b, bytes.fromhex(info["context"]))
        except FileNotFoundError:
            if on_disk:
                with open(abspath, "rb") as f:
                    return f.read()[start:end]
            if self.offline:
                raise FileNotFoundError(
                    f"{path!r}: content fragment is not resident locally and "
                    f"offline=True; run fs.fetch(...) or construct the filesystem "
                    f"with offline=False to allow lazy fetching"
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

    def ukey(self, path):
        """Stable cache key = the Lore content address (mirrors GitFileSystem)."""
        return self.info(path)["hash"]

    def _open(
        self,
        path,
        mode="rb",
        block_size=None,
        autocommit=True,
        cache_options=None,
        ref=None,
        **kwargs,
    ):
        if mode != "rb":
            raise NotImplementedError(
                "writes go through fs.transaction(...) + pipe_file, not open(mode='wb')"
            )
        data = self.cat_file(path, ref=ref)
        return MemoryFile(self, path, data)

    async def open_async(self, path, mode="rb", ref=None, **kwargs):
        """Async streaming reads — the large-asset path for Lore.

        Returns an :class:`AbstractAsyncStreamedFile` whose ``_fetch_range`` maps
        to ``_cat_file`` byte ranges, so a consumer can ``seek``/``read`` chunks
        of a multi-GB asset without buffering the whole blob (as ``_open`` /
        ``MemoryFile`` does). Read-only, mirroring ``_open``.
        """
        if mode != "rb":
            raise NotImplementedError(
                "open_async supports read-only 'rb'; writes go through "
                "fs.transaction(...) + pipe_file"
            )
        info = await self._info(path, ref=ref)
        if info["type"] != "file":
            raise IsADirectoryError(path)
        return LoreAsyncStreamedFile(self, path, info["size"], ref)

    # ----------------------------------------------------------------- writes
    async def _pipe_file(self, path, value, **kwargs):
        """Author bytes into the working copy and stage them.

        Authoring is ordinary file I/O into the clone (Lore's ``file_write`` is
        for materializing store content, not authoring); we then ``file_stage``
        the absolute path. The commit happens on transaction exit.
        """
        inner = self._strip_protocol(path)
        abspath = self._abs(inner)
        parent = os.path.dirname(abspath)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(abspath, "wb") as f:
            f.write(value)
        await self._run(
            self._lore.file_stage, LoreFileStageArgs(paths=[abspath], scan=True)
        )
        if getattr(self, "_intrans", False) and self._transaction is not None:
            self._transaction._staged.append(abspath)
        return None

    def transaction(self, message: str | None = None, metadata: dict | None = None):
        """Enter a write transaction that commits one Lore revision on exit."""
        self._transaction = LoreTransaction(self, message=message, metadata=metadata)
        return self._transaction

    def _commit_revision(self, message, metadata=None):
        fsspec.asyn.sync(self.loop, self._commit_revision_async, message, metadata)

    async def _commit_revision_async(self, message, metadata):
        await self._run(
            self._lore.revision_commit, LoreRevisionCommitArgs(message=message or "")
        )
        if not self.offline:
            await self._run(self._lore.branch_push, LoreBranchPushArgs())

    def _reset_paths(self, paths):
        if paths:
            fsspec.asyn.sync(self.loop, self._unstage_async, paths)

    async def _unstage_async(self, paths):
        await self._run(self._lore.file_unstage, LoreFileUnstageArgs(paths=paths))

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

    # ------------------------------------------------------------- lifecycle
    def close(self) -> None:
        """Release the cached content-store handle, if one was opened."""
        if self._store_handle is not None:
            handle, self._store_handle = self._store_handle, None
            fsspec.asyn.sync(self.loop, self._close_async, handle)

    async def _close_async(self, handle: int) -> None:
        await self._run(self._lore.storage_close, LoreStorageCloseArgs(handle=handle))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # -------------------------------------------------------------- internals
    async def _run(self, command, cmd_args, *, entry_type=None):
        return await _lore.run(command, self._gargs(), cmd_args, entry_type=entry_type)


class LoreAsyncStreamedFile(AbstractAsyncStreamedFile):
    """Read-only async file streaming bytes from Lore's content store.

    ``size`` is passed in so the base class never does a sync ``info`` lookup on
    the loop thread; ``cache_type="none"`` keeps it a true stream (each ``read``
    pulls exactly its range via ``_fetch_range`` → ``_cat_file``).
    """

    def __init__(self, fs, path, size, ref):
        super().__init__(fs, path, mode="rb", size=size, cache_type="none")
        self._ref = ref

    async def _fetch_range(self, start, end):
        return await self.fs._cat_file(self.path, start=start, end=end, ref=self._ref)


def _is_lore_clone(path: str) -> bool:
    return os.path.isdir(os.path.join(path, ".lore"))


def _join(base: str, leaf: str) -> str:
    """Join an fsspec (POSIX) repo-relative dir and a leaf name."""
    return f"{base}/{leaf}" if base else leaf
