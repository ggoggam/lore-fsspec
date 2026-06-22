# lore-fsspec

An [fsspec](https://filesystem-spec.readthedocs.io/) filesystem for
[Lore](https://epicgames.github.io/lore), Epic Games' open-source version control
system — the Lore equivalent of fsspec's built-in `GitFileSystem`.

It lets any fsspec-aware tool (pandas, pyarrow, zarr, dask, `fsspec.open`, …) read
files out of a local Lore clone at an arbitrary branch or revision via `lore://`
URLs, backed by the official `lore` Python bindings (`liblore`).

## Planned usage

```python
import fsspec

fs = fsspec.filesystem("lore", path="/path/to/clone", ref="main")
fs.ls("Content/Maps")
with fs.open("Content/Config/Game.ini", "rt") as f:
    print(f.read())

# URL form: lore://<clone-path>:<ref>@<in-repo-path>
with fsspec.open("lore:///path/to/clone:main@Content/Config/Game.ini", "rt") as f:
    print(f.read())
```

`lore` exposes a real coroutine API, so `LoreFileSystem` is an fsspec
`AsyncFileSystem` — usable directly from `asyncio` for concurrent reads:

```python
fs = fsspec.filesystem("lore", path="/path/to/clone", ref="main", asynchronous=True)
data = await fs._cat_file("Content/Config/Game.ini")     # awaitable
many = await fs._cat(["Content/A.ini", "Content/B.ini"])  # runs concurrently
```

Writes go through a transaction that maps onto Lore's native stage→commit flow —
all files in the block land as a **single revision** (or none, on error):

```python
fs = fsspec.filesystem("lore", path="/path/to/clone", ref="main", writable=True)
with fs.transaction(message="Import baked lighting"):
    fs.pipe_file("Content/Lighting/Baked.bin", data)
    fs.pipe_file("Content/Config/Game.ini", ini_bytes)
```

Branch topology is exposed as explicit methods (not folded into the write
transaction), mirroring `fetch()`. A transaction can target a branch so the
revision lands there in isolation; merging back is a separate, explicit step
because it can conflict:

```python
fs.branches()                                  # ['main']
fs.create_branch("import-job", checkout=True)  # like `git switch -c`

# Commit onto a branch in isolation, then restore the original branch on exit.
with fs.transaction(message="Import", branch="import-job", create=True):
    fs.pipe_file("Content/Lighting/Baked.bin", data)

# Clean merges land as one revision; a conflicting merge is aborted and raises
# with the conflicting paths (auto-resolution is intentionally never performed).
fs.merge("import-job")
```

## Examples

End-to-end usecases, distilled from the integration benchmark
(`tests/test_benchmark_parquet.py`). They show the library doing real
data-engineering work, not just listing files.

### Import a dataset atomically, then query it over `lore://`

Upload a multi-file parquet dataset as **one Lore revision** (all files land
together, or none on error), then read it straight back with two engines —
DuckDB (SQL) and Polars (DataFrame) — through the `lore://` protocol:

```python
import duckdb
import polars as pl
from lore_fsspec import LoreFileSystem

fs = LoreFileSystem(path="/path/to/clone", writable=True)

# Atomic import: every shard becomes part of a single revision.
with fs.transaction(message="import events dataset"):
    for shard in ("part-000.parquet", "part-001.parquet", "part-002.parquet"):
        fs.put_file(f"/local/events/{shard}", f"warehouse/events/{shard}")

# Query with DuckDB by registering the filesystem under the 'lore' protocol.
con = duckdb.connect()
con.register_filesystem(fs)
rows, total = con.sql(
    "SELECT count(*), sum(amount) "
    "FROM read_parquet('lore://warehouse/events/*.parquet')",
).fetchone()

# Or read DataFrames directly via fs.open file objects (column projection works).
df = pl.concat(
    pl.read_parquet(fs.open(p), columns=["region", "amount"])
    for p in sorted(fs.glob("warehouse/events/*.parquet"))
)
by_region = df.group_by("region").agg(pl.len()).sort("region")
```

### Write-audit-publish with a branch

Ingest a new partition on an isolation branch, audit it **without exposing it on
`main`**, then publish it atomically by merging — the classic
"write-audit-publish" pattern:

```python
fs = LoreFileSystem(path="/path/to/clone", writable=True)  # starts on 'main'

# WRITE: commit the new partition onto a fresh branch, not main.
with fs.transaction(message="ingest Feb partition", branch="ingest-2026-02", create=True):
    fs.put_file("/local/part-new.parquet", "warehouse/events/part-new.parquet")
assert fs.ref == "main"  # the transaction restored the original branch on exit

# AUDIT: main is untouched; read the new data back by targeting the branch ref.
fs.ls("warehouse/events")                          # main: existing partitions only
fs.ls("warehouse/events", ref="ingest-2026-02")    # branch: includes part-new
audited = pl.read_parquet(
    fs.open("warehouse/events/part-new.parquet", ref="ingest-2026-02"),
)

# PUBLISH: a clean merge lands as one revision; a conflicting one aborts and raises.
fs.merge("ingest-2026-02")
```

## Development

```bash
uv sync           # install deps (fsspec, lore-vcs)
uv run pytest     # run tests
```

`lore` ships a platform-specific `liblore` shared library; tests skip when the
current platform's library isn't available.

## License

MIT (matching Lore).
