"""Integration tests against a live Lore server (gated by `fixture_repo`)."""

from __future__ import annotations

import os

import fsspec
import pytest

from lore_fsspec import LoreFileSystem

# Every test in this module needs a live Lore server (via `fixture_repo`). The
# marker lets `pytest -m 'not integration'` (mise run test:unit) skip them
# deterministically, independent of whether a server happens to be reachable.
pytestmark = pytest.mark.integration


@pytest.fixture
def fs(fixture_repo):
    # cachable=True would memoize instances across tests; force a fresh one.
    # writable=True so the write/transaction tests can mutate; reads are
    # unaffected. The read-only guardrail is covered separately below.
    return LoreFileSystem(
        path=fixture_repo["root"], writable=True, skip_instance_cache=True
    )


def _revision_count(fs) -> int:
    """Number of committed revisions (the user-visible commit guarantee)."""
    import fsspec.asyn
    from lore.types.args import LoreRevisionHistoryArgs
    from lore.types.events import LoreRevisionHistoryEntryEventData

    evs = fsspec.asyn.sync(
        fs.loop,
        fs._run,
        fs._lore.revision_history,
        LoreRevisionHistoryArgs(),
        entry_type=LoreRevisionHistoryEntryEventData,
    )
    return len(evs)


def test_default_ref_is_current_branch(fs):
    assert fs.ref == "main"


def test_ls_root(fs):
    assert set(fs.ls("", detail=False)) >= {"hello.txt", "sub", "Content"}


def test_ls_detail_shape(fs):
    entries = {e["name"]: e for e in fs.ls("", detail=True)}
    assert entries["hello.txt"]["type"] == "file"
    assert entries["hello.txt"]["size"] == 51
    assert entries["sub"]["type"] == "directory"


def test_ls_nested_returns_full_repo_relative_names(fs):
    assert fs.ls("Content/Config", detail=False) == ["Content/Config/Game.ini"]
    assert fs.ls("Content", detail=False) == ["Content/Config"]


def test_find_recurses(fs):
    found = set(fs.find(""))
    assert {"hello.txt", "sub/data.bin", "Content/Config/Game.ini"} <= found


def test_info_file(fs):
    info = fs.info("hello.txt")
    assert info["type"] == "file"
    assert info["size"] == 51
    assert len(info["hash"]) == 64  # 32-byte content hash, hex


def test_info_missing_raises(fs):
    with pytest.raises(FileNotFoundError):
        fs.info("does/not/exist.txt")


def test_cat_file_full(fs, fixture_repo):
    assert fs.cat("hello.txt") == fixture_repo["files"]["hello.txt"]


def test_cat_file_range(fs):
    assert fs.cat_file("hello.txt", start=6, end=11) == b"lore "


def test_open_read(fs, fixture_repo):
    with fs.open("hello.txt") as f:
        assert f.read() == fixture_repo["files"]["hello.txt"]


def test_cat_file_from_store_when_not_on_disk(fs, fixture_repo):
    """In-store read path: bytes come from `storage_get`, not the working copy."""
    os.remove(os.path.join(fixture_repo["root"], "hello.txt"))
    # file_info still resolves the committed content; local_size drops to 0.
    assert fs.info("hello.txt")["local_size"] == 0
    assert fs.cat("hello.txt") == fixture_repo["files"]["hello.txt"]


def test_cat_file_range_from_store(fs, fixture_repo):
    os.remove(os.path.join(fixture_repo["root"], "hello.txt"))
    assert fs.cat_file("hello.txt", start=6, end=11) == b"lore "


def test_close_releases_store_handle(fs, fixture_repo):
    os.remove(os.path.join(fixture_repo["root"], "hello.txt"))
    fs.cat("hello.txt")  # opens the store handle
    assert fs._store_handle is not None
    fs.close()
    assert fs._store_handle is None


def test_context_manager_closes(fixture_repo):
    with LoreFileSystem(path=fixture_repo["root"], skip_instance_cache=True) as fs:
        os.remove(os.path.join(fixture_repo["root"], "hello.txt"))
        assert fs.cat("hello.txt") == fixture_repo["files"]["hello.txt"]
        assert fs._store_handle is not None
    assert fs._store_handle is None


def _revision_hexes(fs) -> list[str]:
    """Committed revisions newest-first, as hex ids usable in a ``ref=`` arg."""
    import fsspec.asyn
    from lore.types.args import LoreRevisionHistoryArgs
    from lore.types.events import LoreRevisionHistoryEntryEventData

    evs = fsspec.asyn.sync(
        fs.loop,
        fs._run,
        fs._lore.revision_history,
        LoreRevisionHistoryArgs(),
        entry_type=LoreRevisionHistoryEntryEventData,
    )
    return [e.revision.hex() for e in evs]


