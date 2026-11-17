import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from livekit.agents import APIError

from clinic_sms.agents.reschedule import RescheduleAgent
from clinic_sms.channel import Outbound, RecordingChannel
from clinic_sms.cli import Command
from clinic_sms.clinic_db import Job, to_utc_iso
from clinic_sms.clock import CLINIC_TZ, SimClock
from clinic_sms.ctx import normalize_ctx
from clinic_sms.effects import CreateTicket, ScheduleJob
from clinic_sms.fmt import mask_phone
from clinic_sms.jobs import (
    REMINDER_IN_FLOW_INSTRUCTIONS,
    REMINDER_INSTRUCTIONS,
    Scheduler,
    reminder_effects,
)
from clinic_sms.runner import ConversationRunner, NoConversationError, Proactive
from clinic_sms.seed import JANE_PHONE, SEED_APPOINTMENT_AT
from clinic_sms.sim import SIM_START, Simulator, load_clock, parse_duration, save_clock
from clinic_sms.state import ConversationState
from clinic_sms.store import Store
from clinic_sms.testing import Reply, tool_names
from conftest import commit, load

SAM_PHONE = "+15550100002"
SAM_AT = datetime(2026, 11, 19, 14, 0, tzinfo=CLINIC_TZ)
TUE_10AM = datetime(2026, 11, 17, 10, 0, tzinfo=CLINIC_TZ)
TUE_2PM = datetime(2026, 11, 17, 14, 0, tzinfo=CLINIC_TZ)
JANE_REMINDER = REMINDER_INSTRUCTIONS.format(when="Thu 11/19, 10:00 AM", provider="Dr. Lee")
JANE_TEXT = "Reminder: Thu 11/19, 10:00 AM with Dr. Lee. Reply 1 to confirm or 2 to reschedule."
SAM_REMINDER = REMINDER_INSTRUCTIONS.format(when="Thu 11/19, 2:00 PM", provider="Dr. Patel")
SAM_TEXT = "Reminder: Thu 11/19, 2:00 PM with Dr. Patel. Reply 1 to confirm or 2 to reschedule."
OFFER = "I have Mon 11/23 at 9, 10 or 11 AM."
JANE_IN_FLOW = REMINDER_IN_FLOW_INSTRUCTIONS.format(when="Thu 11/19, 10:00 AM", provider="Dr. Lee")
TUE_8AM = datetime(2026, 11, 17, 8, 0, tzinfo=CLINIC_TZ)
TUE_NOON = datetime(2026, 11, 17, 12, 0, tzinfo=CLINIC_TZ)
INSTRUCTIONS_ID = "lk.agent_task.instructions"  # the SDK's id for the agent's instructions


@dataclass
class World:
    """What cli._run builds over one database: one process."""

    store: Store
    clock: SimClock
    ch: RecordingChannel
    runner: ConversationRunner
    scheduler: Scheduler
    sim: Simulator
    out: list[str]

    def messages(self, phone: str = JANE_PHONE) -> list:
        return self.store.messages(phone)

    def jobs(self, phone: str = JANE_PHONE) -> list[Job]:
        return self.store.clinic.jobs(phone)


@pytest.fixture
def world():
    """`world(db, llm, clock=None, ch=None)`: a new Store, runner, Scheduler and Simulator.

    Each call is a restart. Without `clock`, the clock is loaded from the database, as the
    CLI does.
    """
    stores: list[Store] = []

    def make(
        db: Path, llm, *, clock: SimClock | None = None, ch: RecordingChannel | None = None
    ) -> World:
        store = Store(db)
        stores.append(store)
        clock = clock or load_clock(store)
        ch = ch or RecordingChannel()
        runner = ConversationRunner(
            store=store,
            channel=ch,
            clock=clock,
            llm_factory=lambda: llm,
            ready_timeout=2,
            summarize_intake=False,
        )
        scheduler = Scheduler(store=store, runner=runner, clock=clock)
        out: list[str] = []
        sim = Simulator(
            store=store,
            clock=clock,
            runner=runner,
            scheduler=scheduler,
            phone=JANE_PHONE,
            out=out.append,
        )
        return World(store, clock, ch, runner, scheduler, sim, out)

    yield make
    for store in stores:
        store.close()


