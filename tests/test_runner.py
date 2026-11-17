import asyncio
import logging
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from livekit.agents import AgentSession, APIError, RunResult

from clinic_sms.agents import (
    AllergiesMedsTask,
    InsuranceTask,
    IntakeAgent,
    IntakeStep,
    VisitReasonTask,
    registry,
)
from clinic_sms.channel import Outbound, RecordingChannel
from clinic_sms.clinic_db import STAFF_PHONE
from clinic_sms.ctx import normalize_ctx
from clinic_sms.fmt import mask_phone
from clinic_sms.jobs import FOLLOWUP_INSTRUCTIONS
from clinic_sms.proactive import Proactive
from clinic_sms.runner import FALLBACK_TEXT, NO_CONVERSATION_TEXT, FrontDeskIntakeError
from clinic_sms.state import ConversationState, GroupProgress, IntakeState
from clinic_sms.store import Store
from clinic_sms.testing import Reply, ScriptedLLM
from clinic_sms.turn import Turn
from conftest import P, T, commit, load


def sql(db: Path, statement: str) -> None:
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(statement)


def stored_row(db: Path) -> tuple[int, str]:
    """(version, state_json) as stored, without parsing it."""
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute("SELECT version, state_json FROM conversations").fetchone()


async def at_visit_reason(make_runner, db: Path, llm: ScriptedLLM, ch: RecordingChannel) -> None:
    await make_runner(db, llm, ch).start_intake(P)
    await make_runner(db, llm, ch).handle_inbound(P, "Aetna, member W123456789")


async def test_start_intake_sends_first_question(tmp_path, intake_llm, make_runner):
    db, ch = tmp_path / "db", RecordingChannel()
    first = "Hi Jane! What's your insurance carrier and member ID?"
    assert await make_runner(db, intake_llm, ch).start_intake(P) == [first]
    assert ch.sent == [Outbound(to=P, text=first)]
    state, version = load(db)
    assert state.intake.progress.pending[0] == "insurance" and version == 1
    # already started: nothing happens
    assert await make_runner(db, intake_llm, ch).start_intake(P) == []
    assert intake_llm.calls.count(InsuranceTask.OPENING) == 1 and len(ch.sent) == 1


async def test_restart_between_steps_does_not_reask(tmp_path, intake_llm, make_runner):
    db, ch = tmp_path / "db", RecordingChannel()
    await make_runner(db, intake_llm, ch).start_intake(P)
    assert await make_runner(db, intake_llm, ch).handle_inbound(
        P, "Aetna, member W123456789"
    ) == ["Thanks! What brings you in?"]
    assert await make_runner(db, intake_llm, ch).handle_inbound(P, "Follow-up on my knee") == [
        "Any allergies or current medications?"
    ]
    assert intake_llm.calls.count(InsuranceTask.OPENING) == 1
    assert set(load(db)[0].intake.progress.results) == {"insurance", "visit_reason"}


async def test_restart_mid_step(tmp_path, intake_llm, make_runner):
    db, ch = tmp_path / "db", RecordingChannel()
    await make_runner(db, intake_llm, ch).start_intake(P)
    assert await make_runner(db, intake_llm, ch).handle_inbound(P, "Aetna") == [
        "Got it. What's your member ID?"
    ]
    # restored messages are not sent again (Spike 1 S4)
    assert await make_runner(db, intake_llm, ch).handle_inbound(P, "W123456789") == [
        "Thanks! What brings you in?"
    ]
    ctx = normalize_ctx(intake_llm.contexts[intake_llm.calls.index("W123456789")])
    assert ("message", "user", "Aetna") in ctx
    assert ("message", "assistant", "Got it. What's your member ID?") in ctx
    assert load(db)[0].intake.progress.results["insurance"]["member_id"] == "W123456789"


async def test_regression_after_restart(tmp_path, intake_llm, make_runner):
    db, ch = tmp_path / "db", RecordingChannel()
    await at_visit_reason(make_runner, db, intake_llm, ch)
    await make_runner(db, intake_llm, ch).handle_inbound(P, "Actually my member ID is W999")
    assert ch.sent[-1].text == "Hi Jane! What's your insurance carrier and member ID?"
    await make_runner(db, intake_llm, ch).handle_inbound(P, "Aetna, W999")
    assert ch.sent[-1].text == "Thanks! What brings you in?"
    assert load(db)[0].intake.progress.results["insurance"]["member_id"] == "W999"


