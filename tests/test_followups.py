from datetime import UTC, datetime, timedelta

import pytest
from livekit.agents import APIError

from clinic_sms.agents import AllergiesMedsTask, InsuranceTask, VisitReasonTask
from clinic_sms.agents.reschedule import RescheduleAgent
from clinic_sms.clinic_db import MAX_JOB_ATTEMPTS
from clinic_sms.clock import CLINIC_TZ, SimClock
from clinic_sms.effects import CancelJobs, FinishJob, ScheduleJob
from clinic_sms.jobs import (
    FOLLOWUP_INSTRUCTIONS,
    MAX_FOLLOWUPS,
    followup_due,
    followup_effects,
    intake_pending,
    shift_out_of_quiet_hours,
)
from clinic_sms.runner import (
    EmptyReplyError,
    NoConversationError,
    Proactive,
    ProactiveHandoffError,
)
from clinic_sms.seed import JANE_PHONE
from clinic_sms.sim import parse_duration
from clinic_sms.state import ConversationState, IntakeState
from clinic_sms.store import Store
from clinic_sms.testing import Reply
from conftest import commit, load
from test_jobs import JANE_REMINDER, World, world  # noqa: F401

TUE = datetime(2026, 11, 17, tzinfo=CLINIC_TZ)
TUE_5PM = datetime(2026, 11, 17, 17, 0, tzinfo=CLINIC_TZ)
NUDGE = "Hi Jane, just checking in."


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=CLINIC_TZ)


def finish_seed_reminder(store: Store, now: datetime) -> None:
    """Cancel Jane's seeded reminder (due Tue 10:00 AM), so a test sees only follow-ups."""
    (job,) = [j for j in store.clinic.jobs(JANE_PHONE) if j.kind == "reminder"]
    store.apply([FinishJob(job.id, "cancelled")], at=now)


def pending_followups(w: World) -> list[datetime]:
    return [
        j.due_at for j in w.jobs() if j.kind == "intake_followup" and j.status == "pending"
    ]


def state_with(**update) -> ConversationState:
    return ConversationState(phone=JANE_PHONE).model_copy(update=update)


@pytest.mark.parametrize(
    "local,expected",
    [
        ((17, 0), (1, 8, 0)),  # 5:00 PM + 4h = 9:00 PM -> next day 8:00 AM (spec §5.6 example)
        ((16, 59), (0, 20, 59)),  # 8:59 PM stays
        ((19, 30), (1, 8, 0)),  # 11:30 PM -> next day 8:00 AM
        ((3, 0), (0, 8, 0)),  # 7:00 AM -> same day 8:00 AM
        ((4, 0), (0, 8, 0)),  # 8:00 AM stays
        ((8, 0), (0, 12, 0)),  # noon stays
    ],
)
def test_followup_due(local, expected):
    now = TUE.replace(hour=local[0], minute=local[1])
    day, hour, minute = expected
    assert followup_due(now) == at(17 + day, hour, minute)


def test_quiet_hours_are_in_clinic_time_whatever_the_zone_of_the_input():
    # 04:30 UTC on Wed 11/18 is 8:30 PM Tue in the clinic: sending is allowed
    utc = datetime(2026, 11, 18, 4, 30, tzinfo=UTC)
    assert shift_out_of_quiet_hours(utc) == utc
    assert shift_out_of_quiet_hours(at(17, 21, 0)) == at(18, 8, 0)
    assert shift_out_of_quiet_hours(at(17, 7, 59)) == at(17, 8, 0)


