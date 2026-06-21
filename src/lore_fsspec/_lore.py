"""Thin async wrapper over the ``lore`` package.

Isolates the cffi/event plumbing so the rest of the binding never touches it.
``lore`` exposes a genuine coroutine API (``LoreExecutor.collect_async``) backed
by native async entrypoints that resolve ``asyncio`` futures, so these helpers
run cleanly on fsspec's dedicated event-loop thread.
"""

from __future__ import annotations

from typing import Any

from .errors import raise_for_events


async def run(
    command, global_args, args, *, entry_type: type | None = None, check: bool = True
) -> list[Any]:
    """Execute a Lore command on the running loop and return collected events.

    ``command`` is a bound Lore method (e.g. ``lore.repository_dump``) taking
    ``(global_args, args)`` and returning a ``LoreExecutor``. We await
    ``collect_async()``, raise :class:`LoreError` on any failure event (unless
    ``check=False``), then optionally filter the stream down to ``entry_type``.

    ``check=False`` is for commands whose per-item outcome is reported in a
    completion event the caller wants to inspect itself (e.g. ``storage_get``,
    where one item failing should not blanket-raise before we can tell
    "not found" from a real error).
    """
    executor = command(global_args, args)
    events = await executor.collect_async()
    if check:
        raise_for_events(events)
    if entry_type is not None:
        return [e for e in events if isinstance(e, entry_type)]
    return events


def run_sync(
    command, global_args, args, *, entry_type: type | None = None
) -> list[Any]:
    """Blocking variant for one-shot construction-time calls (clone, default ref).

    Uses the executor's synchronous ``collect()`` so it can run off the fsspec
    loop thread (e.g. from ``__init__``).
    """
    executor = command(global_args, args)
    events = executor.collect()
    raise_for_events(events)
    if entry_type is not None:
        return [e for e in events if isinstance(e, entry_type)]
    return events
