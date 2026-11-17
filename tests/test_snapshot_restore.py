import asyncio

from livekit.agents import AgentSession

from clinic_sms.agents import InsuranceTask, IntakeAgent, VisitReasonTask
from clinic_sms.ctx import normalize_ctx
from clinic_sms.state import IntakeState
from clinic_sms.testing import Reply, ScriptedLLM, tool_names
from clinic_sms.workflow import ReadySignal

T = 5.0


def offered(llm: ScriptedLLM, start: int = 0) -> list[tuple[str, list[str]]]:
    """(key, names of the tools offered) of every call `llm` received from `start` on."""
    return [(key, tool_names(tools)) for key, tools in zip(llm.calls, llm.tools, strict=True)][
        start:
    ]


async def run_and_snapshot(
    llm: ScriptedLLM, texts: list[str], next_text: str
) -> tuple[dict[str, list[tuple]], IntakeState, list[tuple[str, list[str]]]]:
    """Session A: fresh intake, feed `texts`, snapshot, then feed `next_text`, close.

    Returns each layer's normalized context at the snapshot, the snapshot after a JSON
    round trip (what the store keeps), and the LLM calls `next_text` caused in this
    long-lived session, with the tools each was offered.
    """
    ready = ReadySignal()
    agent = IntakeAgent(state=IntakeState(), ready=ready)
    session = AgentSession(llm=llm)
    try:
        await asyncio.wait_for(session.start(agent, capture_run=True), T)
        await ready.wait(T)
        for text in texts:
            await asyncio.wait_for(session.run(user_input=text), T)
        assert agent.group is not None
        expected = {
            "root": normalize_ctx(agent.chat_ctx),
            "group": normalize_ctx(agent.group.chat_ctx),
            "step": normalize_ctx(session.current_agent.chat_ctx),
        }
        snap = IntakeState.model_validate_json(agent.snapshot().model_dump_json())
        start = len(llm.calls)
        await asyncio.wait_for(session.run(user_input=next_text), T)
        next_calls = offered(llm, start)
    finally:
        agent.mark_closing()  # as the runner does: a step is still waiting
        await asyncio.wait_for(session.aclose(), T)
    return expected, snap, next_calls


async def test_round_trip_between_steps(scripted_llm, intake_replies):
    next_text = "Follow-up on my knee"
    expected, snap, long_lived = await run_and_snapshot(
        scripted_llm(intake_replies), ["Aetna, member W123456789"], next_text
    )
    llm_b = scripted_llm(intake_replies)
    ready = ReadySignal()
    agent_b = IntakeAgent(state=snap, ready=ready)
    session_b = AgentSession(llm=llm_b)
    try:
        await asyncio.wait_for(session_b.start(agent_b), T)
        await ready.wait(T)
        assert isinstance(session_b.current_agent, VisitReasonTask)
        assert normalize_ctx(session_b.current_agent.chat_ctx) == expected["step"]
        # the other two layers come back too; the group keeps the finished step's tool
        # call, which the stock TaskGroup merges in for its summary
        assert agent_b.group is not None
        assert normalize_ctx(agent_b.group.chat_ctx) == expected["group"]
        assert ("function_call_output", "record_insurance", "") in (
            normalize_ctx(agent_b.group.chat_ctx)
        )
        assert normalize_ctx(agent_b.chat_ctx) == expected["root"]
        # the next message: the same LLM calls, offered the same tools
        await asyncio.wait_for(session_b.run(user_input=next_text), T)
        assert offered(llm_b) == long_lived
    finally:
        agent_b.mark_closing()
        await asyncio.wait_for(session_b.aclose(), T)


async def test_round_trip_mid_step(scripted_llm, intake_replies):
    next_text = "W123456789"
    expected, snap, long_lived = await run_and_snapshot(
        scripted_llm(intake_replies), ["Aetna"], next_text
    )
    llm_b = scripted_llm(intake_replies)
    ready = ReadySignal()
    agent_b = IntakeAgent(state=snap, ready=ready)
    session_b = AgentSession(llm=llm_b)
    try:
        await asyncio.wait_for(session_b.start(agent_b), T)
        await ready.wait(T)
        assert isinstance(session_b.current_agent, InsuranceTask)
        step_ctx = normalize_ctx(session_b.current_agent.chat_ctx)
        assert step_ctx == expected["step"]
        assert ("message", "user", "Aetna") in step_ctx
        assert ("message", "assistant", "Got it. What's your member ID?") in step_ctx
        assert agent_b.group is not None
        assert normalize_ctx(agent_b.group.chat_ctx) == expected["group"]
        assert normalize_ctx(agent_b.chat_ctx) == expected["root"]
        # the next message: the same LLM calls, offered the same tools (the first step
        # has no step to go back to, so no out_of_scope tool)
        await asyncio.wait_for(session_b.run(user_input=next_text), T)
        assert offered(llm_b) == long_lived
        # every intake step also offers escalate_to_staff (spec §5.4)
        assert long_lived[0] == (next_text, ["escalate_to_staff", "record_insurance"])
    finally:
        agent_b.mark_closing()
        await asyncio.wait_for(session_b.aclose(), T)


async def test_snapshot_after_completion(intake_llm):
    ready = ReadySignal()
    agent = IntakeAgent(state=IntakeState(), ready=ready, summarize=False)
    session = AgentSession(llm=intake_llm)
    try:
        await asyncio.wait_for(session.start(agent, capture_run=True), T)
        await ready.wait(T)
        for text in ("Aetna, member W123456789", "Follow-up on my knee", "Penicillin; ibuprofen"):
            await asyncio.wait_for(session.run(user_input=text), T)
        snap = agent.snapshot()
    finally:
        await asyncio.wait_for(session.aclose(), T)
    assert snap.done and snap.progress is None
    assert set(snap.results) == {"insurance", "visit_reason", "allergies_meds"}
    assert snap.root_ctx is not None
    assert snap.group_ctx is None and snap.step_ctx is None
