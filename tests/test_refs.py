"""Unit tests for URL/ref parsing (no server, no liblore).

The grammar mirrors ``GitFileSystem`` verbatim::

    lore://<local-clone-path>[:<ref>][@<inner-path>]

As with Git, parsing keys off the ``:`` (ref) and ``@`` (inner) delimiters, so
the canonical, fully-qualified form always carries both. Degenerate URLs that
omit them inherit Git's quirks (e.g. the clone path is only split out of the URL
when a ``:<ref>`` is present); those edge cases are noted below.
"""

from __future__ import annotations

import pytest

from lore_fsspec import _refs
from lore_fsspec.core import LoreFileSystem


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("lore:///abs/clone:main@Content/Game.ini", "Content/Game.ini"),
        ("lore://Content/Game.ini", "Content/Game.ini"),
        ("Content/Game.ini", "Content/Game.ini"),
        ("lore:///abs/clone:main@sub/data.bin", "sub/data.bin"),
    ],
)
def test_strip_protocol(url: str, expected: str) -> None:
    assert LoreFileSystem._strip_protocol(url) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "lore:///abs/clone:main@Content/Game.ini",
            {"path": "/abs/clone", "ref": "main"},
        ),
        ("lore://rel/clone:ref@a/b", {"path": "rel/clone", "ref": "ref"}),
        # Git-inherited quirk: a ref is only captured when followed by "@", so the
        # clone path is split out but ":dev" (no inner) drops the ref here.
        ("lore:///abs/clone:dev", {"path": "/abs/clone"}),
        # And without any ":" the clone path is not split out of the URL at all
        # (callers pass `path=` explicitly instead).
        ("lore:///abs/clone", {}),
    ],
)
def test_get_kwargs_from_urls(url: str, expected: dict) -> None:
    assert LoreFileSystem._get_kwargs_from_urls(url) == expected


def test_inner_path_strips_ref_and_clone() -> None:
    assert _refs.inner_path("/abs/clone:main@deep/inner.bin") == "deep/inner.bin"
    assert _refs.inner_path("/abs/clone:main@") == ""
