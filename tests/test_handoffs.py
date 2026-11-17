import logging
from datetime import date, datetime, timedelta

import pytest
from livekit.agents import APIError

from clinic_sms.agents import InsuranceTask, IntakeAgent
from clinic_sms.agents.common import OTHER_ASSISTANTS
from clinic_sms.agents.reception import INTAKE_DONE, NO_APPOINTMENT_OUTPUT
from clinic_sms.agents.reschedule import (
    BAD_DATES,
    BOOKED_OUTPUT,
    MAX_SLOTS_LISTED,
    NO_SLOTS_OUTPUT,
    RESCHEDULE_STOPPED,
    RETURN_TO_RECEPTION_OUTPUT,
    SLOT_UNAVAILABLE,
    RescheduleAgent,
    format_slot_list,
)
from clinic_sms.channel import RecordingChannel
from clinic_sms.clinic_db import to_utc_iso
from clinic_sms.clock import CLINIC_TZ
from clinic_sms.ctx import load_ctx, normalize_ctx
from clinic_sms.fmt import mask_phone
from clinic_sms.runner import FALLBACK_TEXT
from clinic_sms.seed import JANE_PHONE
from clinic_sms.sim import SIM_START, SimClock
from clinic_sms.state import ConversationState, IntakeState
from clinic_sms.testing import Reply, tool_names
from conftest import P, commit, jane_slot, load

MON = date(2026, 11, 23)
MOVE = "2, can I move it to next week?"
NEXT_WEEK = {"start_date": "2026-11-23", "end_date": "2026-11-27"}
OFFER = "I have Mon 11/23 at 9, 10 or 11 AM."
PICK = "9 AM please"
BOOKED = BOOKED_OUTPUT.format(when="Mon 11/23, 9:00 AM", provider="Dr. Lee")
BOOKED_SMS = "Booked for Mon 11/23, 9 AM."
INSURANCE_QUESTION = "Hi Jane! What's your insurance carrier and member ID?"
RESULTS = {
    "insurance": {"carrier": "Aetna", "member_id": "W123456789"},
    "visit_reason": {"reason": "Follow-up on my knee"},
    "allergies_meds": {"allergies": ["penicillin"], "medications": ["ibuprofen"]},
}


INSTRUCTIONS_ID = "lk.agent_task.instructions"  # the SDK's id for the agent's instructions


def instructions_seen(llm, key: str) -> str:
    """The agent instructions in the context of the LLM call for `key`."""
    ctx = llm.contexts[llm.calls.index(key)]
    (text,) = [i.text_content for i in ctx.items if i.id == INSTRUCTIONS_ID]
    return text


def runner(make_runner, db, llm, ch):
    # a new Store and runner each call: every step is a restart
    return make_runner(db, llm, ch, clock=SimClock(SIM_START))


def lee_next_week(clinic, provider_id):
    """What list_open_slots should find for NEXT_WEEK: Dr. Lee's first open slots."""
    start = datetime(2026, 11, 23, tzinfo=CLINIC_TZ)
    return clinic.open_slots(
        start=start, end=start + timedelta(days=5), provider_id=provider_id, limit=MAX_SLOTS_LISTED
    )


def jobs(clinic):
    return [(j.kind, j.due_at, j.status) for j in clinic.jobs(JANE_PHONE)]


