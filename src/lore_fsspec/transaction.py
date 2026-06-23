"""Write transaction: stage-on-write, commit one Lore revision on exit.

Lore's native write model is *write content -> stage -> commit a revision*, which
is a transaction. We surface it as fsspec's :class:`~fsspec.transaction.Transaction`
so writes read like any other fsspec write but finalize **atomically as one Lore
revision** (one ``revision_commit`` regardless of file count), pushed to the
server unless the filesystem is ``offline``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fsspec.transaction import Transaction

if TYPE_CHECKING:
    from lore_fsspec.core import LoreFileSystem


class LoreTransaction(Transaction):
    """Batch staged writes into a single ``revision_commit`` (+ optional push)."""

    def __init__(
        self,
        fs: LoreFileSystem,
        message: str | None = None,
        metadata: dict | None = None,
        **kwargs: object,
    ) -> None:
        """Initialize the transaction with optional commit message and metadata."""
        super().__init__(fs, **kwargs)
        self.message = message
        self.metadata = metadata or {}
        self.branch: str | None = None
        self.create = False
        self._staged: list[str] = []
        self._prev_ref: str | None = None

    def __call__(
        self,
        message: str | None = None,
        metadata: dict | None = None,
        branch: str | None = None,
        *,
        create: bool = False,
    ) -> LoreTransaction:
        """Configure the commit, then return self.

        fsspec exposes ``fs.transaction`` as a *property* that lazily builds this
        object (so internals like ``open(..., 'wb')`` can reach ``.files``). To
        also support the documented ``with fs.transaction(message=...):`` form we
        make the transaction callable: it records the settings and hands back
        ``self`` as the context manager.

        ``branch`` checks out that branch for the duration of the block, so the
        revision lands there instead of the current branch (and the original
        branch is restored on exit) — the isolation slice of a feature-branch
        workflow. With ``create=True`` the branch is created first (off the
        current tip). Merging back is left to :meth:`LoreFileSystem.merge`, since
        a merge can conflict and must not be performed implicitly.
        """
        if message is not None:
            self.message = message
        if metadata is not None:
            self.metadata = metadata
        if branch is not None:
            self.branch = branch
            self.create = create
        return self

    def record_staged(self, path: str) -> None:
        """Record an absolute path as touched during the current transaction."""
        self._staged.append(path)

    def start(self) -> None:
        """Begin the transaction: reset staged state and switch branch if needed."""
        super().start()  # resets self.files (deque); arms fs._intrans = True
        self._staged = []
        self._prev_ref = None
        if self.branch is not None and self.branch != self.fs.ref:
            # The branch switch can fail (e.g. create=True on an existing branch).
            # We're inside __enter__ -> start(); if start() raises, __exit__ never
            # runs, so the _intrans flag super().start() just armed would stay set
            # forever, silently letting later writes escape the transaction. Undo
            # it on failure. Record _prev_ref only after a successful switch so a
            # failed start doesn't later try to "restore" a branch we never left.
            prev = self.fs.ref
            try:
                if self.create:
                    self.fs.create_branch(self.branch, checkout=True)
                else:
                    self.fs.switch_branch(self.branch)
            except BaseException:
                self.fs._intrans = False
                self.fs._transaction = None
                raise
            self._prev_ref = prev

    def complete(self, commit: bool = True) -> None:  # noqa: FBT001, FBT002
        """Commit or roll back all staged writes as one atomic revision."""
        try:
            if commit:
                # Files written during the txn were already staged on write.
                self.fs._commit_revision(self.message, self.metadata)
            else:
                self.fs._reset_paths(self._staged)
        finally:
            self.fs._intrans = False
            self._staged = []
            # Restore the branch we were on before the block, even on failure.
            if self._prev_ref is not None:
                self.fs.switch_branch(self._prev_ref)
                self._prev_ref = None
            # One-shot settings: don't leak branch targeting into the next txn.
            self.branch = None
            self.create = False
