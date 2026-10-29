"""Exit codes, and the reason a program needs to know about.

Three branches only - callers should not have to learn ten.

`reason` is the machine-readable half of an actionable failure. The CLI prints
`message` (Chinese, for a person); a program switches on `reason` (a stable
token, for code). Both travel on the same exception on purpose: whoever raises
knows *why* the user must act, and that knowledge is lost the moment it is
flattened into a sentence for someone to match on.

Matching Chinese error text to decide control flow is the failure mode this
field exists to prevent - it breaks the first time a message is improved, and
it breaks silently.
"""
from __future__ import annotations

OK = 0            # success, including "table produced with unparsed lines"
BAD_INPUT = 2     # empty clipboard, invalid card, channel unavailable
INTERNAL = 3      # our bug, not the user's

# Why a human has to do something before the operation can continue.
# The vocabulary is fixed here so `api` and the CLI cannot invent different
# spellings of the same situation.
NEEDS_ACTION = (
    "structure_selection",           # the reading is ambiguous, pick one
    "target_confirmation",           # which window / tab is the source?
    "login_required",                # the site wants credentials, in the user's browser
    "verification_required",         # captcha / slider / interstitial wall
    "resume_position_confirmation",  # cannot prove which page we are on
    "conflict_resolution",           # two observations disagree
    "revision_selection",            # a candidate revision is ready, activate it?
    "legacy_capture_type_required",  # old page has no kind: text or html?
)


class Clip2TableError(Exception):
    """Any condition the user can act on. Carries its own exit code.

    `reason` is empty for ordinary bad input. It is set only when the caller has
    to hand control back to a human, and only with a token from `NEEDS_ACTION`.
    """

    exit_code = INTERNAL

    def __init__(self, message: str, *, hint: str = "", reason: str = "",
                 code: str = "", details=None):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.reason = reason
        self.code = code or ("channel_unavailable" if isinstance(self, ChannelError)
                             else "bad_input" if isinstance(self, InputError) else "internal")
        self.details = details or {}

    @property
    def needs_action(self) -> bool:
        return self.reason in NEEDS_ACTION


class InputError(Clip2TableError):
    """Bad input or configuration."""

    exit_code = BAD_INPUT


class ChannelError(Clip2TableError):
    """A channel could not be used."""

    exit_code = BAD_INPUT


class InternalError(Clip2TableError):
    """A defect in clip2table itself."""

    exit_code = INTERNAL