def test_followup_effects_rules():
    intake = state_with(active="intake")
    assert intake_pending(intake)
    # in intake, patient replied
    assert followup_effects(intake, now=TUE_5PM, patient_replied=True) == [
        CancelJobs(JANE_PHONE, ("intake_followup",)),
        ScheduleJob("intake_followup", JANE_PHONE, at(18, 8, 0), {}),
    ]
    # in intake, the clinic wrote (start_intake or a follow-up)
    assert followup_effects(intake, now=TUE_5PM, patient_replied=False) == [
        CancelJobs(JANE_PHONE, ("intake_followup",)),
        ScheduleJob("intake_followup", JANE_PHONE, at(18, 8, 0), {}),
    ]
    reception = state_with(active="reception")
    assert not intake_pending(reception)
    assert followup_effects(reception, now=TUE_5PM, patient_replied=True) == [
        CancelJobs(JANE_PHONE, ("intake_followup",))
    ]
    assert followup_effects(reception, now=TUE_5PM, patient_replied=False) == []
    done = state_with(active="intake", intake=IntakeState(done=True))
    assert not intake_pending(done)
    capped = state_with(active="intake", intake_followups_sent=MAX_FOLLOWUPS)
    assert followup_effects(capped, now=TUE_5PM, patient_replied=False) == [
        CancelJobs(JANE_PHONE, ("intake_followup",))
    ]
    assert followup_effects(capped, now=TUE_5PM, patient_replied=True) == [
        CancelJobs(JANE_PHONE, ("intake_followup",))
    ]


async def test_followup_next_morning_after_patient_stops_at_5pm(
    seeded_db, scripted_llm, intake_replies, world
):  # spec §5.9 #5, demo step 4
    intake_replies[FOLLOWUP_INSTRUCTIONS] = Reply(text="Morning! Any allergies or current medications?")
    llm = scripted_llm(intake_replies)
    w = world(seeded_db, llm, clock=SimClock(TUE_5PM))
    finish_seed_reminder(w.store, TUE_5PM)
    assert await w.runner.start_intake(JANE_PHONE)
    await w.runner.handle_inbound(JANE_PHONE, "Aetna, member W123456789")
    await w.runner.handle_inbound(JANE_PHONE, "Follow-up on my knee")
    assert pending_followups(w) == [at(18, 8, 0)]

    # restart: a new Store, runner, Scheduler and Simulator with the same clock
    w = world(seeded_db, llm, clock=w.clock)
    await w.sim.advance(timedelta(hours=15))
    assert w.messages()[-1].text == "Morning! Any allergies or current medications?"
    assert w.messages()[-1].at == at(18, 8, 0)
    assert load(seeded_db)[0].intake_followups_sent == 1
    assert pending_followups(w) == [at(18, 12, 0)]

    await w.sim.advance(timedelta(minutes=30))
    await w.runner.handle_inbound(JANE_PHONE, "Penicillin; ibuprofen")
    assert load(seeded_db)[0].intake.done
    assert pending_followups(w) == []
    assert llm.calls.count(InsuranceTask.OPENING) == 1
    assert llm.calls.count(VisitReasonTask.OPENING) == 1
    assert llm.calls.count(AllergiesMedsTask.OPENING) == 1


async def test_reply_cancels_pending_followups_and_schedules_one(
    seeded_db, scripted_llm, intake_llm, world
):
    w = world(seeded_db, intake_llm, clock=SimClock(at(17, 11)))
    finish_seed_reminder(w.store, w.clock.now())
    await w.runner.start_intake(JANE_PHONE)
    assert pending_followups(w) == [at(17, 15)]
    w.clock.advance(timedelta(hours=1))
    await w.runner.handle_inbound(JANE_PHONE, "Aetna, member W123456789")
    assert pending_followups(w) == [at(17, 16)]
    assert [j.status for j in w.jobs() if j.kind == "intake_followup"] == ["cancelled", "pending"]