async def test_completing_a_step_sends_only_the_next_question(
    tmp_path, intake_replies, scripted_llm, make_runner
):
    # A tool that returns a value makes the SDK call the LLM again with that output
    # (voice/generation.py:1241), and Claude answers that call with one more SMS. The
    # record_* tools return None: one LLM call for the answer, one for the next question.
    llm = scripted_llm(intake_replies, default=Reply(text="Saved, thanks!"))
    db, ch = tmp_path / "db", RecordingChannel()
    await make_runner(db, llm, ch).start_intake(P)
    turns = [
        ("Aetna, member W123456789", VisitReasonTask.OPENING, "Thanks! What brings you in?"),
        ("Follow-up on my knee", AllergiesMedsTask.OPENING, "Any allergies or current medications?"),
        ("Penicillin; ibuprofen", IntakeAgent.COMPLETION, "You're all set. See you Thursday!"),
    ]
    for answer, next_instructions, reply in turns:
        start = len(llm.calls)
        assert await make_runner(db, llm, ch).handle_inbound(P, answer) == [reply]
        assert llm.calls[start:] == [answer, next_instructions]
    assert load(db)[0].intake.done


async def test_full_intake_with_summary_sends_no_summary_sms(
    tmp_path, intake_replies, scripted_llm, make_runner
):
    # As in production (summarize_intake defaults to True): the group's summary call
    # runs in the last turn, and its text must not become an SMS.
    summary = "Patient: Aetna W123456789; knee follow-up; penicillin; ibuprofen."
    llm = scripted_llm(intake_replies, default=Reply(text=summary))
    db, ch = tmp_path / "db", RecordingChannel()
    await make_runner(db, llm, ch, summarize_intake=True).start_intake(P)
    for text in ("Aetna", "W123456789", "Follow-up on my knee", "Penicillin; ibuprofen"):
        await make_runner(db, llm, ch, summarize_intake=True).handle_inbound(P, text)
    assert [m.text for m in ch.sent] == [
        "Hi Jane! What's your insurance carrier and member ID?",
        "Got it. What's your member ID?",
        "Thanks! What brings you in?",
        "Any allergies or current medications?",
        "You're all set. See you Thursday!",
    ]
    # the summary call ran, once, in the last turn, just before the completion reply
    (summary_call,) = [k for k in llm.calls if k not in intake_replies]
    assert summary_call.startswith("Conversation to summarize")
    assert llm.calls[-2:] == [summary_call, IntakeAgent.COMPLETION]
    state, _ = load(db)
    assert state.intake.done
    assert set(state.intake.results) == {"insurance", "visit_reason", "allergies_meds"}
    # the summary is stored in the root context, not sent
    with Store(db) as store:
        assert summary not in [m.text for m in store.messages(P)]


async def test_failure_keeps_state_and_sends_fallback(
    tmp_path, intake_replies, scripted_llm, make_runner
):
    intake_replies["boom"] = Reply(error=APIError("down", retryable=False))
    llm = scripted_llm(intake_replies)
    db, ch = tmp_path / "db", RecordingChannel()
    await at_visit_reason(make_runner, db, llm, ch)
    before = load(db)
    assert await make_runner(db, llm, ch).handle_inbound(P, "boom") == [FALLBACK_TEXT]
    assert load(db) == before
    assert ch.sent[-1] == Outbound(to=P, text=FALLBACK_TEXT)
    with Store(db) as store:
        assert [(m.direction, m.text) for m in store.messages(P)][-2:] == [
            ("in", "boom"),
            ("out", FALLBACK_TEXT),
        ]


async def test_empty_reply_is_a_failure(tmp_path, intake_replies, scripted_llm, make_runner):
    intake_replies["..."] = Reply(text="")
    llm = scripted_llm(intake_replies)
    db, ch = tmp_path / "db", RecordingChannel()
    await make_runner(db, llm, ch).start_intake(P)
    before = load(db)
    assert await make_runner(db, llm, ch).handle_inbound(P, "...") == [FALLBACK_TEXT]
    assert load(db) == before


async def test_commit_failure_keeps_state_and_sends_fallback(tmp_path, intake_llm, make_runner):
    # The agent has run, but the commit fails after the state UPDATE (here: on the outbox
    # INSERT). commit_turn must roll back as a whole; the patient gets the fallback and
    # the next message re-runs from the old state.
    db, ch = tmp_path / "db", RecordingChannel()
    await at_visit_reason(make_runner, db, intake_llm, ch)
    before = load(db)
    sql(
        db,
        "CREATE TRIGGER outbox_down BEFORE INSERT ON outbox"
        " WHEN NEW.text = 'Any allergies or current medications?'"
        " BEGIN SELECT RAISE(ABORT, 'outbox down'); END",
    )
    assert await make_runner(db, intake_llm, ch).handle_inbound(P, "Follow-up on my knee") == [
        FALLBACK_TEXT
    ]
    assert load(db) == before
    sql(db, "DROP TRIGGER outbox_down")
    assert await make_runner(db, intake_llm, ch).handle_inbound(P, "Follow-up on my knee") == [
        "Any allergies or current medications?"
    ]