def add_sam(db: Path, db_exec) -> None:
    """A second patient with an appointment Thu 11/19 2:00 PM, Dr. Patel; reminder due Tue 2 PM."""
    db_exec(
        db,
        "INSERT INTO patients (phone, first_name, last_name) VALUES (?, 'Sam', 'Roe')",
        (SAM_PHONE,),
    )
    db_exec(
        db,
        "INSERT INTO slots (provider_id, start_at)"
        " VALUES ((SELECT id FROM providers WHERE name = 'Dr. Patel'), ?)",
        (to_utc_iso(SAM_AT),),
    )
    db_exec(
        db,
        "INSERT INTO appointments (patient_id, slot_id, status) VALUES"
        " ((SELECT id FROM patients WHERE phone = ?), (SELECT max(id) FROM slots), 'booked')",
        (SAM_PHONE,),
    )
    with Store(db) as store:
        patient = store.clinic.patient_by_phone(SAM_PHONE)
        appointment = store.clinic.next_appointment(patient.id, SIM_START)
        assert appointment.start_at == SAM_AT
        store.apply(
            reminder_effects(
                phone=SAM_PHONE, appointment_id=appointment.id, start_at=SAM_AT, now=SIM_START
            ),
            at=SIM_START,
        )


async def test_reminder_fires_48h_before_and_starts_reception(
    seeded_db, scripted_llm, world, jane_appointment
):
    llm = scripted_llm({JANE_REMINDER: Reply(text=JANE_TEXT)})
    w = world(seeded_db, llm)
    assert await w.scheduler.run_due() == 0
    w.clock.advance(timedelta(minutes=5))
    assert await w.scheduler.run_due() == 1
    assert w.ch.sent == [Outbound(JANE_PHONE, JANE_TEXT)]
    state, version = load(seeded_db)
    assert (state.active, version) == ("reception", 1)
    assert state.appointment_id == jane_appointment().id
    assert [m.at for m in w.messages()] == [TUE_10AM]
    assert [(m.direction, m.text) for m in w.messages()] == [("out", JANE_TEXT)]
    assert [j.status for j in w.jobs()] == ["done"]
    assert llm.calls == [JANE_REMINDER]
    # each job once
    assert await w.scheduler.run_due() == 0
    assert len(w.ch.sent) == 1


async def test_a_sent_reminder_that_cannot_be_delivered_waits_in_the_outbox(
    seeded_db, scripted_llm, world, caplog
):
    class DownChannel(RecordingChannel):
        async def send(self, msg: Outbound) -> None:
            raise ConnectionError("gateway down")

    llm = scripted_llm({JANE_REMINDER: Reply(text=JANE_TEXT)})
    w = world(seeded_db, llm, ch=DownChannel())
    await w.sim.advance(timedelta(minutes=5))
    # the turn committed and stands: the job is done, not retried
    assert [(j.status, j.attempts) for j in w.jobs()] == [("done", 0)]
    assert load(seeded_db)[0].active == "reception"
    (logged,) = [r.getMessage() for r in caplog.records if r.name.startswith("clinic_sms")]
    assert logged.startswith(f"delivering the message for {mask_phone(JANE_PHONE)} failed: ")
    # the next startup delivers it, once
    w = world(seeded_db, llm)
    await w.sim.startup()
    assert w.ch.sent == [Outbound(JANE_PHONE, JANE_TEXT)]
    assert llm.calls == [JANE_REMINDER]


async def test_reply_2_after_the_reminder_reaches_reschedule(seeded_db, scripted_llm, world):
    llm = scripted_llm(
        {
            JANE_REMINDER: Reply(text=JANE_TEXT),
            "2": Reply(tools=[("transfer_to_reschedule", {})]),
            RescheduleAgent.OPENING: Reply(text=OFFER),
        }
    )
    w = world(seeded_db, llm)
    await w.sim.advance(timedelta(minutes=5))
    assert w.ch.sent == [Outbound(JANE_PHONE, JANE_TEXT)]

    # restart; the patient answers the reminder
    w = world(seeded_db, llm)
    assert await w.runner.handle_inbound(JANE_PHONE, "2") == [OFFER]
    assert w.ch.sent == [Outbound(JANE_PHONE, OFFER)]
    assert load(seeded_db)[0].active == "reschedule"
    # the restored reception saw the reminder it had sent
    ctx = normalize_ctx(llm.contexts[llm.calls.index("2")])
    assert ("message", "assistant", JANE_TEXT) in ctx
    assert ctx.index(("message", "assistant", JANE_TEXT)) < ctx.index(("message", "user", "2"))