async def test_handoff_chain_persists_each_step(  # spec §5.9 #4
    seeded_db, clinic, scripted_llm, intake_replies, make_runner, jane_appointment
):
    mon9 = jane_slot(clinic, MON, 9)
    offered = lee_next_week(clinic, jane_appointment().provider_id)
    llm = scripted_llm(
        {
            **intake_replies,
            MOVE: Reply(tools=[("transfer_to_reschedule", {})]),
            RescheduleAgent.OPENING: Reply(tools=[("list_open_slots", NEXT_WEEK)]),
            format_slot_list(offered): Reply(text=OFFER),
            PICK: Reply(tools=[("book_slot", {"slot_id": mon9})]),
            BOOKED: Reply(text=BOOKED_SMS),
            "Thanks!": Reply(text="Anytime!"),
        }
    )
    ch = RecordingChannel()

    # 1) reception hands off to reschedule, which offers slots in the same turn
    sent = await runner(make_runner, seeded_db, llm, ch).handle_inbound(P, MOVE)
    assert sent == [OFFER]
    # the handoff passed reception's context on: the new agent sees the request
    opening_ctx = normalize_ctx(llm.contexts[llm.calls.index(RescheduleAgent.OPENING)])
    assert ("message", "user", MOVE) in opening_ctx
    # it is told that earlier turns may be another assistant's
    assert OTHER_ASSISTANTS in instructions_seen(llm, RescheduleAgent.OPENING)
    state = load(seeded_db)[0]
    assert state.active == "reschedule"
    assert state.appointment_id == jane_appointment().id
    stored = normalize_ctx(load_ctx(state.agent_ctx))
    assert ("message", "user", MOVE) in stored
    listed = '{"end_date":"2026-11-27","start_date":"2026-11-23"}'
    assert ("function_call", "list_open_slots", listed) in stored
    assert ("message", "assistant", OFFER) in stored
    assert jane_appointment().slot_id != mon9

    # 2) restart; the restored reschedule agent books, confirms, and intake asks first
    sent = await runner(make_runner, seeded_db, llm, ch).handle_inbound(P, PICK)
    assert sent == [BOOKED_SMS, INSURANCE_QUESTION]
    state = load(seeded_db)[0]
    assert state.active == "intake" and state.intake.progress.pending[0] == "insurance"
    assert jane_appointment().slot_id == mon9
    assert jobs(clinic) == [
        ("reminder", datetime(2026, 11, 17, 10, 0, tzinfo=CLINIC_TZ), "cancelled"),
        ("reminder", datetime(2026, 11, 21, 9, 0, tzinfo=CLINIC_TZ), "pending"),
        # the intake is pending, so its follow-up is scheduled 4 hours after this turn
        ("intake_followup", datetime(2026, 11, 17, 13, 55, tzinfo=CLINIC_TZ), "pending"),
    ]
    assert llm.calls.count(RescheduleAgent.OPENING) == 1  # not re-offered after the restart
    # the restored agent saw its earlier offer
    assert ("message", "assistant", OFFER) in normalize_ctx(llm.contexts[llm.calls.index(PICK)])

    # 3) restart for each answer; the finished intake hands back to reception
    for answer in ("Aetna, member W123456789", "Follow-up on my knee", "Penicillin; ibuprofen"):
        await runner(make_runner, seeded_db, llm, ch).handle_inbound(P, answer)
    state = load(seeded_db)[0]
    assert (state.active, state.intake.done, state.intake.progress) == ("reception", True, None)
    assert jane_appointment().intake == RESULTS
    assert jane_appointment().slot_id == mon9
    assert OTHER_ASSISTANTS in instructions_seen(llm, IntakeAgent.COMPLETION)

    # 4) restart; reception answers, and its context still has the booking
    sent = await runner(make_runner, seeded_db, llm, ch).handle_inbound(P, "Thanks!")
    assert sent == ["Anytime!"]
    assert_booking_follows_the_choice(llm, "Thanks!")
    assert_reception_knows_the_earlier_turns(llm, "Thanks!")


def assert_booking_follows_the_choice(llm, key: str) -> None:
    """The context of the `key` call holds the booking, after the patient's choice."""
    assert llm.calls[-1] == key
    ctx = normalize_ctx(llm.contexts[-1])
    booked = ("message", "assistant", BOOKED)
    assert booked in ctx
    assert ctx.index(("message", "user", PICK)) < ctx.index(booked)


def assert_reception_knows_the_earlier_turns(llm, key: str) -> None:
    """Reception is told other assistants wrote earlier turns, and the intake is done.

    Without this, reception took the offer and the booking for its own unsupported
    claims and retracted them (real-Claude smoke t6b, docs/findings.md "Manual check").
    """
    instructions = instructions_seen(llm, key)
    assert OTHER_ASSISTANTS in instructions
    assert INTAKE_DONE in instructions


def reschedule_state(appointment_id: int) -> ConversationState:
    return ConversationState(phone=P, active="reschedule", appointment_id=appointment_id)


