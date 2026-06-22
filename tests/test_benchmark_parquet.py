"""End-to-end benchmark: upload then query a multi-file parquet dataset.

A realistic data-engineering usecase, not a micro-test: generate a moderate
(~50-200 MB) columnar dataset spread across several parquet files, **upload it as
one atomic Lore revision** through ``LoreFileSystem``, then **query it back over
``lore://``** with two real engines — DuckDB (SQL, via a registered fsspec
filesystem) and Polars (DataFrame, via ``fs.open`` file objects).

This closes the gap recorded in ``docs/03-roadmap.md`` (Phase 2): the ranged /
columnar read path was previously verified only *synthetically* because pyarrow
wasn't installed. Here it's a genuine parquet round-trip.

A second end-to-end scenario (``test_write_audit_publish``) exercises the
branch/merge API as a data team would: ingest a new partition on an isolation
branch, audit it without exposing it on ``main``, then publish it atomically by
merging — the "write-audit-publish" pattern.

Sizing is env-tunable so the same module serves CI (moderate default) and ad-hoc
perf runs::

    LORE_BENCH_FILES=8 LORE_BENCH_ROWS=1000000 uv run --group bench \\
        pytest tests/test_benchmark_parquet.py -m integration -s

``-s`` surfaces the printed timing / throughput table.

Requires a live Lore server (the ``integration`` marker + ``lore_server``
fixture, self-contained via testcontainers) and the ``bench`` dependency group
(``duckdb``, ``polars``, ``numpy``); the engine imports ``importorskip`` so the
module skips cleanly when that group isn't installed.
"""

from __future__ import annotations

import os
import time

import pytest

# Engines + data-gen. Skip the whole module (not error) when the bench group is
# absent, so a plain `uv run pytest` without `--group bench` stays green.
np = pytest.importorskip("numpy")
pl = pytest.importorskip("polars")
duckdb = pytest.importorskip("duckdb")

from lore_fsspec import LoreFileSystem  # noqa: E402

# Needs a live server, same as the other integration tests.
pytestmark = pytest.mark.integration

# Moderate default: 8 files x 1M rows. Each row is ~40 B across 5 columns, so the
# raw dataset is ~320 MB and the parquet-on-disk footprint lands in the target
# ~50-200 MB band (random floats limit compression). Override via env for perf
# runs. ``ROW_GROUP`` keeps several row groups per file so engines can prune.
N_FILES = int(os.environ.get("LORE_BENCH_FILES", "8"))
ROWS_PER_FILE = int(os.environ.get("LORE_BENCH_ROWS", "1000000"))
ROW_GROUP = int(os.environ.get("LORE_BENCH_ROW_GROUP", "200000"))

REGIONS = ["us-east", "us-west", "eu-central", "ap-south", "sa-east"]
DATA_DIR = "warehouse/events"  # in-repo (repo-relative) destination


class _Timer:
    """Records named phase durations and prints one timing table at teardown."""

    def __init__(self, title: str, subtitle: str = "") -> None:
        self.title = title
        self.subtitle = subtitle
        self.rows: list[tuple[str, float, float | None]] = []

    def record(self, label: str, seconds: float, mb: float | None = None) -> None:
        self.rows.append((label, seconds, mb))

    def report(self) -> None:
        print("\n" + "=" * 64)
        print(self.title)
        if self.subtitle:
            print(f"  {self.subtitle}")
        print("-" * 64)
        print(f"  {'phase':<34}{'time(s)':>10}{'MB/s':>12}")
        for label, secs, mb in self.rows:
            tput = f"{mb / secs:>12.1f}" if mb and secs > 0 else " " * 12
            print(f"  {label:<34}{secs:>10.3f}{tput}")
        print("=" * 64)


