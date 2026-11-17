"""The types of a proactive turn: a message the clinic starts (a leaf module).

Kept apart from the runner so that jobs can use them without importing the agents.
"""

from __future__ import annotations

from dataclasses import dataclass

from clinic_sms.effects import Effect


class NoConversationError(RuntimeError):
    """A proactive message for a number with no conversation, which it may not start."""


@dataclass(frozen=True)
class Proactive:
    """A message the clinic sends first (a proactive turn).

    `instructions` tell the active agent what to send; `effects` commit with the turn.
    """

    instructions: str
    effects: tuple[Effect, ...] = ()
    followup: bool = False  # this message counts toward MAX_FOLLOWUPS
    may_start: bool = False  # no conversation yet: start one with the reception agent (reminders)
