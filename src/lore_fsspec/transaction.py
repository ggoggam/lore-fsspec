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

    def start(self):
        self.fs._intrans = True
        self._staged: list[str] = []

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
