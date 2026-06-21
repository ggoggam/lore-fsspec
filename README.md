# lore-fsspec

An [fsspec](https://filesystem-spec.readthedocs.io/) filesystem for
[Lore](https://epicgames.github.io/lore), Epic Games' open-source version control
system — the Lore equivalent of fsspec's built-in `GitFileSystem`.

It lets any fsspec-aware tool (pandas, pyarrow, zarr, dask, `fsspec.open`, …) read
files out of a local Lore clone at an arbitrary branch or revision via `lore://`
URLs, backed by the official `lore` Python bindings (`liblore`).

> **Status:** planning / pre-implementation. See [`docs/`](docs/).

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

## Documentation

- [`docs/01-overview.md`](docs/01-overview.md) — what Lore is, the `lore` Python
  API, and how it maps onto fsspec.
- [`docs/02-design.md`](docs/02-design.md) — `LoreFileSystem` design, URL/ref
  grammar, method-by-method mapping, open questions.
- [`docs/03-roadmap.md`](docs/03-roadmap.md) — phased delivery plan
  (read-only MVP → robustness → writes → content-addressed view).

## Development

```bash
uv sync           # install deps (fsspec, lore-vcs)
uv run pytest     # run tests
```

`lore` ships a platform-specific `liblore` shared library; tests skip when the
current platform's library isn't available.

## License

MIT (matching Lore).