def _make_parquet(path: str, *, seed: int, rows: int = ROWS_PER_FILE) -> int:
    """Write one parquet shard of ``rows`` rows; return its byte size.

    A small star-schema-ish fact table (timestamp, region, user, amount,
    quantity) so the read-back queries are meaningful (group-by region, filter by
    amount) rather than a single trivial column.
    """
    rng = np.random.default_rng(seed)
    n = rows
    base = np.datetime64("2026-01-01T00:00:00.000000")
    regions = np.array(REGIONS)
    df = pl.DataFrame(
        {
            # one event per minute, monotonically increasing within a shard
            "ts": base + np.arange(n, dtype="timedelta64[us]") * 60_000_000,
            "region": regions[rng.integers(0, len(REGIONS), n)],
            "user_id": rng.integers(0, 100_000, n).astype(np.int32),
            "amount": rng.random(n) * 100.0,
            "quantity": rng.integers(1, 11, n).astype(np.int16),
        }
    )
    df.write_parquet(path, row_group_size=ROW_GROUP, compression="snappy")
    return os.path.getsize(path)


@pytest.fixture(scope="module")
def uploaded_dataset(lore_server, tmp_path_factory):
    """Generate the dataset locally, upload it in one Lore revision, yield context.

    Module-scoped so the generate+upload cost is paid once and shared by every
    query test below (this is a benchmark — re-uploading per test would dominate).
    Builds its own server repo (rather than the function-scoped ``fixture_repo``)
    so the scope lines up.
    """
    import lore
    from lore.types import args as A

    timer = _Timer(
        "lore-fsspec parquet benchmark",
        f"files={N_FILES} rows/file={ROWS_PER_FILE:,} row_group={ROW_GROUP:,}",
    )
    tmp = tmp_path_factory.mktemp("bench")
    clone_root = str(tmp / "clone")
    os.makedirs(clone_root, exist_ok=True)
    local_dir = tmp / "local_parquet"
    local_dir.mkdir()

    # --- create an empty server repo + local clone (mirrors conftest.fixture_repo)
    L = lore.Lore()
    url = f"{lore_server}/bench-{int(time.time() * 1000)}"
    g = A.LoreGlobalArgs(repository_path=clone_root)
    create = L.repository_create(g, A.LoreRepositoryCreateArgs(repository_url=url))
    for ev in create.collect():
        if type(ev).__name__ == "LoreErrorEventData":
            pytest.fail(f"repo create failed: {ev.error_inner}")

    # --- generate parquet shards on local disk
    t0 = time.perf_counter()
    local_paths, total_bytes = [], 0
    for i in range(N_FILES):
        p = local_dir / f"part-{i:03d}.parquet"
        total_bytes += _make_parquet(str(p), seed=i)
        local_paths.append(p)
    total_mb = total_bytes / 1e6
    timer.record("generate parquet (local)", time.perf_counter() - t0, total_mb)

    # --- upload all shards as ONE atomic revision through the transaction
    fs = LoreFileSystem(path=clone_root, writable=True, skip_instance_cache=True)
    t0 = time.perf_counter()
    with fs.transaction(message=f"import {N_FILES} parquet shards"):
        for p in local_paths:
            fs.put_file(str(p), f"{DATA_DIR}/{p.name}")
    timer.record("upload -> one revision", time.perf_counter() - t0, total_mb)

    # known-good aggregates (computed locally) the query engines must reproduce
    full = pl.concat([pl.read_parquet(str(p)) for p in local_paths])
    expected = {
        "total_rows": full.height,
        "total_amount": full["amount"].sum(),
        "by_region": dict(
            full.group_by("region").agg(pl.len().alias("c")).sort("region").iter_rows()
        ),
    }

    try:
        yield {
            "fs": fs,
            "glob": f"{DATA_DIR}/*.parquet",
            "total_mb": total_mb,
            "expected": expected,
            "timer": timer,
        }
    finally:
        timer.report()
        fs.close()


def test_upload_landed_as_single_revision(uploaded_dataset):
    """The whole dataset import is one Lore revision, and every shard is listed."""
    import fsspec.asyn
    from lore.types.args import LoreRevisionHistoryArgs
    from lore.types.events import LoreRevisionHistoryEntryEventData

    fs = uploaded_dataset["fs"]
    listed = fs.ls(DATA_DIR, detail=False)
    assert len(listed) == N_FILES
    assert all(name.endswith(".parquet") for name in listed)

    revs = fsspec.asyn.sync(
        fs.loop,
        fs._run,
        fs._lore.revision_history,
        LoreRevisionHistoryArgs(),
        entry_type=LoreRevisionHistoryEntryEventData,
    )
    # exactly one user commit (the import); the empty repo started with none.
    assert len(revs) == 1


