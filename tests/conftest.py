"""Test fixtures.

Unit tests need neither a server nor the ``liblore`` native lib. Integration
tests build a throwaway repo on a reachable Lore server; they are skipped unless
one is available.

By default the integration tests are self-contained: the ``lore_server`` fixture
builds and starts an ephemeral zero-config ``loreserver`` in Docker via
testcontainers (see ``tests/docker/Dockerfile``), bound to the conventional ports
41337 (gRPC/QUIC) and 41339 (HTTP). Set ``LORE_TEST_REPOSITORY_URL`` (and run your
own server) to bypass the container, or just have one already listening on the
default ports and it will be reused.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

try:
    import lore as _lore_module
    import lore.types.args as lore_args
except ImportError:
    _lore_module = None  # type: ignore[assignment]
    lore_args = None  # type: ignore[assignment]

try:
    from testcontainers.core.container import DockerContainer as _DockerContainer
except ImportError:
    _DockerContainer = None  # type: ignore[assignment,misc]

# Default points at a zero-config `loreserver` on the conventional ports.
SERVER_URL = os.environ.get("LORE_TEST_REPOSITORY_URL", "lore://127.0.0.1:41337")
HEALTH_URL = os.environ.get(
    "LORE_TEST_HEALTH_URL",
    "http://127.0.0.1:41339/health_check",
)

# Keep in lockstep with the lore-vcs (liblore) wheel pinned in pyproject.toml and
# the LORE_SERVER_VERSION in mise.toml; passed through as a Docker build arg.
LORE_SERVER_VERSION = os.environ.get("LORE_SERVER_VERSION", "0.8.3")
_DOCKERFILE_DIR = Path(__file__).parent / "docker"
_IMAGE_TAG = f"lore-fsspec/loreserver:{LORE_SERVER_VERSION}"
_GRPC_PORT = 41337  # gRPC (TCP) + QUIC (UDP) share this port
_HTTP_PORT = 41339  # HTTP health check
_HTTP_OK = 200


def _have_liblore() -> bool:
    return importlib.util.find_spec("lore") is not None


def _server_up() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=2) as resp:
            return resp.status == _HTTP_OK
    except (urllib.error.URLError, OSError):
        return False


def _wait_healthy(timeout: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _server_up():
            return True
        time.sleep(1)
    return False


def _build_image() -> None:
    """Build the loreserver image.

    linux/amd64; Docker layer cache makes reruns instant once the binary
    download layer is cached.
    """
    subprocess.run(
        [
            "docker",
            "build",
            "--platform",
            "linux/amd64",
            "--build-arg",
            f"LORE_SERVER_VERSION={LORE_SERVER_VERSION}",
            "-t",
            _IMAGE_TAG,
            str(_DOCKERFILE_DIR),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


requires_lore = pytest.mark.skipif(not _have_liblore(), reason="liblore not available")


@pytest.fixture(scope="session")
def lore_server() -> str:  # type: ignore[return]
    """Yield a Lore server URL, starting a container if needed."""
    if not _have_liblore():
        pytest.skip("liblore not available")

    # Reuse an already-reachable server: either an explicitly configured external
    # one (LORE_TEST_REPOSITORY_URL), a hand-started local server, or a container
    # spun up by another xdist worker. In these cases we don't manage a container.
    if os.environ.get("LORE_TEST_REPOSITORY_URL") or _server_up():
        if not _server_up():
            pytest.skip(f"no Lore server reachable at {HEALTH_URL}")
        yield SERVER_URL
        return

    # Otherwise start a self-contained loreserver via testcontainers/Docker.
    if shutil.which("docker") is None:
        pytest.skip("docker not available to start a loreserver container")
    if _DockerContainer is None:
        pytest.skip("testcontainers not installed")

    try:
        _build_image()
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"failed to build loreserver image:\n{exc.stderr or exc.stdout}")

    container = (
        _DockerContainer(_IMAGE_TAG)
        # Fixed (not random) host ports: liblore uses the single URL port for both
        # gRPC (TCP) and QUIC (UDP), so both must map 1:1 to the same host port.
        .with_bind_ports(f"{_GRPC_PORT}/tcp", _GRPC_PORT)
        .with_bind_ports(f"{_GRPC_PORT}/udp", _GRPC_PORT)
        .with_bind_ports(f"{_HTTP_PORT}/tcp", _HTTP_PORT)
        .with_kwargs(platform="linux/amd64")
    )

    try:
        container.start()
    except Exception:
        # Another xdist worker likely grabbed the fixed ports first; fall back to
        # whatever became healthy on them rather than failing the run.
        if _wait_healthy(timeout=120):
            yield SERVER_URL
            return
        raise

    try:
        if not _wait_healthy():
            logs = container.get_logs()[1].decode(errors="replace")
            pytest.fail(f"loreserver container did not become healthy:\n{logs}")
        yield SERVER_URL
    finally:
        container.stop()


@pytest.fixture
def fixture_repo(lore_server: str, tmp_path: Path) -> dict:
    """Create a scratch repo with a couple of committed files; yield its clone root.

    Layout::

        hello.txt
        sub/data.bin
        Content/Config/Game.ini
    """
    lore_instance = _lore_module.Lore()
    root = str(tmp_path / "clone")
    Path(root).mkdir(parents=True, exist_ok=True)
    url = f"{lore_server}/pytest-{int(time.time() * 1000)}"
    g = lore_args.LoreGlobalArgs(repository_path=root)

    def run(executor: object) -> list:
        events = executor.collect()
        for e in events:
            if type(e).__name__ == "LoreErrorEventData":
                msg = f"lore setup failed: {e.error_inner}"
                raise RuntimeError(msg)
        return events

    create_args = lore_args.LoreRepositoryCreateArgs(repository_url=url)
    run(lore_instance.repository_create(g, create_args))

    files = {
        "hello.txt": b"hello lore world\n" * 3,
        "sub/data.bin": bytes(range(10)),
        "Content/Config/Game.ini": b"[core]\nname=lore\n",
    }
    for rel, data in files.items():
        abspath = str(Path(root) / rel)
        Path(abspath).parent.mkdir(parents=True, exist_ok=True)
        Path(abspath).write_bytes(data)
        stage_args = lore_args.LoreFileStageArgs(paths=[abspath], scan=True)
        run(lore_instance.file_stage(g, stage_args))
    commit_args = lore_args.LoreRevisionCommitArgs(message="fixture")
    run(lore_instance.revision_commit(g, commit_args))

    return {"root": root, "url": url, "files": files}