async def test_group_failure_after_last_step_keeps_state(
    tmp_path, intake_replies, scripted_llm, make_runner, caplog
):
    # The last step records its answer and the model replies, then the group's summary
    # call fails: the group ends without finishing the intake. Nothing is committed, so
    # the answered step is not stored as still pending.
    record = intake_replies["Penicillin; ibuprofen"]
    intake_replies["Penicillin; ibuprofen"] = Reply(text="Thanks, noted.", tools=record.tools)
    summary_down = Reply(error=APIError("summary down", retryable=False))
    llm = scripted_llm(intake_replies, default=summary_down)
    db, ch = tmp_path / "db", RecordingChannel()
    progress = GroupProgress(
        pending=["allergies_meds"],
        results={
            "insurance": {"carrier": "Aetna", "member_id": "W123456789"},
            "visit_reason": {"reason": "Follow-up on my knee"},
        },
        visited=["insurance", "visit_reason", "allergies_meds"],
    )
    commit(db, ConversationState(phone=P, intake=IntakeState(progress=progress)), [])
    before = load(db)
    r = make_runner(db, llm, ch, summarize_intake=True)
    assert await r.handle_inbound(P, "Penicillin; ibuprofen") == [FALLBACK_TEXT]
    assert load(db) == before
    # the reply was produced and the unscripted summary call came after it
    assert llm.calls[-2] == "Penicillin; ibuprofen"
    assert llm.calls[-1].startswith("Conversation to summarize")
    # the cause is logged by IntakeAgent; the SDK gets no exception from on_enter
    (failure,) = [r for r in caplog.records if r.name == "clinic_sms.agents.intake"]
    # type name and traceback only: no exc_info, and not the text "summary down", which
    # stands for an error that echoes the patient's words
    assert failure.levelno == logging.ERROR and failure.exc_info is None
    message = failure.getMessage()
    assert message.startswith(
        f"the intake group failed for {mask_phone(P)}: livekit.agents._exceptions.APIError\n"
    )
    assert "summary down" not in message
    assert not [r for r in caplog.records if r.getMessage().startswith("Error in _traceable")]


async def test_pending_outbox_delivered_once_after_crash(tmp_path, intake_llm, make_runner):
    db, ch = tmp_path / "db", RecordingChannel()
    commit(db, ConversationState(phone=P), ["hello"])  # crash before delivery
    r = make_runner(db, intake_llm, ch)
    assert await r.deliver_pending() == 1 and [m.text for m in ch.sent] == ["hello"]
    assert await r.deliver_pending() == 0
    assert await make_runner(db, intake_llm, ch).deliver_pending() == 0


async def test_unknown_step_in_saved_state_falls_back_fast(tmp_path, intake_llm, make_runner):
    db, ch = tmp_path / "db", RecordingChannel()
    progress = GroupProgress(pending=["zzz"], results={}, visited=[])
    commit(db, ConversationState(phone=P, intake=IntakeState(progress=progress)), [])
    before = load(db)
    r = make_runner(db, intake_llm, ch)
    # well under the 2 s ready timeout: the bad state is rejected before any wait
    assert await asyncio.wait_for(r.handle_inbound(P, "hello"), 0.5) == [FALLBACK_TEXT]
    assert load(db) == before
    assert intake_llm.calls == []


# stored by an older version, say; both hold the patient's earlier words
UNPARSABLE_STATES = [
    '{"phone": "+15550100001", "intake": {"done": "My knee still hurts"}}',
    '{"phone": "+15550100001", "intake": {"results": {"visit_reason": {"reason": "My knee still',
]


