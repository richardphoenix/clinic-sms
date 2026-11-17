import asyncio
import logging

import pytest
from livekit.agents import AgentSession

from clinic_sms.agents import IntakeAgent, VisitReasonTask
from clinic_sms.ctx import assistant_texts
from clinic_sms.state import GroupProgress, IntakeState
from clinic_sms.testing import Reply
from clinic_sms.workflow import ReadySignal, UnknownStepError

T = 5.0

RESULTS = {
    "insurance": {"carrier": "Aetna", "member_id": "W123456789"},
    "visit_reason": {"reason": "Follow-up on my knee"},
    "allergies_meds": {"allergies": ["penicillin"], "medications": ["ibuprofen"]},
}


async def test_full_intake_collects_typed_results(intake_llm):
    state = IntakeState()
    ready = ReadySignal()
    agent = IntakeAgent(state=state, ready=ready, summarize=False)
    async with AgentSession(llm=intake_llm) as session:
        await asyncio.wait_for(session.start(agent, capture_run=True), T)
        await ready.wait(T)
        for text in ("Aetna, member W123456789", "Follow-up on my knee", "Penicillin; ibuprofen"):
            await asyncio.wait_for(session.run(user_input=text), T)
        assert agent.state.done
        assert agent.state.results == RESULTS
        assert agent.state.progress is None
        assert assistant_texts(session.history)[-1] == "You're all set. See you Thursday!"


async def test_resuming_step_does_not_ask(intake_llm):
    state = IntakeState(
        progress=GroupProgress(
            pending=["visit_reason", "allergies_meds"],
            results={"insurance": {"carrier": "Aetna", "member_id": "W123456789"}},
            visited=["insurance", "visit_reason"],
        )
    )
    ready = ReadySignal()
    agent = IntakeAgent(state=state, ready=ready, summarize=False)
    async with AgentSession(llm=intake_llm) as session:
        await asyncio.wait_for(session.start(agent, capture_run=True), T)
        assert isinstance(await ready.wait(T), VisitReasonTask)
        assert VisitReasonTask.OPENING not in intake_llm.calls
        # finish the intake so closing the session does not cancel a waiting step
        for text in ("Follow-up on my knee", "Penicillin; ibuprofen"):
            await asyncio.wait_for(session.run(user_input=text), T)
        assert agent.state.results == RESULTS


async def test_completion_with_summary(intake_replies, scripted_llm):
    llm = scripted_llm(intake_replies, default=Reply(text="Patient completed intake."))
    state = IntakeState()
    ready = ReadySignal()
    agent = IntakeAgent(state=state, ready=ready, summarize=True)
    async with AgentSession(llm=llm) as session:
        await asyncio.wait_for(session.start(agent, capture_run=True), T)
        await ready.wait(T)
        for text in ("Aetna, member W123456789", "Follow-up on my knee", "Penicillin; ibuprofen"):
            await asyncio.wait_for(session.run(user_input=text), T)
        assert agent.state.done
        assert IntakeAgent.COMPLETION in llm.calls


# async: AgentTask.__init__ needs a running event loop in 1.8.11 (voice/agent.py:934)
async def test_unknown_step_rejected_at_construction():
    bad = IntakeState(progress=GroupProgress(pending=["zzz"], results={}, visited=[]))
    with pytest.raises(UnknownStepError):
        IntakeAgent(state=bad, ready=ReadySignal())


async def test_session_close_with_step_waiting_is_a_normal_exit(intake_llm, caplog):
    state = IntakeState()
    ready = ReadySignal()
    agent = IntakeAgent(state=state, ready=ready, summarize=False)
    with caplog.at_level(logging.DEBUG):
        session = AgentSession(llm=intake_llm)
        await asyncio.wait_for(session.start(agent, capture_run=True), T)
        await ready.wait(T)
        await asyncio.wait_for(session.run(user_input="Aetna, member W123456789"), T)
        # visit_reason is now waiting for its answer
        agent.mark_closing()  # what the runner does right before aclose()
        await asyncio.wait_for(session.aclose(), T)
        await asyncio.sleep(0.1)  # let on_enter finish
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == []
    assert not agent.state.done
    assert agent.state == IntakeState()  # untouched; the runner snapshots group.progress
    assert agent.group.progress == GroupProgress(
        pending=["visit_reason", "allergies_meds"],
        results={"insurance": {"carrier": "Aetna", "member_id": "W123456789"}},
        visited=["insurance", "visit_reason"],
    )


async def test_close_not_marked_by_the_owner_is_logged_as_a_failure(intake_llm, caplog):
    # Only the owner's mark_closing() makes the cancellation a normal exit; the SDK's
    # ToolError alone does not say why the step ended (FINDING-4).
    agent = IntakeAgent(state=IntakeState(), ready=(ready := ReadySignal()), summarize=False)
    session = AgentSession(llm=intake_llm)
    await asyncio.wait_for(session.start(agent, capture_run=True), T)
    await ready.wait(T)
    await asyncio.wait_for(session.aclose(), T)
    await asyncio.sleep(0.1)  # let on_enter finish
    (failure,) = [r for r in caplog.records if r.name == "clinic_sms.agents.intake"]
    assert failure.levelno == logging.ERROR
    # type name and traceback only: no exc_info, and not the text
    # "AgentTask insurance_task is cancelled" (logsafe.describe_exception: an exception's
    # text may hold the patient's words, so logs never carry it)
    assert failure.exc_info is None
    message = failure.getMessage()
    assert message.startswith(
        "the intake group failed: livekit.agents.llm.tool_context.ToolError\n"
    )
    assert "is cancelled" not in message
