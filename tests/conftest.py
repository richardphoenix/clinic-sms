import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest
from livekit.agents import RunResult

from clinic_sms.agents import (
    AllergiesMedsTask,
    InsuranceTask,
    IntakeAgent,
    VisitReasonTask,
)
from clinic_sms.agents.common import ESCALATED_OUTPUT
from clinic_sms.agents.reschedule import (
    BOOKED_OUTPUT,
    MAX_SLOTS_LISTED,
    RescheduleAgent,
    format_slot_list,
)
from clinic_sms.channel import RecordingChannel
from clinic_sms.clinic_db import ClinicView
from clinic_sms.clock import CLINIC_TZ, Clock, SystemClock
from clinic_sms.fmt import format_when
from clinic_sms.jobs import FOLLOWUP_INSTRUCTIONS, RELAY_INSTRUCTIONS, REMINDER_INSTRUCTIONS
from clinic_sms.runner import ConversationRunner
from clinic_sms.seed import SEED_APPOINTMENT_AT, seed_demo
from clinic_sms.sim import SIM_START
from clinic_sms.state import ConversationState
from clinic_sms.store import Store
from clinic_sms.testing import Reply, ScriptedLLM, anthropic_tool_problems

P = "+15550100001"
T = datetime(2026, 11, 17, 9, 55, tzinfo=CLINIC_TZ)


def load(db: Path) -> tuple[ConversationState, int] | None:
    with Store(db) as store:
        return store.load(P)


def commit(db: Path, state: ConversationState, outbound: list[str]) -> None:
    with Store(db) as store:
        store.commit_turn(
            phone=P, expected_version=None, state=state, inbound=None, outbound=outbound, at=T
        )


def jane_slot(clinic: ClinicView, day: date, hour: int) -> int:
    """The id of Dr. Lee's open slot at `hour`:00 on `day`, clinic time (read-only)."""
    at = datetime.combine(day, time(hour), tzinfo=CLINIC_TZ)
    slots = clinic.open_slots(start=at, end=at + timedelta(minutes=1), provider_id=None, limit=10)
    (slot,) = [s for s in slots if s.provider == "Dr. Lee"]
    return slot.id


@pytest.fixture
def scripted_llm():
    """`scripted_llm(replies, default=...)`: a ScriptedLLM for a test with real sessions.

    At teardown, the tools of every call each LLM received are formatted as the Anthropic
    plugin sends them, and the test errors if any schema has an empty `enum` (Claude
    rejects the whole request with a 400; ScriptedLLM would not notice).
    """
    made: list[ScriptedLLM] = []

    def make(replies: dict[str, Reply], *, default: Reply | None = None) -> ScriptedLLM:
        llm = ScriptedLLM(replies, default=default)
        made.append(llm)
        return llm

    yield make
    problems = [p for llm in made for p in anthropic_tool_problems(llm)]
    assert problems == [], "tool schemas Anthropic would reject:\n" + "\n".join(problems)


@pytest.fixture
def intake_replies() -> dict[str, Reply]:
    return {
        InsuranceTask.OPENING: Reply(text="Hi Jane! What's your insurance carrier and member ID?"),
        "Aetna, member W123456789": Reply(
            tools=[("record_insurance", {"carrier": "Aetna", "member_id": "W123456789"})]
        ),
        "Aetna": Reply(text="Got it. What's your member ID?"),
        "W123456789": Reply(
            tools=[("record_insurance", {"carrier": "Aetna", "member_id": "W123456789"})]
        ),
        VisitReasonTask.OPENING: Reply(text="Thanks! What brings you in?"),
        "Follow-up on my knee": Reply(
            tools=[("record_visit_reason", {"reason": "Follow-up on my knee"})]
        ),
        AllergiesMedsTask.OPENING: Reply(text="Any allergies or current medications?"),
        "Penicillin; ibuprofen": Reply(
            tools=[
                (
                    "record_allergies_and_medications",
                    {"allergies": ["penicillin"], "medications": ["ibuprofen"]},
                )
            ]
        ),
        IntakeAgent.COMPLETION: Reply(text="You're all set. See you Thursday!"),
        "Actually my member ID is W999": Reply(tools=[("out_of_scope", {"task_ids": ["insurance"]})]),
        "Aetna, W999": Reply(tools=[("record_insurance", {"carrier": "Aetna", "member_id": "W999"})]),
    }