@pytest.mark.parametrize("state_json", UNPARSABLE_STATES, ids=["schema", "truncated"])
async def test_stored_state_that_does_not_parse_is_a_failed_turn(
    tmp_path, intake_llm, make_runner, caplog, state_json
):
    db, ch = tmp_path / "db", RecordingChannel()
    await make_runner(db, intake_llm, ch).start_intake(P)
    sql(db, f"UPDATE conversations SET state_json = '{state_json}'")
    before, n_calls, n_sent = stored_row(db), len(intake_llm.calls), len(ch.sent)
    with caplog.at_level(logging.DEBUG):
        # an inbound message is a failed turn: the fallback is sent, and only the
        # message log changes (the inbound text and the fallback)
        assert await make_runner(db, intake_llm, ch).handle_inbound(P, "Aetna") == [
            FALLBACK_TEXT
        ]
        # starting the intake: only an error log record, nothing stored or sent
        assert await make_runner(db, intake_llm, ch).start_intake(P) == []
    assert stored_row(db) == before
    assert len(intake_llm.calls) == n_calls
    assert [m.text for m in ch.sent[n_sent:]] == [FALLBACK_TEXT]
    with Store(db) as store:
        assert [(m.direction, m.text) for m in store.messages(P)][-2:] == [
            ("in", "Aetna"),
            ("out", FALLBACK_TEXT),
        ]
        assert len(store.messages(P)) == 3 and store.pending_outbox() == []
    ours = [r for r in caplog.records if r.name == "clinic_sms.runner"]
    assert [r.levelno for r in ours] == [logging.ERROR] * 2
    formatter = logging.Formatter("%(name)s %(message)s")
    for r in caplog.records:
        line = formatter.format(r)
        assert P not in line and "knee" not in line and "Aetna" not in line
    assert all(mask_phone(P) in r.getMessage() for r in ours)


async def test_blank_inbound_is_ignored(tmp_path, intake_llm, make_runner):
    db, ch = tmp_path / "db", RecordingChannel()
    assert await make_runner(db, intake_llm, ch).handle_inbound(P, "   ") == []
    with Store(db) as store:
        assert intake_llm.calls == [] and store.messages(P) == []
    assert ch.sent == []


async def test_same_phone_messages_are_serialized(
    tmp_path, intake_replies, scripted_llm, make_runner
):
    intake_replies["Follow-up on my knee"].delay = 0.2
    llm = scripted_llm(intake_replies)
    db, ch = tmp_path / "db", RecordingChannel()
    await at_visit_reason(make_runner, db, llm, ch)
    _, before_version = load(db)
    r = make_runner(db, llm, ch)
    await asyncio.gather(
        r.handle_inbound(P, "Follow-up on my knee"), r.handle_inbound(P, "Penicillin; ibuprofen")
    )
    state, version = load(db)
    assert state.intake.done and version == before_version + 2


async def test_unknown_phone_gets_fixed_reply(tmp_path, intake_llm, make_runner):
    db, ch = tmp_path / "db", RecordingChannel()
    other = "+15550100009"
    assert await make_runner(db, intake_llm, ch).handle_inbound(other, "hi") == [
        NO_CONVERSATION_TEXT
    ]
    assert intake_llm.calls == []
    assert ch.sent == [Outbound(to=other, text=NO_CONVERSATION_TEXT)]


async def test_chained_cold_starts_keep_each_patient_message_once(
    tmp_path, intake_replies, scripted_llm, make_runner
):
    # five sessions through five runner instances: start, mid-step, restore mid-step,
    # restore between steps (twice); non-ASCII in a patient message and a reply
    patient_1, reply_1 = "Aetna — José 😀", "Got it, José — what's your member ID? 😀"
    intake_replies[patient_1] = Reply(text=reply_1)
    llm = scripted_llm(intake_replies)
    db, ch = tmp_path / "db", RecordingChannel()
    patient = [patient_1, "W123456789", "Follow-up on my knee", "Penicillin; ibuprofen"]
    await make_runner(db, llm, ch).start_intake(P)
    for text in patient:
        await make_runner(db, llm, ch).handle_inbound(P, text)
    assert [m.text for m in ch.sent] == [
        "Hi Jane! What's your insurance carrier and member ID?",
        reply_1,
        "Thanks! What brings you in?",
        "Any allergies or current medications?",
        "You're all set. See you Thursday!",
    ]
    # the last step's LLM call, in the fifth session, sees every patient message once
    ctx = normalize_ctx(llm.contexts[llm.calls.index("Penicillin; ibuprofen")])
    for text in patient:
        assert ctx.count(("message", "user", text)) == 1, text
    assert ctx.count(("message", "assistant", reply_1)) == 1
    state, _ = load(db)
    assert state.intake.done
    with Store(db) as store:
        assert [m.text for m in store.messages(P) if m.direction == "in"] == patient


async def test_normal_runs_log_no_errors(tmp_path, intake_llm, make_runner, caplog):
    # every message but the last ends by closing the session while a step waits
    db, ch = tmp_path / "db", RecordingChannel()
    with caplog.at_level(logging.DEBUG):
        await make_runner(db, intake_llm, ch).start_intake(P)
        for text in ("Aetna", "W123456789", "Follow-up on my knee", "Penicillin; ibuprofen"):
            await make_runner(db, intake_llm, ch).handle_inbound(P, text)
        # wait for anything the closed sessions left running, so late records are seen
        others = asyncio.all_tasks() - {asyncio.current_task()}
        if others:
            await asyncio.wait(others, timeout=2)
    assert FALLBACK_TEXT not in [m.text for m in ch.sent]
    assert any(r.name.startswith("livekit") for r in caplog.records)  # SDK records are seen
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert load(db)[0].intake.done
    assert AllergiesMedsTask.OPENING in intake_llm.calls


