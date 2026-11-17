"""Escalating a question to the front desk, and relaying its answer (spec §5.4, §5.9 #7)."""

import logging
from datetime import datetime

import pytest
from livekit.agents import APIError

from clinic_sms.agents import AllergiesMedsTask
from clinic_sms.agents.common import ESCALATED_OUTPUT
from clinic_sms.agents.reschedule import RescheduleAgent
from clinic_sms.channel import Outbound
from clinic_sms.clinic_db import STAFF_PHONE, Ticket
from clinic_sms.clock import CLINIC_TZ, SimClock
from clinic_sms.ctx import normalize_ctx
from clinic_sms.effects import CreateTicket
from clinic_sms.fmt import format_phone, mask_phone
from clinic_sms.jobs import RELAY_INSTRUCTIONS
from clinic_sms.runner import (
    FALLBACK_TEXT,
    STAFF_UNKNOWN_TICKET_TEXT,
    STAFF_USAGE_TEXT,
    parse_staff_reply,
)
from clinic_sms.seed import JANE_PHONE
from clinic_sms.testing import Reply, tool_names
from conftest import load
from test_followups import finish_seed_reminder
from test_jobs import World, world  # noqa: F401

TUE_11AM = datetime(2026, 11, 17, 11, 0, tzinfo=CLINIC_TZ)
QUESTION = "Why was I charged $40?"
ANSWER = "That was your copay."
ESCALATED_SMS = "I've sent that to our front desk. Meanwhile, any allergies or medications?"
RELAY = RELAY_INSTRUCTIONS.format(question=QUESTION, answer=ANSWER)
RELAYED_SMS = "Front desk: that was your copay. Any allergies or medications?"
STAFF_SMS = f"[#1] Jane D. (555) 010-0001: {QUESTION}"


@pytest.fixture
def escalation_llm(scripted_llm, intake_replies):
    intake_replies.update(
        {
            QUESTION: Reply(tools=[("escalate_to_staff", {"question": QUESTION})]),
            ESCALATED_OUTPUT: Reply(text=ESCALATED_SMS),
            RELAY: Reply(text=RELAYED_SMS),
        }
    )
    return scripted_llm(intake_replies)


async def at_allergies_meds(w: World, db) -> None:
    """Start Jane's intake and answer the first two steps.

    The seeded reminder is cancelled first, so that it cannot run in between.
    """
    finish_seed_reminder(w.store, w.clock.now())
    assert await w.runner.start_intake(JANE_PHONE)
    await w.runner.handle_inbound(JANE_PHONE, "Aetna, member W123456789")
    await w.runner.handle_inbound(JANE_PHONE, "Follow-up on my knee")
    assert load(db)[0].intake.progress.pending[0] == "allergies_meds"


async def escalate_and_relay(world, db, llm) -> World:
    """At allergies_meds: escalate, the front desk answers, a restart, the relay runs.

    Returns the world after the restart; its channel holds only the relay.
    """
    w = world(db, llm, clock=SimClock(TUE_11AM))
    await at_allergies_meds(w, db)
    await w.runner.handle_inbound(JANE_PHONE, QUESTION)
    before = len(w.ch.sent)
    assert await w.runner.handle_inbound(STAFF_PHONE, f"#1 {ANSWER}") == []
    assert len(w.ch.sent) == before  # nothing goes back to the front desk
    assert w.store.clinic.ticket(1).status == "answered"
    w = world(db, llm, clock=w.clock)  # restart
    assert await w.scheduler.run_due() == 1
    return w


async def test_escalation_mid_intake_texts_staff_and_keeps_the_step(
    seeded_db, escalation_llm, world
):
    w = world(seeded_db, escalation_llm, clock=SimClock(TUE_11AM))
    await at_allergies_meds(w, seeded_db)
    await w.runner.handle_inbound(JANE_PHONE, QUESTION)
    assert [(m.to, m.text) for m in w.ch.sent[-2:]] == [
        (JANE_PHONE, ESCALATED_SMS),
        (STAFF_PHONE, STAFF_SMS),
    ]
    assert w.store.clinic.ticket(1) == Ticket(1, JANE_PHONE, QUESTION, "open", None)
    assert load(seeded_db)[0].intake.progress.pending[0] == "allergies_meds"
    assert escalation_llm.calls[-2:] == [QUESTION, ESCALATED_OUTPUT]
    # the step offers the tool next to its own
    assert "escalate_to_staff" in tool_names(escalation_llm.tools[-2])
    assert "record_allergies_and_medications" in tool_names(escalation_llm.tools[-2])