def test_duckdb_sql_over_lore(uploaded_dataset):
    """DuckDB SQL aggregation over a lore:// glob, via a registered fsspec fs."""
    fs = uploaded_dataset["fs"]
    exp = uploaded_dataset["expected"]
    timer = uploaded_dataset["timer"]

    con = duckdb.connect()
    con.register_filesystem(fs)  # route the 'lore' protocol to our instance
    glob = f"lore://{uploaded_dataset['glob']}"

    t0 = time.perf_counter()
    n_rows, total_amount = con.sql(
        f"SELECT count(*), sum(amount) FROM read_parquet('{glob}')"
    ).fetchone()
    timer.record(
        "duckdb scan+aggregate", time.perf_counter() - t0, uploaded_dataset["total_mb"]
    )

    assert n_rows == exp["total_rows"]
    assert total_amount == pytest.approx(exp["total_amount"], rel=1e-6)

    # group-by + projection (reads a subset of columns) reproduces local counts
    rows = con.sql(
        f"SELECT region, count(*) c FROM read_parquet('{glob}') "
        "GROUP BY region ORDER BY region"
    ).fetchall()
    assert dict(rows) == exp["by_region"]


def test_polars_dataframe_over_lore(uploaded_dataset):
    """Polars read-back over lore:// via fs.open file objects + a filtered query."""
    fs = uploaded_dataset["fs"]
    exp = uploaded_dataset["expected"]
    timer = uploaded_dataset["timer"]

    files = sorted(fs.glob(uploaded_dataset["glob"]))
    assert len(files) == N_FILES

    t0 = time.perf_counter()
    df = pl.concat([pl.read_parquet(fs.open(p)) for p in files])
    timer.record(
        "polars read (full)", time.perf_counter() - t0, uploaded_dataset["total_mb"]
    )

    assert df.height == exp["total_rows"]
    assert df["amount"].sum() == pytest.approx(exp["total_amount"], rel=1e-6)

    by_region = dict(
        df.group_by("region").agg(pl.len().alias("c")).sort("region").iter_rows()
    )
    assert by_region == exp["by_region"]


def test_polars_column_projection_reads_subset(uploaded_dataset):
    """Column projection: reading two columns must match a full read's aggregate.

    Exercises the columnar read shape (parquet only needs the projected column
    chunks). Note: ``_open`` currently materializes the whole blob into a
    ``MemoryFile``, so projection prunes work *after* the fetch, not at the byte
    level — a known optimization opportunity (true ranged fetch lives in
    ``open_async``).
    """
    fs = uploaded_dataset["fs"]
    exp = uploaded_dataset["expected"]

    files = sorted(fs.glob(uploaded_dataset["glob"]))
    projected = pl.concat(
        [pl.read_parquet(fs.open(p), columns=["region", "amount"]) for p in files]
    )
    assert projected.columns == ["region", "amount"]
    assert projected.height == exp["total_rows"]
    assert projected["amount"].sum() == pytest.approx(exp["total_amount"], rel=1e-6)


# --------------------------------------------------------------------------
# End-to-end: the write-audit-publish (WAP) pattern with branch + merge.
#
# The canonical reason a data team wants branches: ingest a new partition on an
# isolation branch, validate it *without* exposing it on main, then publish it
# atomically by merging. This exercises `create_branch` / `transaction(branch=,
# create=True)` / read-at-ref / `merge` together against real parquet + DuckDB.
# It uses its own repo so it can't perturb the shared `uploaded_dataset` above.
# --------------------------------------------------------------------------

# Smaller than the throughput benchmark: this scenario measures the *workflow*,
# not scan speed, so a few light shards keep it quick.
WAP_BASE_FILES = 2
WAP_ROWS = int(os.environ.get("LORE_BENCH_WAP_ROWS", "200000"))
INGEST_BRANCH = "ingest-2026-02"


