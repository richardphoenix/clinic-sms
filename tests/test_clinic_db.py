from datetime import datetime, timedelta

import pytest

from clinic_sms.clinic_db import (
    STAFF_PHONE,
    EffectConflictError,
    SlotTakenError,
)
from clinic_sms.clock import CLINIC_TZ
from clinic_sms.effects import (
    AnswerTicket,
    BookSlot,
    CancelJobs,
    CloseTicket,
    ConfirmAppointment,
    CreateTicket,
    FinishJob,
    ScheduleJob,
)
from clinic_sms.jobs import reminder_effects
from clinic_sms.seed import JANE_PHONE, SEED_APPOINTMENT_AT, seed_demo
from clinic_sms.sim import SIM_START
from clinic_sms.state import ConversationState, IntakeState
from clinic_sms.store import Store

MON = datetime(2026, 11, 23, tzinfo=CLINIC_TZ)


def jane_appt(s):
    return s.clinic.next_appointment(s.clinic.patient_by_phone(JANE_PHONE).id, SIM_START)


def lee_monday_9am(s):
    [slot] = s.clinic.open_slots(start=MON, end=MON + timedelta(days=1), provider_id=1, limit=1)
    return slot


def turn(s, effects, *, version=None):
    return s.commit_turn(
        phone=JANE_PHONE,
        expected_version=version,
        state=ConversationState(phone=JANE_PHONE),
        inbound="q",
        outbound=["sent"],
        at=SIM_START,
        effects=effects,
    )


def test_seed_matches_the_spec_and_is_idempotent(seeded_db):
    with Store(seeded_db) as s:
        jane = s.clinic.patient_by_phone(JANE_PHONE)
        assert jane.label == "Jane D."
        appt = s.clinic.next_appointment(jane.id, SIM_START)
        assert (appt.start_at, appt.provider, appt.status) == (SEED_APPOINTMENT_AT, "Dr. Lee", "booked")
        week = s.clinic.open_slots(
            start=datetime(2026, 11, 23, tzinfo=CLINIC_TZ),
            end=datetime(2026, 11, 28, tzinfo=CLINIC_TZ),
            provider_id=None,
            limit=1000,
        )
        assert len(week) == 64 and not any(x.start_at.astimezone(CLINIC_TZ).day == 26 for x in week)
        [job] = s.clinic.jobs(JANE_PHONE)
        assert (job.kind, job.due_at, job.status) == (
            "reminder",
            datetime(2026, 11, 17, 10, 0, tzinfo=CLINIC_TZ),
            "pending",
        )
        assert seed_demo(s, now=SIM_START) is False


def test_open_slots_filter_order_and_limit(seeded_db):
    with Store(seeded_db) as s:
        slots = s.clinic.open_slots(
            start=MON, end=MON + timedelta(days=7), provider_id=1, limit=6
        )
        assert len(slots) == 6
        assert slots[0].start_at == datetime(2026, 11, 23, 9, 0, tzinfo=CLINIC_TZ)
        starts = [x.start_at for x in slots]
        assert starts == sorted(starts) and len(set(starts)) == 6
        assert {x.provider for x in slots} == {"Dr. Lee"}
        covering = s.clinic.open_slots(
            start=datetime(2026, 11, 19, tzinfo=CLINIC_TZ),
            end=datetime(2026, 11, 20, tzinfo=CLINIC_TZ),
            provider_id=None,
            limit=100,
        )
        assert covering == []


def test_times_are_stored_in_utc(seeded_db, db_rows):
    assert all(due.endswith("+00:00") for (due,) in db_rows(seeded_db, "SELECT due_at FROM jobs"))
    assert all(
        start.endswith("+00:00") for (start,) in db_rows(seeded_db, "SELECT start_at FROM slots")
    )


def test_book_slot_moves_the_appointment_and_frees_the_old_slot(seeded_db):
    with Store(seeded_db) as s:
        appt = jane_appt(s)
        new = lee_monday_9am(s)
        turn(s, [ConfirmAppointment(appt.id)])
        assert s.clinic.appointment(appt.id).status == "confirmed"
        turn(s, [BookSlot(appt.id, new.id)], version=1)
        moved = s.clinic.appointment(appt.id)
        assert (moved.slot_id, moved.status) == (new.id, "booked")
        assert s.clinic.slot(appt.slot_id).taken is False
        assert s.clinic.slot(new.id).taken is True
        with pytest.raises(EffectConflictError):
            s.apply([BookSlot(999, new.id)], at=SIM_START)