async def test_failed_proactive_turn_is_retried_then_given_up(
    seeded_db, scripted_llm, world, caplog
):
    # the error's text stands in for patient words an error might echo
    llm = scripted_llm({JANE_REMINDER: Reply(error=APIError("Jane's knee hurts", retryable=False))})
    w = world(seeded_db, llm)
    w.clock.advance(timedelta(minutes=5))
    with caplog.at_level(logging.DEBUG):
        for attempt in (1, 2, 3):
            assert await w.scheduler.run_due() == 1
            (job,) = w.jobs()
            assert (job.attempts, job.status) == (attempt, "failed" if attempt == 3 else "pending")
        assert await w.scheduler.run_due() == 0
    assert llm.calls == [JANE_REMINDER] * 3
    assert w.ch.sent == []  # no fallback: the patient has not written
    assert load(seeded_db) is None
    assert w.messages() == [] and w.store.pending_outbox() == []
    assert w.sim.smoke_summary() == (False, "SMOKE: FAIL (fallbacks=0, failed_jobs=1)")
    ours = [r for r in caplog.records if r.name == "clinic_sms.jobs"]
    assert [r.levelno for r in ours] == [logging.ERROR] * 3
    prefix = f"job {job.id} (reminder) for {mask_phone(JANE_PHONE)} failed: "
    for r in ours:
        assert r.getMessage().startswith(prefix + "livekit.agents._exceptions.APIError")
        assert r.exc_info is None
    formatter = logging.Formatter("%(name)s %(message)s")
    for r in caplog.records:
        if r.levelno >= logging.WARNING or r.name.startswith("clinic_sms"):
            line = formatter.format(r)
            assert "knee" not in line and JANE_PHONE not in line, line


async def test_advance_runs_each_due_job_once_at_its_due_time(  # Review Focus 2
    seeded_db, scripted_llm, world, db_exec
):
    add_sam(seeded_db, db_exec)
    llm = scripted_llm(
        {JANE_REMINDER: Reply(text=JANE_TEXT), SAM_REMINDER: Reply(text=SAM_TEXT)}
    )
    w = world(seeded_db, llm)
    await w.sim.advance(parse_duration("1d"))
    assert w.ch.sent == [Outbound(JANE_PHONE, JANE_TEXT), Outbound(SAM_PHONE, SAM_TEXT)]
    assert [m.at for m in w.messages(JANE_PHONE)] == [TUE_10AM]
    assert [m.at for m in w.messages(SAM_PHONE)] == [TUE_2PM]
    assert w.clock.now() == SIM_START + timedelta(days=1)
    assert load_clock(w.store).now() == SIM_START + timedelta(days=1)
    assert [j.status for j in w.jobs(JANE_PHONE) + w.jobs(SAM_PHONE)] == ["done", "done"]
    assert llm.calls == [JANE_REMINDER, SAM_REMINDER]  # each once, in due order


async def test_startup_runs_overdue_jobs(seeded_db, scripted_llm, world):
    eleven = datetime(2026, 11, 17, 11, 0, tzinfo=CLINIC_TZ)
    with Store(seeded_db) as store:
        save_clock(store, SimClock(eleven))
    llm = scripted_llm({JANE_REMINDER: Reply(text=JANE_TEXT)})
    w = world(seeded_db, llm)
    assert w.clock.now() == eleven
    await w.sim.startup()
    assert w.ch.sent == [Outbound(JANE_PHONE, JANE_TEXT)]
    assert [m.at for m in w.messages()] == [eleven]  # sent late, never back-dated
    assert [j.status for j in w.jobs()] == ["done"]