@pytest.mark.parametrize("which", ["unknown", "huge", "taken", "past"])
async def test_book_slot_rejects_unknown_taken_or_past_slot(  # Review Focus 1
    which, seeded_db, clinic, scripted_llm, make_runner, jane_appointment, db_exec, db_rows
):
    if which == "unknown":
        slot_id = 9999
    elif which == "huge":
        slot_id = 2**64  # beyond SQLite's integer range
    elif which == "taken":
        slot_id = jane_slot(clinic, MON, 10)
        db_exec(
            seeded_db,
            "INSERT INTO patients (phone, first_name, last_name)"
            " VALUES ('+15550100002', 'Sam', 'Roe')",
        )
        db_exec(
            seeded_db,
            "INSERT INTO appointments (patient_id, slot_id, status)"
            " SELECT id, ?, 'booked' FROM patients WHERE phone = '+15550100002'",
            (slot_id,),
        )
    else:
        db_exec(
            seeded_db,
            "INSERT INTO slots (provider_id, start_at) VALUES (?, ?)",
            (jane_appointment().provider_id, to_utc_iso(SIM_START - timedelta(hours=1))),
        )
        ((slot_id,),) = db_rows(seeded_db, "SELECT max(id) FROM slots")
    appointment = jane_appointment()
    commit(seeded_db, reschedule_state(appointment.id), [])
    jobs_before = jobs(clinic)
    _, version = load(seeded_db)
    llm = scripted_llm(
        {
            "That one please": Reply(tools=[("book_slot", {"slot_id": slot_id})]),
            SLOT_UNAVAILABLE: Reply(text="Sorry, that one is gone. How about 10 AM?"),
        }
    )
    sent = await runner(make_runner, seeded_db, llm, RecordingChannel()).handle_inbound(
        P, "That one please"
    )
    assert sent == ["Sorry, that one is gone. How about 10 AM?"]
    assert jane_appointment() == appointment
    assert jobs(clinic) == jobs_before
    state, new_version = load(seeded_db)
    assert (state.active, state.appointment_id, new_version) == (
        "reschedule",
        appointment.id,
        version + 1,
    )
    # the error reached the model as a failed tool call
    assert llm.contexts[llm.calls.index(SLOT_UNAVAILABLE)].items[-1].is_error


@pytest.mark.parametrize(
    "failing",
    [BOOKED, InsuranceTask.OPENING],
    ids=["confirmation_fails", "next_opening_fails"],
)
async def test_a_failed_reply_in_a_handoff_turn_writes_nothing(  # SDK-FINDING-8
    failing, seeded_db, clinic, scripted_llm, intake_replies, make_runner, jane_appointment, caplog
):
    # book_slot hands off to intake, so the turn has two replies: the old agent's
    # confirmation, then the insurance step's question. Either one failing fails the
    # whole turn, even though the other one produced text and run() did not raise.
    mon9 = jane_slot(clinic, MON, 9)
    appointment = jane_appointment()
    commit(seeded_db, reschedule_state(appointment.id), [])
    before = load(seeded_db)
    jobs_before = jobs(clinic)
    replies = {
        **intake_replies,
        PICK: Reply(tools=[("book_slot", {"slot_id": mon9})]),
        BOOKED: Reply(text=BOOKED_SMS),
    }
    replies[failing] = Reply(error=APIError("down", retryable=False))
    llm = scripted_llm(replies)
    ch = RecordingChannel()
    sent = await runner(make_runner, seeded_db, llm, ch).handle_inbound(P, PICK)
    assert sent == [FALLBACK_TEXT]
    assert [m.text for m in ch.sent] == [FALLBACK_TEXT]
    assert llm.calls == [PICK, BOOKED, InsuranceTask.OPENING]  # both replies were attempted
    assert jane_appointment() == appointment
    assert jobs(clinic) == jobs_before
    assert load(seeded_db) == before
    # logged with the type name only (the cause's text is "down")
    (failure,) = [r for r in caplog.records if r.name == "clinic_sms.runner"]
    assert failure.levelno == logging.ERROR and failure.exc_info is None
    assert failure.getMessage().startswith(f"inbound message for {mask_phone(P)} failed: ")
    assert "livekit.agents._exceptions.APIError" in failure.getMessage()


@pytest.mark.parametrize(
    "dates",
    [
        {"start_date": "2026-13-01", "end_date": "2026-13-05"},
        {"start_date": "2026-11-27", "end_date": "2026-11-23"},
        {"start_date": "next monday", "end_date": "2026-11-27"},
        {"start_date": "2026-11-23", "end_date": "9999-12-31"},
    ],
    ids=["bad_month", "end_before_start", "not_a_date", "end_of_calendar"],
)
async def test_list_open_slots_rejects_bad_dates(
    dates, seeded_db, scripted_llm, make_runner, jane_appointment
):
    commit(seeded_db, reschedule_state(jane_appointment().id), [])
    llm = scripted_llm(
        {
            "Something later?": Reply(tools=[("list_open_slots", dates)]),
            BAD_DATES: Reply(text="Which dates work for you?"),
        }
    )
    sent = await runner(make_runner, seeded_db, llm, RecordingChannel()).handle_inbound(
        P, "Something later?"
    )
    assert sent == ["Which dates work for you?"]
    assert llm.contexts[llm.calls.index(BAD_DATES)].items[-1].is_error