def test_resolve_rev_defaults_and_passthrough(fs):
    import fsspec.asyn

    # empty / current branch -> "" (working copy, no resolution roundtrip)
    assert fsspec.asyn.sync(fs.loop, fs._resolve_rev, None) == ""
    assert fsspec.asyn.sync(fs.loop, fs._resolve_rev, "main") == ""
    # an unknown ref (not a branch) passes through as a revision id
    assert fsspec.asyn.sync(fs.loop, fs._resolve_rev, "abc123") == "abc123"


def test_resolve_branch_name_to_tip(fs):
    import fsspec.asyn
    from lore.types.args import LoreBranchCreateArgs

    fsspec.asyn.sync(
        fs.loop,
        fs._run,
        fs._lore.branch_create,
        LoreBranchCreateArgs(branch="feature"),
    )
    tip = fsspec.asyn.sync(fs.loop, fs._resolve_rev, "feature")
    # branch name resolves to a 32-byte revision id (hex), not passed through raw
    assert tip and tip != "feature" and len(tip) == 64


def test_cat_at_revision_id(fs, fixture_repo):
    """Reading an explicit (non-checked-out) revision goes through the store."""
    orig = fixture_repo["files"]["hello.txt"]
    with fs.transaction(message="rev2"):
        fs.pipe_file("hello.txt", b"second revision\n")
    assert fs.cat("hello.txt") == b"second revision\n"  # working copy is rev2
    rev1 = _revision_hexes(fs)[-1]  # oldest revision == the fixture commit
    assert fs.cat("hello.txt", ref=rev1) == orig


def test_open_async_full_read(fs, fixture_repo):
    import fsspec.asyn

    f = fsspec.asyn.sync(fs.loop, fs.open_async, "hello.txt")
    try:
        data = fsspec.asyn.sync(fs.loop, f.read)
    finally:
        fsspec.asyn.sync(fs.loop, f.close)
    assert data == fixture_repo["files"]["hello.txt"]


def test_open_async_seek_and_chunked_read(fs, fixture_repo):
    import fsspec.asyn

    full = fixture_repo["files"]["hello.txt"]
    f = fsspec.asyn.sync(fs.loop, fs.open_async, "hello.txt")
    try:
        first = fsspec.asyn.sync(fs.loop, f.read, 6)
        f.seek(6)
        mid = fsspec.asyn.sync(fs.loop, f.read, 5)
    finally:
        fsspec.asyn.sync(fs.loop, f.close)
    assert first == full[:6]
    assert mid == b"lore "


def test_fetch_syncs_current_ref(fs):
    targets = fs.fetch()
    assert targets and all(isinstance(t, int) for t in targets)
    # naming the default branch resolves to the same tip, no error
    assert fs.fetch("main")


def test_concurrent_cat_fanout_shares_one_handle(fs, fixture_repo):
    """`_cat` fan-out over the store path must not race-open multiple handles."""
    rels = ["hello.txt", "sub/data.bin", "Content/Config/Game.ini"]
    for rel in rels:
        os.remove(os.path.join(fixture_repo["root"], rel))
    out = fs.cat(rels)  # concurrent _cat_file gather, all via storage_get
    for rel in rels:
        assert out[rel] == fixture_repo["files"][rel]
    assert isinstance(fs._store_handle, int)


def test_cat_ranges_random_access_from_store(fs, fixture_repo):
    """Random-access ranged reads (the pyarrow/zarr pattern) over the store."""
    os.remove(os.path.join(fixture_repo["root"], "hello.txt"))
    full = fixture_repo["files"]["hello.txt"]
    assert fs.cat_file("hello.txt", start=0, end=5) == full[0:5]
    assert fs.cat_file("hello.txt", start=10, end=20) == full[10:20]
    assert fs.cat_file("hello.txt", start=40) == full[40:]
    res = fs.cat_ranges(["hello.txt", "hello.txt"], [0, 17], [5, 34])
    assert res == [full[0:5], full[17:34]]


def test_ukey_is_content_hash(fs):
    assert fs.ukey("hello.txt") == fs.info("hello.txt")["hash"]


def test_url_roundtrip(fixture_repo):
    url = f"lore://{fixture_repo['root']}:main@hello.txt"
    with fsspec.open(url) as f:
        assert f.read() == fixture_repo["files"]["hello.txt"]


def test_writes_require_writable_flag(fixture_repo):
    """A default (read-only) filesystem rejects writes with a clear error."""
    ro = LoreFileSystem(path=fixture_repo["root"], skip_instance_cache=True)
    with pytest.raises(PermissionError):
        with ro.transaction(message="nope"):
            ro.pipe_file("blocked.txt", b"x")


def test_writes_require_open_transaction(fs):
    """Even a writable filesystem rejects a write outside a transaction."""
    with pytest.raises(ValueError):
        fs.pipe_file("loose.txt", b"x")


def test_open_wb_writes_in_transaction(fs):
    before = _revision_count(fs)
    with fs.transaction(message="open wb"):
        with fs.open("written.txt", "wb") as f:
            f.write(b"hello via open\n")
    assert fs.cat("written.txt") == b"hello via open\n"
    assert _revision_count(fs) == before + 1


