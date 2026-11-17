import pytest
from livekit.agents import APIError

from clinic_sms.agents.common import OTHER_ASSISTANTS
from clinic_sms.agents.reception import (
    CONFIRMED_OUTPUT,
    INTAKE_DONE,
    INTAKE_IN_PROGRESS,
    INTAKE_NOT_STARTED,
    intake_status,
)
from clinic_sms.channel import RecordingChannel
from clinic_sms.ctx import normalize_ctx, load_ctx
from clinic_sms.runner import FALLBACK_TEXT
from clinic_sms.sim import SIM_START, SimClock
from clinic_sms.state import GroupProgress, IntakeState
from clinic_sms.testing import Reply
from conftest import P, load

HOURS = "What are your hours on Friday?"
CONFIRMED = CONFIRMED_OUTPUT.format(when="Thu 11/19, 10:00 AM", provider="Dr. Lee")


def runner(make_runner, db, llm, ch):
    return make_runner(db, llm, ch, clock=SimClock(SIM_START))


async def test_known_patient_first_message_starts_reception(
    seeded_db, scripted_llm, make_runner, jane_appointment
):
    llm = scripted_llm({HOURS: Reply(text="We're open 8 to 5 on Friday.")})
    ch = RecordingChannel()
    sent = await runner(make_runner, seeded_db, llm, ch).handle_inbound(P, HOURS)
    assert sent == ["We're open 8 to 5 on Friday."]
    state, version = load(seeded_db)
    assert (state.active, version, state.appointment_id) == (
        "reception",
        1,
        jane_appointment().id,
    )
    assert ("message", "user", HOURS) in normalize_ctx(load_ctx(state.agent_ctx))
    (instructions,) = [
        i.text_content for i in llm.contexts[0].items if i.id == "lk.agent_task.instructions"
    ]
    assert OTHER_ASSISTANTS in instructions and INTAKE_NOT_STARTED in instructions


@pytest.mark.parametrize(
    ("intake", "line"),
    [
        (IntakeState(), INTAKE_NOT_STARTED),
        (
            IntakeState(
                progress=GroupProgress(pending=["insurance"], results={}, visited=["insurance"])
            ),
            INTAKE_IN_PROGRESS,
        ),
        (IntakeState(done=True), INTAKE_DONE),
    ],
    ids=["not_started", "in_progress", "done"],
)
def test_intake_status_line(intake, line):
    assert intake_status(intake) == line


async def test_reception_restored_after_restart_sees_earlier_messages(
    seeded_db, scripted_llm, make_runner
):
    llm = scripted_llm(
        {
            HOURS: Reply(text="We're open 8 to 5 on Friday."),
            "And Saturday?": Reply(text="Closed on weekends."),
        }
    )
    ch = RecordingChannel()
    await runner(make_runner, seeded_db, llm, ch).handle_inbound(P, HOURS)
    sent = await runner(make_runner, seeded_db, llm, ch).handle_inbound(P, "And Saturday?")
    assert sent == ["Closed on weekends."]
    ctx = normalize_ctx(llm.contexts[llm.calls.index("And Saturday?")])
    assert ("message", "user", HOURS) in ctx
    assert ("message", "assistant", "We're open 8 to 5 on Friday.") in ctx


async def test_confirm_appointment_commits_with_the_turn(
    seeded_db, scripted_llm, make_runner, jane_appointment
):
    llm = scripted_llm(
        {
            "1": Reply(tools=[("confirm_appointment", {})]),
            CONFIRMED: Reply(text="You're confirmed!"),
        }
    )
    sent = await runner(make_runner, seeded_db, llm, RecordingChannel()).handle_inbound(P, "1")
    assert sent == ["You're confirmed!"]
    assert jane_appointment().status == "confirmed"
    assert load(seeded_db)[1] == 1


async def test_effects_are_dropped_when_the_turn_fails(
    seeded_db, scripted_llm, make_runner, jane_appointment
):
    llm = scripted_llm(
        {
            "1": Reply(tools=[("confirm_appointment", {})]),
            CONFIRMED: Reply(error=APIError("boom", retryable=False)),
        }
    )
    sent = await runner(make_runner, seeded_db, llm, RecordingChannel()).handle_inbound(P, "1")
    assert sent == [FALLBACK_TEXT]
    assert jane_appointment().status == "booked"
    assert load(seeded_db) is None


async def test_intake_completion_hands_back_to_reception_and_saves_results(
    seeded_db, scripted_llm, intake_replies, make_runner, jane_appointment
):
    llm = scripted_llm({**intake_replies, "Thanks!": Reply(text="Anytime!")})
    ch = RecordingChannel()
    await runner(make_runner, seeded_db, llm, ch).start_intake(P)
    for answer in ("Aetna, member W123456789", "Follow-up on my knee", "Penicillin; ibuprofen"):
        await runner(make_runner, seeded_db, llm, ch).handle_inbound(P, answer)
    state = load(seeded_db)[0]
    assert (state.active, state.intake.done, state.intake.progress) == ("reception", True, None)
    assert jane_appointment().intake == {
        "insurance": {"carrier": "Aetna", "member_id": "W123456789"},
        "visit_reason": {"reason": "Follow-up on my knee"},
        "allergies_meds": {"allergies": ["penicillin"], "medications": ["ibuprofen"]},
    }
    sent = await runner(make_runner, seeded_db, llm, ch).handle_inbound(P, "Thanks!")
    assert sent == ["Anytime!"]
    ctx = normalize_ctx(llm.contexts[llm.calls.index("Thanks!")])
    assert ("message", "assistant", "You're all set. See you Thursday!") in ctx
