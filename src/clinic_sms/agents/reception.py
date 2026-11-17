"""The reception agent: answers a known patient's texts and confirms their appointment."""

from __future__ import annotations

from typing import ClassVar

from livekit.agents import Agent, RunContext, function_tool, llm

from clinic_sms.agents.common import (
    ESCALATION_RULE,
    OTHER_ASSISTANTS,
    SMS_STYLE,
    escalate_to_staff,
)
from clinic_sms.effects import ConfirmAppointment
from clinic_sms.fmt import format_when
from clinic_sms.state import AgentName, IntakeState
from clinic_sms.turn import Turn

CLINIC_FAQ = (
    "Maple Street Family Clinic, 123 Maple Street, Springfield. Open Monday to Friday, "
    "8:00 AM to 5:00 PM; closed weekends and on Thanksgiving (Thu 11/26)."
)
CONFIRMED_OUTPUT = "Confirmed: {when} with {provider}."
NO_APPOINTMENT_OUTPUT = "The patient has no upcoming appointment."
INTAKE_DONE = "The patient's intake is complete, and their answers are saved."
INTAKE_IN_PROGRESS = "The patient's intake is in progress."
INTAKE_NOT_STARTED = "The patient's intake has not started."


def intake_status(intake: IntakeState) -> str:
    """The instructions line on the patient's intake, from its stored state."""
    if intake.done:
        return INTAKE_DONE
    return INTAKE_NOT_STARTED if intake.progress is None else INTAKE_IN_PROGRESS


class ReceptionAgent(Agent):
    NAME: ClassVar[AgentName] = "reception"

    def __init__(self, *, turn: Turn, chat_ctx: llm.ChatContext | None = None) -> None:
        self._turn = turn
        super().__init__(
            instructions=self._instructions(turn), chat_ctx=chat_ctx, tools=[escalate_to_staff]
        )

    @staticmethod
    def _instructions(turn: Turn) -> str:
        appointment = (
            None if turn.appointment_id is None else turn.clinic.appointment(turn.appointment_id)
        )
        if appointment is None:
            appointment_line = "The patient's appointment: none."
        else:
            appointment_line = (
                "The patient's appointment, as the clinic has it now (including any change "
                f"made earlier in this conversation): {format_when(appointment.start_at)} "
                f"with {appointment.provider}, status {appointment.status}."
            )
        return (
            f"{SMS_STYLE} You are the clinic's reception. {OTHER_ASSISTANTS} "
            f"Clinic facts: {CLINIC_FAQ} "
            f"The current date and time: {format_when(turn.clock.now())}. "
            f"{appointment_line} {intake_status(turn.intake)} "
            "If the patient replies 1 or asks to confirm their appointment, call "
            "confirm_appointment, then tell them it is confirmed. If the patient replies 2 "
            "or asks to move or reschedule their appointment, call transfer_to_reschedule "
            "without replying yourself; the scheduler answers them. Answer only from these "
            "facts and from this conversation. Do not make up anything you do not know. "
            f"{ESCALATION_RULE}"
        )

    async def on_enter(self) -> None:
        self._turn.active = self  # first statement, before any await
        self._turn.ready.set(self)  # never speaks: the patient's message is answered next

    @function_tool
    async def confirm_appointment(self, ctx: RunContext[Turn]) -> str:
        """Confirm the patient's upcoming appointment."""
        turn = ctx.userdata
        appointment = (
            None if turn.appointment_id is None else turn.clinic.appointment(turn.appointment_id)
        )
        if appointment is None:
            return NO_APPOINTMENT_OUTPUT
        turn.effects.append(ConfirmAppointment(appointment.id))
        return CONFIRMED_OUTPUT.format(
            when=format_when(appointment.start_at), provider=appointment.provider
        )

    @function_tool
    async def transfer_to_reschedule(self, ctx: RunContext[Turn]) -> Agent:
        """Hand the patient over to rescheduling, to move their appointment to another time."""
        # a bare handoff: no tool output, so no reply here; the reschedule agent's
        # on_enter speaks next, in the same run (Spike 4 H1)
        return ctx.userdata.handoff("reschedule", self.chat_ctx)