async def test_each_command_runs_the_jobs_due(seeded_db, scripted_llm, world):
    llm = scripted_llm({JANE_REMINDER: Reply(text=JANE_TEXT)})
    w = world(seeded_db, llm)
    w.clock.advance(timedelta(minutes=5))  # moved without /advance: the job is due, not run
    assert w.ch.sent == []
    await w.sim.execute(Command("time"))
    assert w.ch.sent == [Outbound(JANE_PHONE, JANE_TEXT)]


async def test_proactive_without_a_conversation_needs_may_start(seeded_db, scripted_llm, world):
    llm = scripted_llm({}, default=Reply(text="Hello!"))
    w = world(seeded_db, llm)
    with pytest.raises(NoConversationError):
        await w.runner.run_proactive(JANE_PHONE, Proactive("x"))
    # may_start starts a conversation only for a known patient
    with pytest.raises(NoConversationError):
        await w.runner.run_proactive("+15550100009", Proactive("x", may_start=True))
    assert llm.calls == []
    assert w.ch.sent == [] and w.messages() == [] and w.messages("+15550100009") == []
    assert load(seeded_db) is None


@pytest.mark.parametrize("path", ["inbound", "proactive"])
async def test_a_row_without_an_active_agent_starts_reception_with_its_version(
    path, seeded_db, scripted_llm, world, jane_appointment
):
    # Plan 1 stored empty states: such a row is no conversation yet, but the turn that
    # starts one updates it (its version), instead of inserting a second row
    commit(seeded_db, ConversationState(phone=JANE_PHONE), [])
    assert load(seeded_db)[0].active_agent is None
    llm = scripted_llm(
        {"Hi": Reply(text="Hi Jane!"), "nudge": Reply(text="Hi Jane, a quick note.")}
    )
    w = world(seeded_db, llm)
    if path == "inbound":
        sent = await w.runner.handle_inbound(JANE_PHONE, "Hi")
        assert sent == ["Hi Jane!"]
    else:
        with pytest.raises(NoConversationError):
            await w.runner.run_proactive(JANE_PHONE, Proactive("nudge"))
        assert load(seeded_db)[1] == 1
        sent = await w.runner.run_proactive(JANE_PHONE, Proactive("nudge", may_start=True))
        assert sent == ["Hi Jane, a quick note."]
    state, version = load(seeded_db)
    assert (state.active, version, state.appointment_id) == (
        "reception",
        2,
        jane_appointment().id,
    )


@pytest.mark.parametrize("why", ["missing", "started"])
async def test_reminder_for_a_missing_or_started_appointment_is_cancelled(
    why, seeded_db, scripted_llm, world, db_exec
):
    if why == "missing":
        db_exec(seeded_db, "DELETE FROM appointments")
        clock = SimClock(TUE_10AM)
    else:
        clock = SimClock(SEED_APPOINTMENT_AT + timedelta(minutes=30))
    llm = scripted_llm({}, default=Reply(text="Hello!"))
    w = world(seeded_db, llm, clock=clock)
    assert await w.scheduler.run_due() == 1
    assert [(j.status, j.attempts) for j in w.jobs()] == [("cancelled", 0)]
    assert llm.calls == [] and w.ch.sent == [] and w.messages() == []
    assert load(seeded_db) is None


# A relay passes on the front desk's answer: with no answered ticket there is nothing to send.
@pytest.mark.parametrize("ticket", ["missing", "open"])
async def test_a_staff_relay_without_an_answered_ticket_is_cancelled(
    ticket, seeded_db, scripted_llm, world
):
    llm = scripted_llm({}, default=Reply(text="Hello!"))
    w = world(seeded_db, llm, clock=SimClock(SIM_START))
    if ticket == "missing":
        ticket_id = 99
    else:
        # exists, but the front desk has not answered it yet
        w.store.apply([CreateTicket(JANE_PHONE, "How much is my copay?", "Jane D.")], at=SIM_START)
        (open_ticket,) = w.store.clinic.tickets(JANE_PHONE)
        ticket_id = open_ticket.id
    w.store.apply(
        [ScheduleJob("staff_relay", JANE_PHONE, SIM_START, {"ticket_id": ticket_id})], at=SIM_START
    )
    outbox = w.store.pending_outbox()
    assert await w.scheduler.run_due() == 1
    (job,) = [j for j in w.jobs() if j.kind == "staff_relay"]
    assert (job.status, job.attempts) == ("cancelled", 0)
    assert llm.calls == [] and w.ch.sent == [] and w.messages() == []
    assert w.store.pending_outbox() == outbox  # no SMS queued either
    if ticket == "open":
        assert w.store.clinic.ticket(ticket_id).status == "open"


