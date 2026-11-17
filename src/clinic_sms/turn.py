"""Turn: the AgentSession userdata of one cold-started session."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from livekit.agents import Agent, llm

from clinic_sms.clinic_db import ClinicView
from clinic_sms.clock import Clock
from clinic_sms.effects import Effect
from clinic_sms.state import AgentName, IntakeState
from clinic_sms.workflow import ReadySignal


@dataclass
class Turn:
    """What the agents of one turn share, and the effects they queue for its commit.

    The runner builds a new Turn, with a new ReadySignal, for every cold-started
    session: ReadySignal is one-shot, so neither may be reused for another session.

    `appointment_id` is the number's own appointment: the one in its stored state, else
    its next appointment. Tools and effects take appointment ids only from here, never
    from LLM arguments.
    """

    phone: str
    clock: Clock
    clinic: ClinicView
    ready: ReadySignal  # one-shot: a new Turn (and ReadySignal) per session
    intake: IntakeState  # the stored intake state, carried into the snapshot
    appointment_id: int | None
    patient_label: str | None  # "Jane D."; None for a number not in patients
    summarize_intake: bool
    agent_factory: Callable[[Turn, AgentName, llm.ChatContext], Agent]
    effects: list[Effect] = field(default_factory=list)
    active: Agent | None = None  # set as the first statement of each top-level agent's on_enter

    def handoff(self, name: AgentName, chat_ctx: llm.ChatContext) -> Agent:
        """The agent to hand off to: `name`, taking over with `chat_ctx`."""
        return self.agent_factory(self, name, chat_ctx)