# The SMS the scripted LLM writes in scenarios/mainline.txt (`mainline_replies`).
MAINLINE_SMS = {
    "reminder": (
        "Hi Jane, this is Maple Street Family Clinic: you're booked Thu 11/19, 10:00 AM with "
        "Dr. Lee. Reply 1 to confirm or 2 to reschedule."
    ),
    "offer": "Dr. Lee has Mon 11/23 at 9:00, 10:00 or 11:00 AM. Which works for you?",
    "booked": "You're booked for Mon 11/23, 9:00 AM with Dr. Lee.",
    "insurance": "Before your visit: what's your insurance carrier and member ID?",
    "visit_reason": "Thanks! What brings you in?",
    "allergies_meds": "Any allergies or current medications?",
    "followup": "Good morning, Jane! Any allergies or current medications?",
    "escalated": (
        "I've sent that to our front desk, and they'll text you back. Meanwhile, any "
        "allergies or medications?"
    ),
    "relayed": (
        "The front desk says the $40 was your copay from your 10/2 visit, and nothing else "
        "is due. Any allergies or medications?"
    ),
    "completion": "Thanks, Jane! Your intake is complete. See you Mon 11/23 at 9:00 AM.",
}


@pytest.fixture
def mainline_replies(clinic) -> dict[str, Reply]:
    """A reply for every LLM call scenarios/mainline.txt causes, keyed as ScriptedLLM looks up.

    In order: the reminder, the transfer, the reschedule OPENING, the slot list, the
    booking, the booked note, three intake answers with their openings, the follow-up,
    the regression (out_of_scope), the re-asked insurance (InsuranceTask.OPENING again),
    the escalation, its ESCALATED_OUTPUT, the relay, the last answer and the COMPLETION.
    """
    sms = MAINLINE_SMS
    patient = clinic.patient_by_phone(P)
    appointment = clinic.next_appointment(patient.id, SIM_START)
    # what list_open_slots returns for Dr. Lee, Mon 11/23 to Fri 11/27
    slots = clinic.open_slots(
        start=datetime(2026, 11, 23, tzinfo=CLINIC_TZ),
        end=datetime(2026, 11, 28, tzinfo=CLINIC_TZ),
        provider_id=appointment.provider_id,
        limit=MAX_SLOTS_LISTED,
    )
    first = slots[0]
    assert (first.start_at, first.provider) == (
        datetime(2026, 11, 23, 9, tzinfo=CLINIC_TZ),
        "Dr. Lee",
    )
    question = "Why was I charged $40 last time?"
    answer = "That was your copay from your 10/2 visit. Nothing else is due."
    return {
        REMINDER_INSTRUCTIONS.format(
            when=format_when(SEED_APPOINTMENT_AT), provider="Dr. Lee"
        ): Reply(text=sms["reminder"]),
        "2, can I move it to next week?": Reply(tools=[("transfer_to_reschedule", {})]),
        RescheduleAgent.OPENING: Reply(
            tools=[("list_open_slots", {"start_date": "2026-11-23", "end_date": "2026-11-27"})]
        ),
        format_slot_list(slots): Reply(text=sms["offer"]),
        "The first one works": Reply(tools=[("book_slot", {"slot_id": first.id})]),
        BOOKED_OUTPUT.format(when=format_when(first.start_at), provider="Dr. Lee"): Reply(
            text=sms["booked"]
        ),
        InsuranceTask.OPENING: Reply(text=sms["insurance"]),
        "Aetna, member W123456789": Reply(
            tools=[("record_insurance", {"carrier": "Aetna", "member_id": "W123456789"})]
        ),
        VisitReasonTask.OPENING: Reply(text=sms["visit_reason"]),
        "Follow-up on my knee": Reply(
            tools=[("record_visit_reason", {"reason": "Follow-up on my knee"})]
        ),
        AllergiesMedsTask.OPENING: Reply(text=sms["allergies_meds"]),
        FOLLOWUP_INSTRUCTIONS: Reply(text=sms["followup"]),
        "Actually my member ID is wrong, it's W999": Reply(
            tools=[("out_of_scope", {"task_ids": ["insurance"]})]
        ),
        "Aetna, W999": Reply(
            tools=[("record_insurance", {"carrier": "Aetna", "member_id": "W999"})]
        ),
        "Also, why was I charged $40 last time?": Reply(
            tools=[("escalate_to_staff", {"question": question})]
        ),
        ESCALATED_OUTPUT: Reply(text=sms["escalated"]),
        RELAY_INSTRUCTIONS.format(question=question, answer=answer): Reply(text=sms["relayed"]),
        "No allergies, and I take ibuprofen daily": Reply(
            tools=[
                (
                    "record_allergies_and_medications",
                    {"allergies": [], "medications": ["ibuprofen"]},
                )
            ]
        ),
        IntakeAgent.COMPLETION: Reply(text=sms["completion"]),
    }


