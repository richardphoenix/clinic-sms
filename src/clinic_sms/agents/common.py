"""Pieces shared by every clinic agent."""

from __future__ import annotations

from livekit.agents import RunContext, function_tool

from clinic_sms.effects import CreateTicket
from clinic_sms.turn import Turn

SMS_STYLE = (
    "You are texting a patient of Maple Street Family Clinic by SMS. "
    "Reply in plain English text, no markdown, at most two short sentences. "
    # a real-Claude run once texted a patient that it was calling a tool
    "Write only the text message itself; never mention tools, function calls or your own "
    "reasoning."
)

# Every top-level agent includes this. A handoff passes the conversation on, but each
# agent's context drops the calls of tools it does not have (Agent.__init__ copies it
# with tools=..., voice/agent.py:92). Without this line an agent read another agent's
# messages (the slots it offered, the booking, the intake) as its own unsupported
# claims, and retracted them (real-Claude smoke t6b, docs/findings.md "Manual check").
# The second sentence keeps those actions from being retracted; the third settles which
# wins when an earlier message is out of date (e.g. the appointment time, after a booking).
OTHER_ASSISTANTS = (
    "Earlier assistant messages in this conversation may have been written by other "
    "clinic assistants (scheduling, intake) using tools you do not have. What they did "
    "really happened: never retract their messages or apologize for them. Where a fact in "
    "your instructions is newer than an earlier message, such as the current appointment "
    "time, go by your instructions."
)

# Every agent and intake step that has escalate_to_staff includes this (spec §5.4).
ESCALATION_RULE = (
    "If the patient asks something you cannot answer from what you know, such as a "
    "question about billing, charges or what their insurance covers, call "
    "escalate_to_staff with their question instead of guessing."
)

ESCALATED_OUTPUT = (
    "Sent to the front desk. Tell the patient the front desk will text them back, "
    "then continue with your current task."
)


@function_tool
async def escalate_to_staff(ctx: RunContext[Turn], question: str) -> str:
    """Send the patient's question to the clinic's front desk, who will text the patient back.

    Args:
        question: The patient's question, as one short sentence in English.
    """
    turn = ctx.userdata
    # the number and the name come from the turn, never from the model, so a ticket can
    # only ever name the patient who is texting
    turn.effects.append(CreateTicket(turn.phone, question, turn.patient_label))
    return ESCALATED_OUTPUT