@pytest.fixture(scope="module")
def warehouse(lore_server, tmp_path_factory):
    """An empty repo whose ``main`` already holds a few committed partitions."""
    import lore
    from lore.types import args as A

    tmp = tmp_path_factory.mktemp("wap")
    clone_root = str(tmp / "clone")
    os.makedirs(clone_root, exist_ok=True)
    local_dir = tmp / "local"
    local_dir.mkdir()

    L = lore.Lore()
    url = f"{lore_server}/wap-{int(time.time() * 1000)}"
    g = A.LoreGlobalArgs(repository_path=clone_root)
    for ev in L.repository_create(
        g, A.LoreRepositoryCreateArgs(repository_url=url)
    ).collect():
        if type(ev).__name__ == "LoreErrorEventData":
            pytest.fail(f"repo create failed: {ev.error_inner}")

    # Seed main with the existing warehouse partitions, as one revision.
    base_locals = []
    for i in range(WAP_BASE_FILES):
        p = local_dir / f"part-{i:03d}.parquet"
        _make_parquet(str(p), seed=i, rows=WAP_ROWS)
        base_locals.append(p)

    fs = LoreFileSystem(path=clone_root, writable=True, skip_instance_cache=True)
    with fs.transaction(message="seed warehouse"):
        for p in base_locals:
            fs.put_file(str(p), f"{DATA_DIR}/{p.name}")

    base_df = pl.concat([pl.read_parquet(str(p)) for p in base_locals])
    try:
        yield {
            "fs": fs,
            "local_dir": local_dir,
            "base_df": base_df,
            "timer": _Timer(
                "lore-fsspec write-audit-publish (branch + merge)",
                f"base_files={WAP_BASE_FILES} rows/file={WAP_ROWS:,}",
            ),
        }
    finally:
        fs.close()


def test_write_audit_publish(warehouse):
    """Ingest a partition on a branch, audit in isolation, then publish by merge."""
    fs = warehouse["fs"]
    timer = warehouse["timer"]
    new_local = warehouse["local_dir"] / "part-new.parquet"
    _make_parquet(str(new_local), seed=999, rows=WAP_ROWS)
    new_repo_path = f"{DATA_DIR}/{new_local.name}"
    new_df = pl.read_parquet(str(new_local))

    # --- WRITE: commit the new partition onto an isolation branch, not main.
    t0 = time.perf_counter()
    with fs.transaction(
        message="ingest Feb partition", branch=INGEST_BRANCH, create=True
    ):
        fs.put_file(str(new_local), new_repo_path)
    timer.record("write (isolated branch commit)", time.perf_counter() - t0)
    assert fs.ref == "main"  # the block restored us to the original branch

    # --- AUDIT: main is untouched; the branch has the new partition; and the
    # new data reads back correctly when we target the branch ref explicitly.
    assert sorted(fs.ls(DATA_DIR, detail=False)) == sorted(
        f"{DATA_DIR}/part-{i:03d}.parquet" for i in range(WAP_BASE_FILES)
    )
    on_branch = fs.ls(DATA_DIR, detail=False, ref=INGEST_BRANCH)
    assert len(on_branch) == WAP_BASE_FILES + 1
    assert new_repo_path in on_branch

    audited = pl.read_parquet(fs.open(new_repo_path, ref=INGEST_BRANCH))
    assert audited.height == new_df.height
    assert audited["amount"].sum() == pytest.approx(new_df["amount"].sum(), rel=1e-6)

    # --- PUBLISH: merge the branch into main as one revision.
    t0 = time.perf_counter()
    fs.merge(INGEST_BRANCH)
    timer.record("publish (merge -> main)", time.perf_counter() - t0)

    # main now serves the full dataset; verify the published aggregate via DuckDB.
    published = fs.ls(DATA_DIR, detail=False)
    assert len(published) == WAP_BASE_FILES + 1

    expected = pl.concat([warehouse["base_df"], new_df])
    con = duckdb.connect()
    con.register_filesystem(fs)
    n_rows, total_amount = con.sql(
        f"SELECT count(*), sum(amount) FROM read_parquet('lore://{DATA_DIR}/*.parquet')"
    ).fetchone()
    assert n_rows == expected.height
    assert total_amount == pytest.approx(expected["amount"].sum(), rel=1e-6)

    timer.report()