async def test_advance_stops_running_jobs_when_the_scheduler_fails(
    seeded_db, scripted_llm, world, monkeypatch, caplog
):
    # a job whose failure cannot even be recorded stays pending: advance must not loop on it
    def record_fails(self, job_id, *, at):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(Store, "record_job_failure", record_fails)
    llm = scripted_llm({JANE_REMINDER: Reply(error=APIError("down", retryable=False))})
    w = world(seeded_db, llm)
    await w.sim.advance(timedelta(hours=1))
    assert w.clock.now() == SIM_START + timedelta(hours=1)
    assert llm.calls == [JANE_REMINDER]
    errors = [r.getMessage() for r in caplog.records if r.name == "clinic_sms.sim"]
    assert len(errors) == 1
    assert errors[0].startswith("running due jobs failed: builtins.RuntimeError")


async def test_state_shows_the_conversation_jobs_and_tickets(seeded_db, scripted_llm, world):
    llm = scripted_llm({JANE_REMINDER: Reply(text=JANE_TEXT)})
    w = world(seeded_db, llm)
    await w.sim.execute(Command("state"))
    assert w.out[-1] == "no conversation for this number"
    await w.sim.advance(timedelta(minutes=5))
    sat = datetime(2026, 11, 21, 9, 0, tzinfo=CLINIC_TZ)
    w.store.apply(
        [
            ScheduleJob("reminder", JANE_PHONE, sat, {"appointment_id": 1}),
            CreateTicket(JANE_PHONE, "How much is my copay?", "Jane D."),
        ],
        at=w.clock.now(),
    )
    await w.sim.execute(Command("state"))
    assert json.loads(w.out[-1]) == {
        "active": "reception",
        "appointment": {"when": "Thu 11/19, 10:00 AM", "provider": "Dr. Lee", "status": "booked"},
        "intake": {"done": False, "progress": None, "results": {}},
        # the reminder already sent is done: only pending jobs are listed
        "jobs": [{"kind": "reminder", "due": "Sat 11/21, 9:00 AM", "status": "pending"}],
        "tickets": [{"id": 1, "status": "open"}],
    }


def system_texts(llm, key: str) -> list[str]:
    """The system messages in the context of the LLM call for `key`: instructions."""
    ctx = llm.contexts[llm.calls.index(key)]
    return [i.text_content for i in ctx.items if i.type == "message" and i.role == "system"]


def agent_instructions(llm, key: str) -> str:
    """The agent instructions in the context of the LLM call for `key`."""
    ctx = llm.contexts[llm.calls.index(key)]
    (text,) = [i.text_content for i in ctx.items if i.id == INSTRUCTIONS_ID]
    return text


def assert_in_flow_reminder(llm) -> None:
    """The reminder went out with the in-flow instructions; nothing asked for "reply 1"."""
    assert llm.calls.count(JANE_IN_FLOW) == 1 and JANE_REMINDER not in llm.calls
    texts = system_texts(llm, JANE_IN_FLOW)
    assert texts[-1] == JANE_IN_FLOW  # the reply's own instructions come last
    assert not any("reply 1" in text.lower() for text in texts), texts