async def test_logs_carry_no_phone_or_patient_text(
    tmp_path, intake_replies, scripted_llm, make_runner, caplog
):
    intake_replies["boom"] = Reply(error=APIError("down", retryable=False))
    llm = scripted_llm(intake_replies)
    ch = RecordingChannel()
    # stored state from an old schema holds the patient's earlier words: a result that
    # no longer parses, and a context item that no longer parses
    old_result, old_ctx_text = "My knee still hurts", "My card says Aetna"
    stale_result = GroupProgress(
        pending=["allergies_meds"],
        results={
            "insurance": {"carrier": "Aetna", "member_id": "W123456789"},
            "visit_reason": {"text": old_result},
        },
        visited=["insurance", "visit_reason", "allergies_meds"],
    )
    stale_ctx = IntakeState(
        progress=GroupProgress(pending=["insurance"], results={}, visited=["insurance"]),
        step_ctx={"items": [{"type": "message", "role": "user", "content": old_ctx_text}]},
    )
    stale_state = ConversationState(phone=P, intake=IntakeState(progress=stale_result))
    commit(tmp_path / "result", stale_state, [])
    commit(tmp_path / "ctx", ConversationState(phone=P, intake=stale_ctx), [])
    patient = ["Aetna, member W123456789", "boom", "hello again", "W123456789"]
    with caplog.at_level(logging.DEBUG):
        await make_runner(tmp_path / "db", llm, ch).start_intake(P)
        for text in patient[:2]:
            await make_runner(tmp_path / "db", llm, ch).handle_inbound(P, text)
        await make_runner(tmp_path / "result", llm, ch).handle_inbound(P, patient[2])
        await make_runner(tmp_path / "ctx", llm, ch).handle_inbound(P, patient[3])
    # the three failure paths ran and were logged
    assert [m.text for m in ch.sent[-3:]] == [FALLBACK_TEXT] * 3
    formatter = logging.Formatter("%(name)s %(message)s")
    # our records at any level (message and traceback); everyone's at WARNING and above.
    # The SDK's DEBUG records carry message text only in `extra["lk.pii.text"]`.
    lines = [
        formatter.format(r)
        for r in caplog.records
        if r.name.startswith("clinic_sms") or r.levelno >= logging.WARNING
    ]
    assert sum(mask_phone(P) in line for line in lines) == 3
    for line in lines:
        assert P not in line
        for text in [*patient, old_result, old_ctx_text]:
            assert text not in line


async def test_close_error_after_commit_keeps_the_turn(
    tmp_path, intake_llm, make_runner, monkeypatch, caplog
):
    # aclose() raising after a successful commit must not turn the turn into a failure
    real_aclose = AgentSession.aclose
    close_error = "close failed"  # not on the raise line: logged tracebacks show source lines

    async def aclose_then_fail(self: AgentSession) -> None:
        await real_aclose(self)
        raise RuntimeError(close_error)

    monkeypatch.setattr(AgentSession, "aclose", aclose_then_fail)
    db, ch = tmp_path / "db", RecordingChannel()
    first = "Hi Jane! What's your insurance carrier and member ID?"
    second = "Thanks! What brings you in?"
    assert await make_runner(db, intake_llm, ch).start_intake(P) == [first]
    assert await make_runner(db, intake_llm, ch).handle_inbound(
        P, "Aetna, member W123456789"
    ) == [second]
    assert [m.text for m in ch.sent] == [first, second]  # delivered, in order, no fallback
    state, version = load(db)
    assert version == 2 and set(state.intake.progress.results) == {"insurance"}
    with Store(db) as store:
        assert store.pending_outbox() == []
    closes = [r for r in caplog.records if r.getMessage().startswith("closing the session")]
    expected = f"closing the session for {mask_phone(P)} failed"
    assert len(closes) == 2
    for r in closes:  # the uniform failure record: type name and traceback, no message text
        assert r.getMessage().startswith(expected + ": ")
        rest = r.getMessage().removeprefix(expected + ": ")
        assert "RuntimeError" in rest and close_error not in rest


