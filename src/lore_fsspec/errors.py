"""Error handling: map Lore's event-stream failures onto Python exceptions.

Every Lore command ends with a ``LoreCompleteEventData(status)`` (``0`` success,
``1`` failure) followed by ``LoreEndEventData``. A failure additionally emits one
or more ``LoreErrorEventData(error_type, error_inner)`` where ``error_type`` is a
``LoreErrorCode`` value. :func:`raise_for_events` inspects a collected event list
and raises :class:`LoreError` (a subclass of the closest builtin) on failure.
"""

from __future__ import annotations

from lore.types.enums import LoreErrorCode
from lore.types.events import LoreCompleteEventData, LoreErrorEventData


class LoreError(Exception):
    """A Lore command failed.

    Carries the originating :class:`LoreErrorCode` and the library's
    ``error_inner`` message. Subclasses below also inherit from a builtin
    (``FileNotFoundError``/``ValueError``) so fsspec callers can catch the
    idiomatic exception type.
    """

    def __init__(self, code: LoreErrorCode | int | None, inner: str = "") -> None:
        """Initialize with an error code and optional message."""
        try:
            self.code = LoreErrorCode(int(code)) if code is not None else None
        except (ValueError, TypeError):
            self.code = None
        self.inner = inner
        name = self.code.name if self.code is not None else "ERROR"
        super().__init__(f"[{name}] {inner}" if inner else name)


class LoreFileNotFoundError(LoreError, FileNotFoundError):
    """Raised when a Lore address or path is not found."""


class LoreInvalidArgumentsError(LoreError, ValueError):
    """Raised when Lore rejects arguments as invalid."""


LoreInvalidArguments = LoreInvalidArgumentsError


# LoreErrorCode -> LoreError subclass. Unmapped codes fall back to LoreError.
_CODE_TO_EXC: dict[LoreErrorCode, type[LoreError]] = {
    LoreErrorCode.ADDRESS_NOT_FOUND: LoreFileNotFoundError,
    LoreErrorCode.INVALID_ARGUMENTS: LoreInvalidArgumentsError,
}


def _exc_for(code: LoreErrorCode | int | None) -> type[LoreError]:
    try:
        code = LoreErrorCode(int(code))
    except (ValueError, TypeError):
        return LoreError
    return _CODE_TO_EXC.get(code, LoreError)


def raise_for_events(events: list) -> None:
    """Raise an appropriate exception if ``events`` reports a failure.

    Prefers an explicit ``LoreErrorEventData`` (it carries the code + message);
    otherwise falls back to a non-zero ``LoreCompleteEventData.status``.
    """
    for ev in events:
        if isinstance(ev, LoreErrorEventData):
            raise _exc_for(ev.error_type)(ev.error_type, ev.error_inner)
    for ev in events:
        if isinstance(ev, LoreCompleteEventData) and ev.status != 0:
            raise LoreError(None, f"command failed with status {ev.status}")
