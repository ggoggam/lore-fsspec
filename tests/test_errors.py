"""Unit tests for error mapping (no server)."""

from __future__ import annotations

import pytest

pytest.importorskip("lore", reason="liblore not available")

from lore.types.enums import LoreErrorCode
from lore.types.events import (
    LoreCompleteEventData,
    LoreEndEventData,
    LoreErrorEventData,
)

from lore_fsspec.errors import (
    LoreError,
    LoreFileNotFoundError,
    LoreInvalidArguments,
    raise_for_events,
)


def test_success_does_not_raise():
    raise_for_events([LoreCompleteEventData(status=0), LoreEndEventData(unused=0)])


def test_address_not_found_maps_to_filenotfound():
    events = [
        LoreErrorEventData(
            error_type=int(LoreErrorCode.ADDRESS_NOT_FOUND), error_inner="missing"
        ),
        LoreCompleteEventData(status=1),
    ]
    with pytest.raises(FileNotFoundError) as ei:
        raise_for_events(events)
    assert isinstance(ei.value, LoreFileNotFoundError)
    assert ei.value.code == LoreErrorCode.ADDRESS_NOT_FOUND


def test_invalid_arguments_maps_to_valueerror():
    events = [
        LoreErrorEventData(
            error_type=int(LoreErrorCode.INVALID_ARGUMENTS), error_inner="bad"
        ),
        LoreCompleteEventData(status=1),
    ]
    with pytest.raises(ValueError) as ei:
        raise_for_events(events)
    assert isinstance(ei.value, LoreInvalidArguments)


def test_nonzero_status_without_error_event_raises_loreerror():
    with pytest.raises(LoreError):
        raise_for_events([LoreCompleteEventData(status=1)])


def test_unmapped_code_raises_base_loreerror():
    events = [
        LoreErrorEventData(error_type=int(LoreErrorCode.INTERNAL), error_inner="boom"),
        LoreCompleteEventData(status=1),
    ]
    with pytest.raises(LoreError) as ei:
        raise_for_events(events)
    assert ei.value.code == LoreErrorCode.INTERNAL
