"""Exit codes. Three branches only - callers should not have to learn ten."""
from __future__ import annotations

OK = 0            # success, including "table produced with unparsed lines"
BAD_INPUT = 2     # empty clipboard, invalid card, channel unavailable
INTERNAL = 3      # our bug, not the user's


class Clip2TableError(Exception):
    """Any condition the user can act on. Carries its own exit code."""

    exit_code = INTERNAL

    def __init__(self, message: str, *, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint


class InputError(Clip2TableError):
    """Bad input or configuration."""

    exit_code = BAD_INPUT


class ChannelError(Clip2TableError):
    """A channel could not be used."""

    exit_code = BAD_INPUT


class InternalError(Clip2TableError):
    """A defect in clip2table itself."""

    exit_code = INTERNAL