async def test_list_open_slots_skips_past_slots(
    seeded_db, scripted_llm, make_runner, jane_appointment, db_exec
):
    # an open slot earlier today is not offered; the booked one on Thu 11/19 is not open
    db_exec(
        seeded_db,
        "INSERT INTO slots (provider_id, start_at) VALUES (?, ?)",
        (jane_appointment().provider_id, to_utc_iso(SIM_START - timedelta(hours=1))),
    )
    commit(seeded_db, reschedule_state(jane_appointment().id), [])
    llm = scripted_llm(
        {
            "Anything this week?": Reply(
                tools=[("list_open_slots", {"start_date": "2026-11-17", "end_date": "2026-11-20"})]
            ),
            NO_SLOTS_OUTPUT: Reply(text="Nothing this week. Next week?"),
        }
    )
    sent = await runner(make_runner, seeded_db, llm, RecordingChannel()).handle_inbound(
        P, "Anything this week?"
    )
    assert sent == ["Nothing this week. Next week?"]


async def test_without_an_appointment_every_provider_is_listed_and_nothing_is_booked(
    seeded_db, clinic, scripted_llm, make_runner, db_exec, db_rows
):
    db_exec(seeded_db, "DELETE FROM appointments")
    commit(seeded_db, ConversationState(phone=P, active="reschedule"), [])
    jobs_before = jobs(clinic)
    mon9 = jane_slot(clinic, MON, 9)
    start = datetime(2026, 11, 23, tzinfo=CLINIC_TZ)
    every_provider = clinic.open_slots(
        start=start, end=start + timedelta(days=5), provider_id=None, limit=MAX_SLOTS_LISTED
    )
    assert {s.provider for s in every_provider} == {"Dr. Lee", "Dr. Patel"}
    llm = scripted_llm(
        {
            "Next week?": Reply(tools=[("list_open_slots", NEXT_WEEK)]),
            format_slot_list(every_provider): Reply(text=OFFER),
            PICK: Reply(tools=[("book_slot", {"slot_id": mon9})]),
            NO_APPOINTMENT_OUTPUT: Reply(text="I can't find an appointment to move."),
        }
    )
    ch = RecordingChannel()
    assert await runner(make_runner, seeded_db, llm, ch).handle_inbound(P, "Next week?") == [OFFER]
    sent = await runner(make_runner, seeded_db, llm, ch).handle_inbound(P, PICK)
    assert sent == ["I can't find an appointment to move."]
    assert db_rows(seeded_db, "SELECT count(*) FROM appointments") == [(0,)]
    assert jobs(clinic) == jobs_before
    assert load(seeded_db)[0].active == "reschedule"


async def test_rescheduling_after_intake_returns_to_reception(
    seeded_db, clinic, scripted_llm, make_runner, jane_appointment
):
    commit(
        seeded_db,
        ConversationState(
            phone=P,
            active="reception",
            intake=IntakeState(done=True, results=RESULTS),
            appointment_id=jane_appointment().id,
        ),
        [],
    )
    mon9 = jane_slot(clinic, MON, 9)
    offered = lee_next_week(clinic, jane_appointment().provider_id)
    llm = scripted_llm(
        {
            "2": Reply(tools=[("transfer_to_reschedule", {})]),
            RescheduleAgent.OPENING: Reply(tools=[("list_open_slots", NEXT_WEEK)]),
            format_slot_list(offered): Reply(text=OFFER),
            PICK: Reply(tools=[("book_slot", {"slot_id": mon9})]),
            BOOKED: Reply(text=BOOKED_SMS),
            "Thanks!": Reply(text="Anytime!"),
        }
    )
    ch = RecordingChannel()
    assert await runner(make_runner, seeded_db, llm, ch).handle_inbound(P, "2") == [OFFER]
    assert load(seeded_db)[0].active == "reschedule"

    sent = await runner(make_runner, seeded_db, llm, ch).handle_inbound(P, PICK)
    assert sent == [BOOKED_SMS]  # reception takes over silently
    state = load(seeded_db)[0]
    assert (state.active, state.intake) == ("reception", IntakeState(done=True, results=RESULTS))
    assert jane_appointment().slot_id == mon9
    assert InsuranceTask.OPENING not in llm.calls

    # restart: the restored reception answers, with its tools
    sent = await runner(make_runner, seeded_db, llm, ch).handle_inbound(P, "Thanks!")
    assert sent == ["Anytime!"]
    assert tool_names(llm.tools[llm.calls.index("Thanks!")]) == [
        "confirm_appointment",
        "escalate_to_staff",  # every agent can escalate (spec §5.4)
        "transfer_to_reschedule",
    ]
    # the booking is in reception's context, after the patient's choice
    assert_booking_follows_the_choice(llm, "Thanks!")
    assert_reception_knows_the_earlier_turns(llm, "Thanks!")


