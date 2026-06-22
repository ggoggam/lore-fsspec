"""Ref/revision parsing helpers.

A Lore "ref" is either a branch name (``main``, ``feature/x``), a revision
identifier, or empty (the current branch tip / working copy). The ``lore`` args
accept a ``revision`` string for both branches and revisions; the binding's job
is to translate an empty/default ref into ``""`` (meaning "current working
copy") and pass anything else straight through.

URL grammar (mirrors ``GitFileSystem`` verbatim)::

    lore://<local-clone-path>[:<ref>][@<inner-path>]
"""

from __future__ import annotations

PROTOCOL = "lore"
PREFIX = f"{PROTOCOL}://"


def split_url(path: str) -> dict[str, str]:
    """Extract ``{path, ref}`` (clone dir + ref) from a ``lore://`` URL.

    Mirrors ``GitFileSystem._get_kwargs_from_urls``: the clone path is taken up
    to the first ``:``; the ref is between ``:`` and ``@``. The trailing
    ``@<inner>`` is the in-repo path and is dropped here (it is recovered by
    ``_strip_protocol``).
    """
    path = path.removeprefix(PREFIX)
    out: dict[str, str] = {}
    if ":" in path:
        out["path"], path = path.split(":", 1)
    if "@" in path:
        out["ref"], path = path.split("@", 1)
    return out


def inner_path(path: str) -> str:
    """Reduce a ``lore://`` URL (or bare path) to its in-repo inner path.

    Drops the clone-path and ref segments, leaving the repository-relative path
    used as the fsspec path. Mirrors ``GitFileSystem._strip_protocol`` once the
    protocol prefix and leading slashes have been removed by the caller.
    """
    path = path.lstrip("/")
    if ":" in path:
        path = path.split(":", 1)[1]
    if "@" in path:
        path = path.split("@", 1)[1]
    return path.lstrip("/")
