"""Write transaction: stage-on-write, commit one Lore revision on exit.

Lore's native write model is *write content -> stage -> commit a revision*, which
is a transaction. We surface it as fsspec's :class:`~fsspec.transaction.Transaction`
so writes read like any other fsspec write but finalize **atomically as one Lore
revision** (one ``revision_commit`` regardless of file count), pushed to the
server unless the filesystem is ``offline``.
"""

from __future__ import annotations

from fsspec.transaction import Transaction


class LoreTransaction(Transaction):
    """Batch staged writes into a single ``revision_commit`` (+ optional push)."""

    def __init__(
        self, fs, message: str | None = None, metadata: dict | None = None, **kwargs
    ):
        super().__init__(fs, **kwargs)
        self.message = message
        self.metadata = metadata or {}
        self._staged: list[str] = []

    def __call__(
        self, message: str | None = None, metadata: dict | None = None
    ) -> LoreTransaction:
        """Configure the commit message/metadata, then return self.

        fsspec exposes ``fs.transaction`` as a *property* that lazily builds this
        object (so internals like ``open(..., 'wb')`` can reach ``.files``). To
        also support the documented ``with fs.transaction(message=...):`` form we
        make the transaction callable: it records the message/metadata and hands
        back ``self`` as the context manager.
        """
        if message is not None:
            self.message = message
        if metadata is not None:
            self.metadata = metadata
        return self

    def start(self):
        super().start()  # resets self.files (deque) for a fresh transaction
        self.fs._intrans = True
        self._staged = []

    def complete(self, commit: bool = True):
        try:
            if commit:
                # Files written during the txn were already staged on write.
                self.fs._commit_revision(self.message, self.metadata)
            else:
                self.fs._reset_paths(self._staged)
        finally:
            self.fs._intrans = False
            self._staged = []