@pytest.fixture
def make_runner():
    """`make_runner(db, llm, ch)`: a new Store and runner each call, i.e. a process restart."""
    stores: list[Store] = []

    def make(
        db: Path,
        llm: ScriptedLLM,
        ch: RecordingChannel,
        *,
        summarize_intake: bool = False,
        ready_timeout: float = 2,
        clock: Clock | None = None,
        run_listener: Callable[[str, RunResult], None] | None = None,
    ) -> ConversationRunner:
        store = Store(db)
        stores.append(store)
        return ConversationRunner(
            store=store,
            channel=ch,
            clock=clock or SystemClock(),
            llm_factory=lambda: llm,
            ready_timeout=ready_timeout,
            summarize_intake=summarize_intake,
            run_listener=run_listener,
        )

    yield make
    for store in stores:
        store.close()


@pytest.fixture
def intake_llm(scripted_llm, intake_replies: dict[str, Reply]) -> ScriptedLLM:
    # No default: every call must be scripted. The record_* tools return None, so the
    # SDK makes no extra LLM call with their output (voice/generation.py:1241).
    return scripted_llm(intake_replies)


@pytest.fixture
def seeded_db(tmp_path) -> Path:
    """A fresh database seeded with the demo clinic data."""
    path = tmp_path / "clinic.db"
    with Store(path) as store:
        seed_demo(store, now=SIM_START)
    return path


@pytest.fixture
def db_rows():
    """`db_rows(db, sql)`: read rows on a second connection, never touching Store._conn."""

    def read(db: Path, sql: str) -> list[tuple]:
        with closing(sqlite3.connect(db)) as conn:
            return conn.execute(sql).fetchall()

    return read


@pytest.fixture
def db_exec():
    """`db_exec(db, sql, params=())`: run a setup write on its own connection and commit."""

    def run(db: Path, sql: str, params: tuple = ()) -> None:
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute(sql, params)

    return run


@pytest.fixture
def clinic(seeded_db):
    """A ClinicView of the seeded database on its own connection, for reads only."""
    with Store(seeded_db) as store:
        yield store.clinic


@pytest.fixture
def jane_appointment(seeded_db):
    """`jane_appointment()`: Jane's appointment as stored now (read-only)."""

    def read():
        with Store(seeded_db) as store:
            patient = store.clinic.patient_by_phone(P)
            return store.clinic.next_appointment(patient.id, datetime(2026, 1, 1, tzinfo=CLINIC_TZ))

    return read