async def test_ready_timeout_falls_back_fast(tmp_path, intake_llm, make_runner, monkeypatch):
    db, ch = tmp_path / "db", RecordingChannel()
    await at_visit_reason(make_runner, db, intake_llm, ch)
    before, n_calls = load(db), len(intake_llm.calls)

    async def never_ready(self: IntakeStep) -> None:
        pass  # takes over without setting the ReadySignal

    monkeypatch.setattr(IntakeStep, "on_enter", never_ready)
    r = make_runner(db, intake_llm, ch, ready_timeout=0.3)
    assert await asyncio.wait_for(r.handle_inbound(P, "Follow-up on my knee"), 2) == [
        FALLBACK_TEXT
    ]
    assert load(db) == before
    assert len(intake_llm.calls) == n_calls  # the message never reached the LLM


async def test_run_timeout_falls_back(
    tmp_path, intake_replies, scripted_llm, make_runner, monkeypatch
):
    intake_replies["Follow-up on my knee"].delay = 1
    llm = scripted_llm(intake_replies)
    db, ch = tmp_path / "db", RecordingChannel()
    await at_visit_reason(make_runner, db, llm, ch)
    before = load(db)
    monkeypatch.setattr("clinic_sms.runner.RUN_TIMEOUT_S", 0.2)
    r = make_runner(db, llm, ch)
    assert await asyncio.wait_for(r.handle_inbound(P, "Follow-up on my knee"), 1) == [
        FALLBACK_TEXT
    ]
    assert load(db) == before


async def test_stale_version_falls_back_and_keeps_the_other_write(
    tmp_path, intake_replies, scripted_llm, make_runner
):
    intake_replies["Follow-up on my knee"].delay = 0.3
    llm = scripted_llm(intake_replies)
    db, ch = tmp_path / "db", RecordingChannel()
    await at_visit_reason(make_runner, db, llm, ch)
    state, version = load(db)
    turn = asyncio.create_task(make_runner(db, llm, ch).handle_inbound(P, "Follow-up on my knee"))
    async with asyncio.timeout(2):  # the turn has loaded the state and waits on the LLM
        while "Follow-up on my knee" not in llm.calls:
            await asyncio.sleep(0.01)
    other = state.model_copy(update={"intake_followups_sent": 1})
    with Store(db) as store:
        store.commit_turn(
            phone=P, expected_version=version, state=other, inbound=None, outbound=[], at=T
        )
    assert await asyncio.wait_for(turn, 5) == [FALLBACK_TEXT]
    assert load(db) == (other, version + 1)


async def test_failure_logs_carry_no_exception_text(
    tmp_path, intake_replies, scripted_llm, make_runner, caplog
):
    # Review Focus 5: an exception whose message echoes the patient's words
    intake_replies["boom"] = Reply(error=APIError("Jane says her knee hurts", retryable=False))
    llm = scripted_llm(intake_replies)
    db, ch = tmp_path / "db", RecordingChannel()
    await at_visit_reason(make_runner, db, llm, ch)
    with caplog.at_level(logging.DEBUG):
        assert await make_runner(db, llm, ch).handle_inbound(P, "boom") == [FALLBACK_TEXT]
    formatter = logging.Formatter("%(name)s %(message)s")  # also formats exc_info
    ours = [r for r in caplog.records if r.name == "clinic_sms.runner"]
    assert [r.levelno for r in ours] == [logging.ERROR]
    line = formatter.format(ours[0])
    assert "APIError" in line and mask_phone(P) in line
    # everyone's records at WARNING and above, ours at any level
    for r in caplog.records:
        if r.levelno >= logging.WARNING or r.name.startswith("clinic_sms"):
            line = formatter.format(r)
            assert "knee hurts" not in line and P not in line, line


async def test_fallback_is_sent_even_if_recording_it_fails(
    tmp_path, intake_replies, scripted_llm, make_runner, monkeypatch, caplog
):
    intake_replies["boom"] = Reply(error=APIError("down", retryable=False))
    llm = scripted_llm(intake_replies)
    db, ch = tmp_path / "db", RecordingChannel()
    await at_visit_reason(make_runner, db, llm, ch)
    before = load(db)

    def log_only_fails(self: Store, **kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Store, "log_only", log_only_fails)
    assert await make_runner(db, llm, ch).handle_inbound(P, "boom") == [FALLBACK_TEXT]
    assert ch.sent[-1] == Outbound(to=P, text=FALLBACK_TEXT)
    assert load(db) == before
    with Store(db) as store:  # sent directly: not logged, not queued
        assert store.pending_outbox() == []
        assert FALLBACK_TEXT not in [m.text for m in store.messages(P)]
    ours = [r.getMessage() for r in caplog.records if r.name == "clinic_sms.runner"]
    assert len(ours) == 2 and "OperationalError" in ours[1] and mask_phone(P) in ours[1]


