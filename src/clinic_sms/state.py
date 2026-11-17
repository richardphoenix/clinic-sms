from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AgentName = Literal["reception", "reschedule", "intake"]


class GroupProgress(BaseModel):
    # holds the patient's words and number: keep them out of error messages and logs
    model_config = ConfigDict(hide_input_in_errors=True)

    pending: list[str]  # step ids still to run, current step first
    results: dict[str, dict]  # step id -> result.model_dump(mode="json")
    visited: list[str]


class IntakeState(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    done: bool = False
    progress: GroupProgress | None = None  # None: intake not started (or done)
    root_ctx: dict | None = None
    group_ctx: dict | None = None
    step_ctx: dict | None = None
    results: dict[str, dict] = Field(default_factory=dict)  # final results once done


class ConversationState(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    phone: str  # E.164
    intake: IntakeState = Field(default_factory=IntakeState)
    # Added in Plan 2, all defaulted: state stored by Plan 1 still parses.
    active: AgentName | None = None
    agent_ctx: dict | None = None  # dump_ctx of the active agent: reception or reschedule
    appointment_id: int | None = None
    intake_followups_sent: int = 0

    @property
    def intake_started(self) -> bool:
        return self.intake.done or self.intake.progress is not None

    @property
    def active_agent(self) -> AgentName | None:
        """The agent that handles the next turn; None: no conversation yet.

        Plan 1 stored no `active`: a started intake is its active agent.
        """
        if self.active is not None:
            return self.active
        return "intake" if self.intake_started else None
