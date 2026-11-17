"""The reschedule agent: offers open slots and moves the patient's appointment to one, or
hands the patient back to reception, keeping the appointment."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import ClassVar

from livekit.agents import Agent, RunContext, function_tool, llm
from livekit.agents.llm import ToolError

from clinic_sms.agents.common import (
    ESCALATION_RULE,
    OTHER_ASSISTANTS,
    SMS_STYLE,
    escalate_to_staff,
)
from clinic_sms.agents.reception import NO_APPOINTMENT_OUTPUT
from clinic_sms.clinic_db import Slot
from clinic_sms.clock import CLINIC_TZ
from clinic_sms.effects import BookSlot
from clinic_sms.fmt import format_when
from clinic_sms.jobs import reminder_effects
from clinic_sms.state import AgentName
from clinic_sms.turn import Turn

MAX_SLOTS_LISTED = 6
NO_SLOTS_OUTPUT = "No open slots in that range."
BOOKED_OUTPUT = "Booked: {when} with {provider}."
SLOT_UNAVAILABLE = "That slot is not available. Offer other open slots."  # ToolError message
BAD_DATES = "Dates must be YYYY-MM-DD, and end_date must not be before start_date."  # ToolError
# transfer_to_reception's output, which the reschedule agent answers with its last reply:
# reception never speaks on enter, so without that reply the turn would send nothing
RETURN_TO_RECEPTION_OUTPUT = (
    "Rescheduling has stopped and the appointment is unchanged. Tell the patient their "
    "appointment stays as it is; if they asked about something else, ask them to text "
    "that again."
)
# what reception's context records of that reply, which is spoken after the handoff
RESCHEDULE_STOPPED = "Rescheduling stopped: the appointment stays as it is."

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def format_slot_list(slots: list[Slot]) -> str:
    """One "slot {id}: {when} with {provider}" line per slot; NO_SLOTS_OUTPUT if none."""
    if not slots:
        return NO_SLOTS_OUTPUT
    return "\n".join(
        f"slot {slot.id}: {format_when(slot.start_at)} with {slot.provider}" for slot in slots
    )


def _parse_date(text: str) -> date:
    if not _ISO_DATE.fullmatch(text):
        raise ValueError("not YYYY-MM-DD")
    return date.fromisoformat(text)


class RescheduleAgent(Agent):
    NAME: ClassVar[AgentName] = "reschedule"
    OPENING: ClassVar[str] = (
        "Find open slots that match what the patient asked for with list_open_slots, "
        "and offer up to three of them."
    )

    def __init__(
        self, *, turn: Turn, chat_ctx: llm.ChatContext | None = None, resuming: bool
    ) -> None:
        """`resuming`: restored for a later message of the same rescheduling, so on_enter
        does not offer slots again."""
        self._turn = turn
        self._resuming = resuming
        super().__init__(
            instructions=self._instructions(turn), chat_ctx=chat_ctx, tools=[escalate_to_staff]
        )

    @staticmethod
    def _instructions(turn: Turn) -> str:
        now = turn.clock.now().astimezone(CLINIC_TZ)
        appointment = (
            None if turn.appointment_id is None else turn.clinic.appointment(turn.appointment_id)
        )
        if appointment is None:
            appointment_line = "The patient's current appointment: none."
        else:
            appointment_line = (
                f"The patient's current appointment: {format_when(appointment.start_at)} "
                f"with {appointment.provider}."
            )
        return (
            f"{SMS_STYLE} You help the patient move their appointment to another time. "
            f"{OTHER_ASSISTANTS} "
            f"The current date and time: {format_when(now)} (today is "
            f"{now.date().isoformat()}, a {now:%A}). {appointment_line} "
            "Find open times only with list_open_slots, and offer only times it returned. "
            "Each slot has an id for book_slot: never show slot ids to the patient; "
            "describe a slot by its day and time. Call book_slot only for the slot the "
            "patient has clearly chosen; if it is not clear which one, ask. If the patient "
            "no longer wants to reschedule, or asks for something other than scheduling, "
            "call transfer_to_reception. "
            f"{ESCALATION_RULE}"
        )

    async def on_enter(self) -> None:
        self._turn.active = self  # first statement, before any await
        self._turn.ready.set(self)
        if not self._resuming:
            self.session.generate_reply(instructions=self.OPENING)

    @function_tool
    async def list_open_slots(self, ctx: RunContext[Turn], start_date: str, end_date: str) -> str:
        """List open appointment slots between two dates, both included.

        Args:
            start_date: The first day to search, YYYY-MM-DD, in the clinic's time zone.
            end_date: The last day to search, YYYY-MM-DD, in the clinic's time zone.
        """
        turn = ctx.userdata
        try:
            first, last = _parse_date(start_date), _parse_date(end_date)
            end = datetime.combine(last + timedelta(days=1), time(0), tzinfo=CLINIC_TZ)
        except (ValueError, OverflowError):  # OverflowError: the day after 9999-12-31
            raise ToolError(BAD_DATES) from None
        if last < first:
            raise ToolError(BAD_DATES)
        now = turn.clock.now()
        start = datetime.combine(first, time(0), tzinfo=CLINIC_TZ)
        # Only slots after now, since book_slot rejects the rest: open_slots includes
        # `start`, so start one microsecond after now.
        start = max(start, now + timedelta(microseconds=1))
        appointment = (
            None if turn.appointment_id is None else turn.clinic.appointment(turn.appointment_id)
        )
        slots = turn.clinic.open_slots(
            start=start,
            end=end,
            provider_id=None if appointment is None else appointment.provider_id,
            limit=MAX_SLOTS_LISTED,
        )
        return format_slot_list(slots)

    @function_tool
    async def book_slot(self, ctx: RunContext[Turn], slot_id: int) -> tuple[Agent, str]:
        """Move the patient's appointment to the open slot they chose.

        Args:
            slot_id: The id of the chosen slot, as listed by list_open_slots.
        """
        turn = ctx.userdata
        if turn.appointment_id is None:
            raise ToolError(NO_APPOINTMENT_OUTPUT)
        now = turn.clock.now()
        try:
            slot = turn.clinic.slot(slot_id)
        except OverflowError:  # too large for an SQLite integer: not a slot id
            slot = None
        if slot is None or slot.taken or slot.start_at <= now:
            raise ToolError(SLOT_UNAVAILABLE)
        turn.effects.append(BookSlot(turn.appointment_id, slot.id))
        turn.effects.extend(
            reminder_effects(
                phone=turn.phone,
                appointment_id=turn.appointment_id,
                start_at=slot.start_at,
                now=now,
            )
        )
        booked = BOOKED_OUTPUT.format(when=format_when(slot.start_at), provider=slot.provider)
        # The old agent's confirmation reply is spoken after this handoff ctx is copied, so
        # without this message the next agent never sees that the booking happened.
        handoff_ctx = self.chat_ctx.copy()
        handoff_ctx.add_message(role="assistant", content=booked)
        # a finished intake goes back to reception; otherwise a fresh intake starts
        next_agent = turn.handoff("reception" if turn.intake.done else "intake", handoff_ctx)
        # The old agent confirms in one SMS, then the next agent speaks (Spike 4 H1n).
        return next_agent, booked

    @function_tool
    async def transfer_to_reception(self, ctx: RunContext[Turn]) -> tuple[Agent, str]:
        """Stop rescheduling and hand the patient back to reception, keeping their appointment."""
        # Not a bare handoff: reception never speaks on enter, so this agent replies once
        # to the tool output (Spike 4 H1n) and reception answers from the next message.
        # That reply is spoken after this context is copied: the note tells reception.
        handoff_ctx = self.chat_ctx.copy()
        handoff_ctx.add_message(role="assistant", content=RESCHEDULE_STOPPED)
        return ctx.userdata.handoff("reception", handoff_ctx), RETURN_TO_RECEPTION_OUTPUT