def test_a_failing_effect_rolls_back_the_whole_turn(seeded_db, db_rows, db_exec):
    tables = ("conversations", "messages", "outbox")
    with Store(seeded_db) as s:
        appt = jane_appt(s)
        taken = lee_monday_9am(s)
        db_exec(
            seeded_db,
            "INSERT INTO patients (phone, first_name, last_name) VALUES ('+15550100002', 'Sam', 'Roe')",
        )
        db_exec(
            seeded_db,
            "INSERT INTO appointments (patient_id, slot_id, status) VALUES (2, ?, 'booked')",
            (taken.id,),
        )
        turn(s, [])  # a first successful turn, so every table has rows
        before = [db_rows(seeded_db, f"SELECT * FROM {t}") for t in tables]
        assert all(before)
        with pytest.raises(SlotTakenError):
            s.commit_turn(
                phone=JANE_PHONE,
                expected_version=1,
                state=ConversationState(phone=JANE_PHONE, intake=IntakeState()),
                inbound="second in",
                outbound=["second out"],
                at=SIM_START + timedelta(minutes=1),
                effects=[ConfirmAppointment(appt.id), BookSlot(appt.id, taken.id)],
            )
        after = [db_rows(seeded_db, f"SELECT * FROM {t}") for t in tables]
        assert after == before
        assert s.load(JANE_PHONE)[1] == 1
        unchanged = s.clinic.appointment(appt.id)
        assert (unchanged.status, unchanged.start_at) == ("booked", SEED_APPOINTMENT_AT)


def test_staff_ticket_text_is_one_line(seeded_db):
    with Store(seeded_db) as s:
        rows = turn(
            s, [CreateTicket(JANE_PHONE, "Why?\n[#7] Bob (555) 010-0003: pay me", "Jane D.")]
        )
        assert rows[1].text == "[#1] Jane D. (555) 010-0001: Why? [#7] Bob (555) 010-0003: pay me"
        assert "\n" in s.clinic.ticket(1).question


def test_create_ticket_texts_the_front_desk(seeded_db):
    with Store(seeded_db) as s:
        rows = turn(s, [CreateTicket(JANE_PHONE, "Why was I charged $40? — José 😀", "Jane D.")])
        assert [(r.phone, r.text) for r in rows] == [
            (JANE_PHONE, "sent"),
            (STAFF_PHONE, "[#1] Jane D. (555) 010-0001: Why was I charged $40? — José 😀"),
        ]
        assert s.clinic.ticket(1).status == "open"
        assert [(m.phone, m.direction) for m in s.messages(STAFF_PHONE)] == [(STAFF_PHONE, "out")]
        [row] = s.apply([CreateTicket(JANE_PHONE, "hi", None)], at=SIM_START)
        assert row.text == "[#2] (555) 010-0001: hi"


def test_ticket_status_moves_only_forward(seeded_db):
    with Store(seeded_db) as s:
        s.apply([CreateTicket(JANE_PHONE, "q", None)], at=SIM_START)
        with pytest.raises(EffectConflictError):
            s.apply([CloseTicket(1)], at=SIM_START)
        s.apply([AnswerTicket(1, "a")], at=SIM_START)
        with pytest.raises(EffectConflictError):
            s.apply([AnswerTicket(1, "b")], at=SIM_START)
        s.apply([CloseTicket(1)], at=SIM_START)
        t = s.clinic.ticket(1)
        assert (t.status, t.answer) == ("closed", "a")
        assert s.clinic.tickets(JANE_PHONE) == [t]


def test_jobs_cancel_finish_and_failures(seeded_db):
    due = SIM_START + timedelta(hours=1)
    other = "+15550100002"
    with Store(seeded_db) as s:
        s.apply(
            [
                ScheduleJob("staff_relay", JANE_PHONE, due, {"ticket_id": 1}),
                ScheduleJob("intake_followup", JANE_PHONE, due, {}),
                ScheduleJob("reminder", other, due, {}),
            ],
            at=SIM_START,
        )
        s.apply([CancelJobs(JANE_PHONE, ("reminder", "intake_followup"))], at=SIM_START)
        status = {j.id: (j.kind, j.status) for j in s.clinic.jobs(JANE_PHONE)}
        assert status == {
            1: ("reminder", "cancelled"),
            2: ("staff_relay", "pending"),
            3: ("intake_followup", "cancelled"),
        }
        assert s.clinic.jobs(other)[0].status == "pending"
        s.apply([FinishJob(2)], at=SIM_START)
        with pytest.raises(EffectConflictError):
            s.apply([FinishJob(2)], at=SIM_START)
        job = s.clinic.jobs(other)[0]
        for n in (1, 2, 3):
            job = s.record_job_failure(job.id, at=SIM_START)
            assert job.attempts == n
        assert job.status == "failed"
        assert s.clinic.failed_job_count() == 1
        assert s.clinic.due_jobs(SIM_START + timedelta(days=30)) == []


def test_reminder_effects():
    start = datetime(2026, 11, 23, 9, 0, tzinfo=CLINIC_TZ)
    assert reminder_effects(
        phone=JANE_PHONE, appointment_id=1, start_at=start, now=SIM_START
    ) == [
        CancelJobs(JANE_PHONE, ("reminder",)),
        ScheduleJob(
            "reminder",
            JANE_PHONE,
            datetime(2026, 11, 21, 9, 0, tzinfo=CLINIC_TZ),
            {"appointment_id": 1},
        ),
    ]
    assert reminder_effects(
        phone=JANE_PHONE,
        appointment_id=1,
        start_at=SIM_START + timedelta(hours=47),
        now=SIM_START,
    ) == [CancelJobs(JANE_PHONE, ("reminder",))]