class FlakyChannel(RecordingChannel):
    """Fails the first `failures` sends, then records like RecordingChannel."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self._failures = failures

    async def send(self, msg: Outbound) -> None:
        if self._failures > 0:
            self._failures -= 1
            raise ConnectionError("gateway down")
        await super().send(msg)


@pytest.mark.parametrize("failing", ["recording", "delivery"])
async def test_the_fallback_reaches_the_patient_once(
    failing, tmp_path, intake_replies, scripted_llm, make_runner, monkeypatch, caplog
):
    # The fallback goes straight to the channel only when it could not be recorded. Once
    # recorded, it is in the outbox: if delivering it fails, it waits there for
    # deliver_pending, and is not also sent directly (that would be a second apology).
    intake_replies["boom"] = Reply(error=APIError("down", retryable=False))
    llm = scripted_llm(intake_replies)
    db = tmp_path / "db"
    await at_visit_reason(make_runner, db, llm, RecordingChannel())
    ch = FlakyChannel(failures=1 if failing == "delivery" else 0)
    if failing == "recording":

        def log_only_fails(self: Store, **kwargs: object) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(Store, "log_only", log_only_fails)
    sent = await make_runner(db, llm, ch).handle_inbound(P, "boom")
    ours = [r.getMessage() for r in caplog.records if r.name == "clinic_sms.runner"]
    assert len(ours) == 2 and mask_phone(P) in ours[1]
    if failing == "recording":
        assert sent == [FALLBACK_TEXT] and ch.sent == [Outbound(to=P, text=FALLBACK_TEXT)]
        assert ours[1].startswith(f"recording the fallback for {mask_phone(P)} failed: ")
        assert "OperationalError" in ours[1]
    else:
        assert sent == [] and ch.sent == []
        assert ours[1].startswith(f"delivering the fallback for {mask_phone(P)} failed: ")
        assert "ConnectionError" in ours[1]
    monkeypatch.undo()  # recording works again
    # the next startup sends what is left in the outbox
    await make_runner(db, llm, ch).deliver_pending()
    assert ch.sent == [Outbound(to=P, text=FALLBACK_TEXT)]  # once, either way
    with Store(db) as store:
        assert store.pending_outbox() == []


async def test_close_error_after_a_failed_turn_still_sends_the_fallback(
    tmp_path, intake_replies, scripted_llm, make_runner, monkeypatch, caplog
):
    real_aclose = AgentSession.aclose

    async def aclose_then_fail(self: AgentSession) -> None:
        await real_aclose(self)
        raise RuntimeError("close failed")

    intake_replies["boom"] = Reply(error=APIError("down", retryable=False))
    llm = scripted_llm(intake_replies)
    db, ch = tmp_path / "db", RecordingChannel()
    await at_visit_reason(make_runner, db, llm, ch)
    before = load(db)
    monkeypatch.setattr(AgentSession, "aclose", aclose_then_fail)
    assert await make_runner(db, llm, ch).handle_inbound(P, "boom") == [FALLBACK_TEXT]
    assert ch.sent[-1] == Outbound(to=P, text=FALLBACK_TEXT)
    assert load(db) == before
    with Store(db) as store:
        assert [(m.direction, m.text) for m in store.messages(P)][-2:] == [
            ("in", "boom"),
            ("out", FALLBACK_TEXT),
        ]
    # the turn is logged with its own error, not the close error
    (failed,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("inbound")]
    assert "RuntimeError" not in failed


async def test_delivery_failure_after_commit_keeps_the_turn(
    tmp_path, intake_llm, make_runner, caplog
):
    class DownChannel(RecordingChannel):
        async def send(self, msg: Outbound) -> None:
            raise ConnectionError("gateway down")

    db = tmp_path / "db"
    await make_runner(db, intake_llm, RecordingChannel()).start_intake(P)
    r = make_runner(db, intake_llm, DownChannel())
    # no exception and no fallback: the turn stands, its reply waits in the outbox
    assert await r.handle_inbound(P, "Aetna, member W123456789") == []
    state, version = load(db)
    assert version == 2 and set(state.intake.progress.results) == {"insurance"}
    ch = RecordingChannel()
    assert await make_runner(db, intake_llm, ch).deliver_pending() == 1
    assert [m.text for m in ch.sent] == ["Thanks! What brings you in?"]
    ours = [r.getMessage() for r in caplog.records if r.name == "clinic_sms.runner"]
    assert len(ours) == 1 and "ConnectionError" in ours[0] and mask_phone(P) in ours[0]


async def test_turn_userdata_and_active_agent(tmp_path, intake_llm, make_runner, monkeypatch):
    turns: list[Turn] = []
    real_snapshot = registry.conversation_snapshot

    def recording_snapshot(state: ConversationState, turn: Turn) -> ConversationState:
        turns.append(turn)
        return real_snapshot(state, turn)

    sessions: list[AgentSession] = []
    real_start = AgentSession.start

    async def recording_start(self: AgentSession, *args, **kwargs):
        sessions.append(self)
        return await real_start(self, *args, **kwargs)

    monkeypatch.setattr(registry, "conversation_snapshot", recording_snapshot)
    monkeypatch.setattr(AgentSession, "start", recording_start)
    db, ch = tmp_path / "db", RecordingChannel()
    await at_visit_reason(make_runner, db, intake_llm, ch)
    assert len(turns) == len(sessions) == 2
    for turn, session in zip(turns, sessions, strict=True):
        assert turn.phone == P and isinstance(turn.active, IntakeAgent)
        assert session.userdata is turn
    # one Turn, and one one-shot ReadySignal, per cold-started session
    assert turns[0] is not turns[1] and turns[0].ready is not turns[1].ready


async def test_run_listener_sees_each_inbound_run(
    tmp_path, intake_replies, scripted_llm, make_runner
):
    nudge = "Still there? What's your insurance carrier and member ID?"
    llm = scripted_llm({**intake_replies, FOLLOWUP_INSTRUCTIONS: Reply(text=nudge)})
    db, ch = tmp_path / "db", RecordingChannel()
    seen: list[tuple[str, RunResult]] = []

    def runner():
        return make_runner(db, llm, ch, run_listener=lambda phone, run: seen.append((phone, run)))

    # the intake's opening and a proactive message are not inbound runs
    opening = "Hi Jane! What's your insurance carrier and member ID?"
    assert await runner().start_intake(P) == [opening]
    assert await runner().run_proactive(P, Proactive(FOLLOWUP_INSTRUCTIONS)) == [nudge]
    assert seen == []
    # a failed inbound turn (unscripted: the LLM call fails) is answered with the fallback
    assert await runner().handle_inbound(P, "unscripted") == [FALLBACK_TEXT]
    assert seen == []

    assert await runner().handle_inbound(P, "Aetna, member W123456789") == [
        "Thanks! What brings you in?"
    ]
    ((phone, run),) = seen
    assert phone == P
    run.expect.contains_function_call(name="record_insurance")


async def test_start_intake_delivery_failure_after_commit_is_logged_safely(
    tmp_path, intake_llm, make_runner, caplog
):
    # stands for an error that echoes the message; the traceback shows only this name
    error_text = f"gateway down, could not send to {P}: {{msg.text}}"

    class DownChannel(RecordingChannel):
        async def send(self, msg: Outbound) -> None:
            raise ConnectionError(error_text.format(msg=msg))

    db = tmp_path / "db"
    first = "Hi Jane! What's your insurance carrier and member ID?"
    # the turn committed and stands; nothing was sent, so nothing is returned
    assert await make_runner(db, intake_llm, DownChannel()).start_intake(P) == []
    state, version = load(db)
    assert state.intake.progress.pending[0] == "insurance" and version == 1
    (logged,) = [r for r in caplog.records if r.name == "clinic_sms.runner"]
    assert logged.levelno == logging.ERROR and logged.exc_info is None
    message = logged.getMessage()
    assert message.startswith(
        f"delivering the intake's first question for {mask_phone(P)} failed: "
        "builtins.ConnectionError"
    )
    for secret in (P, "gateway down", "insurance"):
        assert secret not in message, secret
    # the next startup delivers it, once; the intake is not asked again
    ch = RecordingChannel()
    assert await make_runner(db, intake_llm, ch).deliver_pending() == 1
    assert ch.sent == [Outbound(to=P, text=first)]
    assert await make_runner(db, intake_llm, ch).start_intake(P) == []
    assert intake_llm.calls.count(InsuranceTask.OPENING) == 1


async def test_start_intake_refuses_the_front_desk_number(
    tmp_path, scripted_llm, make_runner, db_rows
):
    llm = scripted_llm({}, default=Reply(text="What's your insurance?"))
    db, ch = tmp_path / "db", RecordingChannel()
    with pytest.raises(FrontDeskIntakeError):
        await make_runner(db, llm, ch).start_intake(STAFF_PHONE)
    assert llm.calls == [] and ch.sent == []
    for table in ("conversations", "messages", "outbox"):
        assert db_rows(db, f"SELECT count(*) FROM {table}") == [(0,)]