async def test_staff_answer_is_relayed_mid_intake_then_intake_continues(  # spec §5.9 #7
    seeded_db, escalation_llm, world
):
    w = await escalate_and_relay(world, seeded_db, escalation_llm)
    assert escalation_llm.calls[-1] == RELAY
    assert w.ch.sent == [Outbound(JANE_PHONE, RELAYED_SMS)]
    assert w.store.clinic.ticket(1) == Ticket(1, JANE_PHONE, QUESTION, "closed", ANSWER)
    assert [(j.kind, j.status) for j in w.jobs() if j.kind == "staff_relay"] == [
        ("staff_relay", "done")
    ]
    # the step that was waiting sent the relay, and still waits for its answer
    assert load(seeded_db)[0].intake.progress.pending[0] == "allergies_meds"

    await w.runner.handle_inbound(JANE_PHONE, "Penicillin; ibuprofen")
    state = load(seeded_db)[0]
    assert (state.active, state.intake.done) == ("reception", True)
    assert state.intake.results["allergies_meds"] == {
        "allergies": ["penicillin"],
        "medications": ["ibuprofen"],
    }
    assert escalation_llm.calls.count(AllergiesMedsTask.OPENING) == 1
    # the restored step saw the relay it had sent
    answered = escalation_llm.calls.index("Penicillin; ibuprofen")
    ctx = normalize_ctx(escalation_llm.contexts[answered])
    assert ("message", "assistant", RELAYED_SMS) in ctx


async def test_bad_staff_replies_get_usage_or_unknown_ticket_text(  # Review Focus 4
    seeded_db, scripted_llm, world, db_rows
):
    llm = scripted_llm({})
    w = world(seeded_db, llm, clock=SimClock(TUE_11AM))
    finish_seed_reminder(w.store, TUE_11AM)
    w.store.apply([CreateTicket(JANE_PHONE, QUESTION, "Jane D.")], at=TUE_11AM)
    staff = w.runner.handle_inbound
    assert await staff(STAFF_PHONE, "hello") == [STAFF_USAGE_TEXT]
    assert await staff(STAFF_PHONE, "#99 x") == ["There is no open ticket #99."]
    assert await staff(STAFF_PHONE, "#1 a") == []
    assert await staff(STAFF_PHONE, "#1 b") == ["There is no open ticket #1."]
    huge = "9" * 30  # too large for an SQLite integer
    assert await staff(STAFF_PHONE, f"#{huge} x") == [
        STAFF_UNKNOWN_TICKET_TEXT.format(ticket_id=int(huge))
    ]
    relays = "SELECT kind, phone, payload FROM jobs WHERE kind = 'staff_relay'"
    assert db_rows(seeded_db, relays) == [("staff_relay", JANE_PHONE, '{"ticket_id": 1}')]
    assert [m for m in w.ch.sent if m.to == JANE_PHONE] == []
    assert w.store.messages(JANE_PHONE) == []
    assert w.store.clinic.ticket(1).answer == "a"
    assert llm.calls == []
    # every staff text is logged with its reply
    assert [(m.direction, m.text) for m in w.store.messages(STAFF_PHONE)][1:3] == [
        ("in", "hello"),
        ("out", STAFF_USAGE_TEXT),
    ]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("#12 Your copay was $25.", (12, "Your copay was $25.")),
        ("  #3   Two\nlines  \n", (3, "Two\nlines")),
        ("#4\nOn the next line", (4, "On the next line")),
        ("#1", None),
        ("#1   ", None),
        ("#1x yes", None),
        ("# 1 yes", None),
        ("1 yes", None),
        ("hello", None),
    ],
)
def test_parse_staff_reply(text, expected):
    assert parse_staff_reply(text) == expected


async def test_reception_can_escalate_too(seeded_db, scripted_llm, world):
    question = "How much do I still owe?"
    answer = "Nothing, you're all paid up."
    llm = scripted_llm(
        {
            question: Reply(tools=[("escalate_to_staff", {"question": question})]),
            ESCALATED_OUTPUT: Reply(text="I've asked our front desk; they'll text you back."),
            RELAY_INSTRUCTIONS.format(question=question, answer=answer): Reply(
                text="Our front desk says you owe nothing."
            ),
        }
    )
    w = world(seeded_db, llm, clock=SimClock(TUE_11AM))
    finish_seed_reminder(w.store, TUE_11AM)
    await w.runner.handle_inbound(JANE_PHONE, question)
    assert w.ch.sent == [
        Outbound(JANE_PHONE, "I've asked our front desk; they'll text you back."),
        Outbound(STAFF_PHONE, f"[#1] Jane D. (555) 010-0001: {question}"),
    ]
    assert load(seeded_db)[0].active == "reception"
    assert "escalate_to_staff" in tool_names(llm.tools[0])

    assert await w.runner.handle_inbound(STAFF_PHONE, f"#1 {answer}") == []
    w = world(seeded_db, llm, clock=w.clock)  # restart
    assert await w.scheduler.run_due() == 1
    assert w.ch.sent == [Outbound(JANE_PHONE, "Our front desk says you owe nothing.")]
    assert w.store.clinic.ticket(1).status == "closed"
    assert load(seeded_db)[0].active == "reception"