async def test_at_most_two_followups_per_intake(seeded_db, scripted_llm, intake_replies, world):
    intake_replies[FOLLOWUP_INSTRUCTIONS] = Reply(text=NUDGE)
    llm = scripted_llm(intake_replies)
    w = world(seeded_db, llm, clock=SimClock(at(17, 11)))
    finish_seed_reminder(w.store, w.clock.now())
    await w.runner.start_intake(JANE_PHONE)
    await w.sim.advance(parse_duration("3d"))
    assert [m.text for m in w.messages() if m.direction == "out"].count(NUDGE) == MAX_FOLLOWUPS
    assert llm.calls.count(FOLLOWUP_INSTRUCTIONS) == MAX_FOLLOWUPS
    assert load(seeded_db)[0].intake_followups_sent == MAX_FOLLOWUPS
    assert pending_followups(w) == []
    assert [j.status for j in w.jobs() if j.kind == "intake_followup"] == ["done", "done"]


@pytest.mark.parametrize("why", ["intake_done", "no_conversation", "capped"])
async def test_stale_followup_is_cancelled_without_llm_call(  # Review Focus 3
    why, seeded_db, scripted_llm, world
):
    now = at(17, 11)
    llm = scripted_llm({}, default=Reply(text="Hello!"))
    w = world(seeded_db, llm, clock=SimClock(now))
    finish_seed_reminder(w.store, now)
    if why == "intake_done":
        commit(seeded_db, state_with(active="reception", intake=IntakeState(done=True)), [])
    elif why == "capped":
        commit(seeded_db, state_with(active="intake", intake_followups_sent=MAX_FOLLOWUPS), [])
    w.store.apply([ScheduleJob("intake_followup", JANE_PHONE, at(17, 12), {})], at=now)
    before = len(w.messages())
    await w.sim.advance(timedelta(hours=2))
    assert [(j.status, j.attempts) for j in w.jobs() if j.kind == "intake_followup"] == [
        ("cancelled", 0)
    ]
    assert llm.calls == [] and w.ch.sent == [] and len(w.messages()) == before


# Proactive turns that go wrong: a handoff, an empty reply, a job that always fails


async def test_a_handoff_during_a_proactive_reply_fails_the_turn(
    seeded_db, scripted_llm, world
):
    reply = Reply(text="Reminder: reply 1 or 2.", tools=[("transfer_to_reschedule", {})])
    llm = scripted_llm({JANE_REMINDER: reply, RescheduleAgent.OPENING: Reply(text="Offer")})
    w = world(seeded_db, llm)
    w.clock.advance(timedelta(minutes=5))
    assert await w.scheduler.run_due() == 1
    (job,) = w.jobs()
    assert (job.status, job.attempts) == ("pending", 1)
    assert load(seeded_db) is None
    assert w.ch.sent == [] and w.messages() == [] and w.store.pending_outbox() == []


async def test_a_proactive_turn_with_an_empty_reply_fails(seeded_db, scripted_llm, world):
    llm = scripted_llm({"go": Reply(text="")})
    w = world(seeded_db, llm)
    with pytest.raises(EmptyReplyError):
        await w.runner.run_proactive(JANE_PHONE, Proactive("go", may_start=True))
    assert load(seeded_db) is None
    assert w.ch.sent == [] and w.messages() == []


async def test_advance_across_a_job_that_always_errors(seeded_db, scripted_llm, world):
    llm = scripted_llm({JANE_REMINDER: Reply(error=APIError("down", retryable=False))})
    w = world(seeded_db, llm)
    target = w.clock.now() + timedelta(hours=1)
    await w.sim.advance(timedelta(hours=1))
    assert llm.calls == [JANE_REMINDER] * MAX_JOB_ATTEMPTS
    assert [(j.status, j.attempts) for j in w.jobs()] == [("failed", MAX_JOB_ATTEMPTS)]
    assert w.clock.now() == target
    assert w.ch.sent == []


def test_the_proactive_types_live_in_a_leaf_module():
    import clinic_sms.proactive as leaf
    import clinic_sms.runner as runner

    assert runner.Proactive is leaf.Proactive
    assert runner.NoConversationError is leaf.NoConversationError
    assert NoConversationError is leaf.NoConversationError
