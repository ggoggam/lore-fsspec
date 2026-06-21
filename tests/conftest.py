"""Test fixtures.

Unit tests need neither a server nor the ``liblore`` native lib. Integration
tests build a throwaway repo on a reachable Lore server; they are skipped unless
one is configured. A local zero-config ``loreserver`` (ports 41337/41339) is the
expected default.
"""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.request

import pytest

# Default points at a local zero-config `loreserver`.
SERVER_URL = os.environ.get("LORE_TEST_REPOSITORY_URL", "lore://127.0.0.1:41337")
HEALTH_URL = os.environ.get(
    "LORE_TEST_HEALTH_URL", "http://127.0.0.1:41339/health_check"
)


def _have_liblore() -> bool:
    try:
        import lore  # noqa: F401
    except Exception:
        return False
    return True


def _server_up() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=2) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


requires_lore = pytest.mark.skipif(not _have_liblore(), reason="liblore not available")


@pytest.fixture(scope="session")
def lore_server():
    if not _have_liblore():
        pytest.skip("liblore not available")
    if not _server_up():
        pytest.skip(f"no Lore server reachable at {HEALTH_URL}")
    return SERVER_URL


@pytest.fixture
def fixture_repo(lore_server, tmp_path):
    """Create a scratch repo with a couple of committed files; yield its clone root.

    Layout::

        hello.txt
        sub/data.bin
        Content/Config/Game.ini
    """
    import lore
    from lore.types import args as A

    L = lore.Lore()
    root = str(tmp_path / "clone")
    os.makedirs(root, exist_ok=True)
    url = f"{lore_server}/pytest-{int(time.time() * 1000)}"
    g = A.LoreGlobalArgs(repository_path=root)

    def run(executor):
        events = executor.collect()
        for e in events:
            if type(e).__name__ == "LoreErrorEventData":
                raise RuntimeError(f"lore setup failed: {e.error_inner}")
        return events

    run(L.repository_create(g, A.LoreRepositoryCreateArgs(repository_url=url)))

    files = {
        "hello.txt": b"hello lore world\n" * 3,
        "sub/data.bin": bytes(range(10)),
        "Content/Config/Game.ini": b"[core]\nname=lore\n",
    }
    for rel, data in files.items():
        abspath = os.path.join(root, rel)
        os.makedirs(os.path.dirname(abspath), exist_ok=True)
        with open(abspath, "wb") as f:
            f.write(data)
        run(L.file_stage(g, A.LoreFileStageArgs(paths=[abspath], scan=True)))
    run(L.revision_commit(g, A.LoreRevisionCommitArgs(message="fixture")))

    return {"root": root, "url": url, "files": files}