async def test_a_reminder_during_the_intake_offers_no_choices_and_resets_the_followup(
    seeded_db, scripted_llm, intake_replies, world
):
    reminded = "A reminder: Thu 11/19, 10:00 AM with Dr. Lee. Now, what brings you in?"
    llm = scripted_llm({**intake_replies, JANE_IN_FLOW: Reply(text=reminded)})
    w = world(seeded_db, llm, clock=SimClock(TUE_8AM))
    assert await w.runner.start_intake(JANE_PHONE)
    await w.runner.handle_inbound(JANE_PHONE, "Aetna, member W123456789")
    progress = load(seeded_db)[0].intake.progress
    assert progress.pending[0] == "visit_reason"

    await w.sim.advance(timedelta(hours=2))  # the reminder falls due at 10:00 AM
    assert_in_flow_reminder(llm)
    assert "Your topic: the reason for the patient's visit." in agent_instructions(
        llm, JANE_IN_FLOW
    )
    assert w.ch.sent[-1] == Outbound(JANE_PHONE, reminded)
    assert w.messages()[-1].at == TUE_10AM
    state = load(seeded_db)[0]
    assert (state.active, state.intake.progress) == ("intake", progress)
    assert state.intake_followups_sent == 0  # a reminder is not a follow-up
    assert [j.status for j in w.jobs() if j.kind == "reminder"] == ["done"]
    # The reminder turn resets the follow-up timer: 4 hours from 10:00 AM, not from the
    # patient's 8:00 AM answer.
    assert [(j.due_at, j.status) for j in w.jobs() if j.kind == "intake_followup"] == [
        (TUE_NOON, "cancelled"),  # start_intake
        (TUE_NOON, "cancelled"),  # the patient's answer
        (TUE_2PM, "pending"),  # the reminder
    ]


async def test_a_reminder_while_rescheduling_offers_no_choices(
    seeded_db, scripted_llm, world, jane_appointment
):
    reminded = "A reminder: Thu 11/19, 10:00 AM with Dr. Lee. Do any of those new times work?"
    llm = scripted_llm(
        {
            "2": Reply(tools=[("transfer_to_reschedule", {})]),
            RescheduleAgent.OPENING: Reply(text=OFFER),
            JANE_IN_FLOW: Reply(text=reminded),
        }
    )
    w = world(seeded_db, llm, clock=SimClock(TUE_8AM))
    assert await w.runner.handle_inbound(JANE_PHONE, "2") == [OFFER]
    appointment = jane_appointment()

    await w.sim.advance(timedelta(hours=2))  # the reminder falls due at 10:00 AM
    assert_in_flow_reminder(llm)
    assert "You help the patient move their appointment" in agent_instructions(
        llm, JANE_IN_FLOW
    )
    assert tool_names(llm.tools[llm.calls.index(JANE_IN_FLOW)]) == [
        "book_slot",
        "escalate_to_staff",
        "list_open_slots",
        "transfer_to_reception",
    ]
    assert w.ch.sent == [Outbound(JANE_PHONE, OFFER), Outbound(JANE_PHONE, reminded)]
    assert load(seeded_db)[0].active == "reschedule"
    assert jane_appointment() == appointment
    assert [(j.kind, j.status) for j in w.jobs()] == [("reminder", "done")]


async def test_a_reminder_to_reception_still_asks_for_1_or_2(seeded_db, scripted_llm, world):
    llm = scripted_llm({"Hi": Reply(text="Hi Jane!"), JANE_REMINDER: Reply(text=JANE_TEXT)})
    w = world(seeded_db, llm, clock=SimClock(TUE_8AM))
    assert await w.runner.handle_inbound(JANE_PHONE, "Hi") == ["Hi Jane!"]
    await w.sim.advance(timedelta(hours=2))
    assert llm.calls == ["Hi", JANE_REMINDER]
    assert w.ch.sent[-1] == Outbound(JANE_PHONE, JANE_TEXT)


async def test_state_without_a_conversation_still_shows_the_jobs(seeded_db, scripted_llm, world):
    llm = scripted_llm({})
    w = world(seeded_db, llm)  # a fresh seeded database: Tue 11/17, 9:55 AM
    assert w.clock.now() == SIM_START
    await w.sim.execute(Command("state"))
    assert w.out[-1] == "no conversation for this number"
    assert json.loads(w.out[-2]) == {
        "active": None,
        "appointment": {"when": "Thu 11/19, 10:00 AM", "provider": "Dr. Lee", "status": "booked"},
        "intake": {"done": False, "progress": None, "results": {}},
        "jobs": [{"kind": "reminder", "due": "Tue 11/17, 10:00 AM", "status": "pending"}],
        "tickets": [],
    }
    assert llm.calls == [] and load(seeded_db) is None
