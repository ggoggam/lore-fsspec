"""fsspec filesystem for Lore (Epic Games' version control system).

Exposes :class:`LoreFileSystem`, the Lore analogue of fsspec's ``GitFileSystem``.
Registered under the ``lore`` protocol via the ``fsspec.specs`` entry point in
``pyproject.toml``, so ``fsspec.filesystem("lore")`` and ``lore://`` URLs work
after install. We also register on import as a convenience for editable/dev use.
"""

from __future__ import annotations

from fsspec import register_implementation

from .core import LoreFileSystem
from .errors import LoreError
from .transaction import LoreTransaction

register_implementation("lore", LoreFileSystem, clobber=True)

__all__ = ["LoreFileSystem", "LoreTransaction", "LoreError"]