def test_put_file_stages_local_file(fs, tmp_path):
    src = tmp_path / "local.bin"
    src.write_bytes(b"payload-from-disk")
    with fs.transaction(message="put"):
        fs.put_file(str(src), "uploaded.bin")
    assert fs.cat("uploaded.bin") == b"payload-from-disk"


def test_rm_removes_from_tree(fs):
    before = _revision_count(fs)
    assert fs.exists("hello.txt")
    with fs.transaction(message="rm hello"):
        fs.rm("hello.txt")
    assert not fs.exists("hello.txt")
    assert _revision_count(fs) == before + 1


def test_mv_renames_in_tree(fs, fixture_repo):
    orig = fixture_repo["files"]["hello.txt"]
    with fs.transaction(message="mv hello"):
        fs.mv("hello.txt", "renamed.txt")
    assert not fs.exists("hello.txt")
    assert fs.cat("renamed.txt") == orig


def test_rollback_restores_tracked_and_purges_new(fs, fixture_repo):
    """Aborting a transaction restores edited tracked files and drops new ones."""
    orig = fixture_repo["files"]["hello.txt"]
    before = _revision_count(fs)
    with pytest.raises(RuntimeError):
        with fs.transaction(message="should roll back"):
            fs.pipe_file("hello.txt", b"clobbered\n")  # edit a tracked file
            fs.pipe_file("brand_new.txt", b"new\n")  # add a new file
            raise RuntimeError("boom")
    # tracked file restored to its committed content; new file purged.
    assert fs.cat("hello.txt") == orig
    assert not fs.exists("brand_new.txt")
    assert _revision_count(fs) == before


def test_transaction_commit_roundtrip(fs):
    before = _revision_count(fs)
    with fs.transaction(message="add via fsspec"):
        fs.pipe_file("new/dir/a.txt", b"alpha")
        fs.pipe_file("b.txt", b"beta")
    assert fs.cat("new/dir/a.txt") == b"alpha"
    assert fs.cat("b.txt") == b"beta"
    # One revision for the whole transaction, regardless of file count.
    assert _revision_count(fs) == before + 1


def test_transaction_rollback_does_not_commit(fs):
    before = _revision_count(fs)
    with pytest.raises(RuntimeError):
        with fs.transaction(message="should not land"):
            fs.pipe_file("ghost.txt", b"boo")
            raise RuntimeError("boom")
    # Rollback unstages the write; no new revision is committed.
    assert _revision_count(fs) == before


# ----------------------------------------------------------------- branches
def test_branches_lists_and_creates(fs):
    assert fs.branches() == ["main"]
    fs.create_branch("feature")
    assert set(fs.branches()) == {"main", "feature"}
    # Creating a branch alone doesn't move us off the current branch.
    assert fs.ref == "main"


def test_create_branch_checkout_switches_ref(fs):
    fs.create_branch("work", checkout=True)
    assert fs.ref == "work"
    fs.switch_branch("main")
    assert fs.ref == "main"


def test_branch_ops_require_writable(fixture_repo):
    ro = LoreFileSystem(path=fixture_repo["root"], skip_instance_cache=True)
    with pytest.raises(PermissionError):
        ro.create_branch("nope")
    with pytest.raises(PermissionError):
        ro.merge("main")


def test_transaction_branch_commits_on_target_and_restores(fs):
    """Writes in a `branch=`/`create=` block land on that branch, not main."""
    main_before = _revision_count(fs)
    with fs.transaction(message="on feature", branch="feature", create=True):
        fs.pipe_file("feature_only.txt", b"feat\n")
    # Back on main when the block exits, and main is untouched.
    assert fs.ref == "main"
    assert _revision_count(fs) == main_before
    assert not fs.exists("feature_only.txt")
    # The file is present on the feature branch we committed to.
    assert fs.cat("feature_only.txt", ref="feature") == b"feat\n"


def test_clean_merge_brings_in_branch_changes(fs):
    fs.create_branch("feature", checkout=True)
    with fs.transaction(message="add on feature"):
        fs.pipe_file("from_feature.txt", b"hi\n")
    fs.switch_branch("main")
    assert not fs.exists("from_feature.txt")
    fs.merge("feature")
    # Merge is a no-op-free fast path: the file now exists on main.
    assert fs.cat("from_feature.txt") == b"hi\n"


def test_conflicting_merge_aborts_and_raises(fs):
    from lore_fsspec.errors import LoreError

    # Divergent edits to the same path on main and feature.
    fs.create_branch("feature", checkout=True)
    with fs.transaction(message="feature edit"):
        fs.pipe_file("hello.txt", b"feature side\n")
    fs.switch_branch("main")
    with fs.transaction(message="main edit"):
        fs.pipe_file("hello.txt", b"main side\n")

    main_before = _revision_count(fs)
    with pytest.raises(LoreError, match="conflict"):
        fs.merge("feature")
    # Aborted: main's revision count is unchanged and its content is intact.
    assert _revision_count(fs) == main_before
    assert fs.cat("hello.txt") == b"main side\n"