# a proactive turn must not hand off: the next agent's opening would be cut off
async def test_a_relay_that_hands_off_is_retried_not_half_committed(
    seeded_db, scripted_llm, world
):
    question = "How much do I still owe?"
    answer = "Nothing."
    relay = RELAY_INSTRUCTIONS.format(question=question, answer=answer)
    llm = scripted_llm(
        {
            question: Reply(tools=[("escalate_to_staff", {"question": question})]),
            ESCALATED_OUTPUT: Reply(text="I've asked our front desk."),
            relay: Reply(text="You owe nothing.", tools=[("transfer_to_reschedule", {})]),
            RescheduleAgent.OPENING: Reply(text="I have Mon 11/23 at 9 AM."),
        }
    )
    w = world(seeded_db, llm, clock=SimClock(TUE_11AM))
    finish_seed_reminder(w.store, TUE_11AM)
    await w.runner.handle_inbound(JANE_PHONE, question)
    await w.runner.handle_inbound(STAFF_PHONE, f"#1 {answer}")
    sent, messages = list(w.ch.sent), w.messages()
    assert await w.scheduler.run_due() == 1
    (job,) = [j for j in w.jobs() if j.kind == "staff_relay"]
    assert (job.status, job.attempts) == ("pending", 1)
    assert w.store.clinic.ticket(1).status == "answered"
    assert w.ch.sent == sent and w.messages() == messages
    assert load(seeded_db)[0].active == "reception"


async def test_logs_carry_no_question_or_answer(
    seeded_db, escalation_llm, scripted_llm, world, caplog
):
    escalate = Reply(tools=[("escalate_to_staff", {"question": QUESTION})])
    # the error's text stands in for patient words an error might echo
    failing = scripted_llm(
        {
            QUESTION: escalate,
            ESCALATED_OUTPUT: Reply(
                error=APIError(f"{JANE_PHONE} asked: {QUESTION}", retryable=False)
            ),
        }
    )
    handing_off = scripted_llm(
        {
            QUESTION: escalate,
            ESCALATED_OUTPUT: Reply(text="I've asked our front desk."),
            RELAY: Reply(text=RELAYED_SMS, tools=[("transfer_to_reschedule", {})]),
            RescheduleAgent.OPENING: Reply(text="I have Mon 11/23 at 9 AM."),
        }
    )
    with caplog.at_level(logging.DEBUG):
        # the two flows above: escalate mid-intake, the answer, the relay, the last answer
        w = await escalate_and_relay(world, seeded_db, escalation_llm)
        await w.runner.handle_inbound(STAFF_PHONE, f"hello {ANSWER}")  # a bad staff reply
        await w.runner.handle_inbound(JANE_PHONE, "Penicillin; ibuprofen")
        assert load(seeded_db)[0].intake.done
        # an escalation turn that fails: logged, answered with the fallback, no ticket
        w = world(seeded_db, failing, clock=w.clock)
        assert await w.runner.handle_inbound(JANE_PHONE, QUESTION) == [FALLBACK_TEXT]
        assert len(w.store.clinic.tickets(JANE_PHONE)) == 1
        # a relay that hands off: the job fails and is logged (proactive turns never hand off)
        w = world(seeded_db, handing_off, clock=w.clock)
        await w.runner.handle_inbound(JANE_PHONE, QUESTION)
        assert await w.runner.handle_inbound(STAFF_PHONE, f"#2 {ANSWER}") == []
        assert await w.scheduler.run_due() == 1
        (job,) = [j for j in w.jobs() if j.kind == "staff_relay" and j.status == "pending"]
        assert job.attempts == 1

    secrets = [
        QUESTION,
        ANSWER,
        "$40",
        "copay",
        JANE_PHONE,
        STAFF_PHONE,
        format_phone(JANE_PHONE),
        format_phone(STAFF_PHONE),
    ]
    formatter = logging.Formatter("%(name)s %(levelname)s %(message)s")
    examined = [
        r
        for r in caplog.records
        # our loggers at any level; the SDK's at WARNING and above, what the CLI lets through
        if r.name.startswith("clinic_sms") or r.levelno >= logging.WARNING
    ]
    for r in examined:
        line = formatter.format(r)  # also fills exc_text from exc_info, if any
        for text in (r.getMessage(), r.exc_text or "", line):
            for secret in secrets:
                assert secret not in text, (secret, line)
    ours = [
        (r.name, r.levelno, r.getMessage()) for r in examined if r.name.startswith("clinic_sms")
    ]
    masked = mask_phone(JANE_PHONE)
    assert any(
        name == "clinic_sms.runner"
        and level == logging.ERROR
        and message.startswith(
            f"inbound message for {masked} failed: livekit.agents._exceptions.APIError"
        )
        for name, level, message in ours
    ), ours
    assert any(
        name == "clinic_sms.jobs"
        and level == logging.ERROR
        and message.startswith(
            f"job {job.id} (staff_relay) for {masked} failed: "
            "clinic_sms.runner.ProactiveHandoffError"
        )
        for name, level, message in ours
    ), ours