def test_format_slot_list(clinic):
    slots = lee_next_week(clinic, provider_id=1)[:2]
    assert format_slot_list(slots) == (
        f"slot {slots[0].id}: Mon 11/23, 9:00 AM with Dr. Lee\n"
        f"slot {slots[1].id}: Mon 11/23, 10:00 AM with Dr. Lee"
    )
    assert format_slot_list([]) == NO_SLOTS_OUTPUT


NEVER_MIND = "Never mind, keep my current time"
KEPT_SMS = "No problem, your appointment stays Thu 11/19 at 10:00 AM."
HOURS = "What are your hours?"
HOURS_SMS = "We're open Monday to Friday, 8 AM to 5 PM."
RECEPTION_TOOLS = ["confirm_appointment", "escalate_to_staff", "transfer_to_reschedule"]
RESCHEDULE_TOOLS = ["book_slot", "escalate_to_staff", "list_open_slots", "transfer_to_reception"]


async def test_reschedule_hands_back_to_reception_and_keeps_the_appointment(
    seeded_db, clinic, scripted_llm, make_runner, jane_appointment
):
    offered = lee_next_week(clinic, jane_appointment().provider_id)
    llm = scripted_llm(
        {
            MOVE: Reply(tools=[("transfer_to_reschedule", {})]),
            RescheduleAgent.OPENING: Reply(tools=[("list_open_slots", NEXT_WEEK)]),
            format_slot_list(offered): Reply(text=OFFER),
            NEVER_MIND: Reply(tools=[("transfer_to_reception", {})]),
            RETURN_TO_RECEPTION_OUTPUT: Reply(text=KEPT_SMS),
            HOURS: Reply(text=HOURS_SMS),
        }
    )
    ch = RecordingChannel()
    assert await runner(make_runner, seeded_db, llm, ch).handle_inbound(P, MOVE) == [OFFER]
    assert load(seeded_db)[0].active == "reschedule"
    appointment, jobs_before = jane_appointment(), jobs(clinic)

    # restart; the patient changes their mind: the reschedule agent answers, once
    calls_before = len(llm.calls)
    sent = await runner(make_runner, seeded_db, llm, ch).handle_inbound(P, NEVER_MIND)
    assert sent == [KEPT_SMS]
    assert llm.calls[calls_before:] == [NEVER_MIND, RETURN_TO_RECEPTION_OUTPUT]
    assert tool_names(llm.tools[llm.calls.index(NEVER_MIND)]) == RESCHEDULE_TOOLS
    # the reply after the tool is the reschedule agent's own
    reply_instructions = instructions_seen(llm, RETURN_TO_RECEPTION_OUTPUT)
    assert "You help the patient move their appointment" in reply_instructions
    state = load(seeded_db)[0]
    assert (state.active, state.appointment_id) == ("reception", appointment.id)
    assert jane_appointment() == appointment
    assert jobs(clinic) == jobs_before

    # restart; reception answers the next message, knowing the earlier turns
    sent = await runner(make_runner, seeded_db, llm, ch).handle_inbound(P, HOURS)
    assert sent == [HOURS_SMS]
    assert "You are the clinic's reception." in instructions_seen(llm, HOURS)
    assert OTHER_ASSISTANTS in instructions_seen(llm, HOURS)
    assert tool_names(llm.tools[llm.calls.index(HOURS)]) == RECEPTION_TOOLS
    ctx = normalize_ctx(llm.contexts[llm.calls.index(HOURS)])
    earlier = [
        ("message", "user", MOVE),
        ("message", "assistant", OFFER),
        ("message", "user", NEVER_MIND),
        ("message", "assistant", RESCHEDULE_STOPPED),
        ("message", "user", HOURS),
    ]
    assert [item for item in ctx if item in earlier] == earlier
    assert jane_appointment() == appointment
    assert [m.text for m in ch.sent] == [OFFER, KEPT_SMS, HOURS_SMS]
