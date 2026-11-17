import asyncio

import pytest
from livekit.agents import Agent, AgentSession, AgentTask, RunContext, function_tool
from livekit.agents.beta.workflows import TaskGroup
from livekit.agents.llm import ChatContext, ToolError
from pydantic import BaseModel, ValidationError

from clinic_sms.state import GroupProgress
from clinic_sms.testing import Reply, ScriptedLLM, anthropic_tool_schemas, tool_names
from clinic_sms.workflow import ReadySignal, ResumableTaskGroup, StepFactory, UnknownStepError

T = 5.0

SCRIPT = {
    "ask a": Reply(text="What is A?"),
    "a1": Reply(tools=[("save", {"value": "a1"})]),
    "ask b": Reply(text="What is B?"),
    "b1": Reply(tools=[("save", {"value": "b1"})]),
    "redo a": Reply(tools=[("out_of_scope", {"task_ids": ["a"]})]),
}

# a done, b started: what a cold start in the middle of b restores
AT_B = GroupProgress(pending=["b"], results={"a": {"value": "a1"}}, visited=["a", "b"])


class Val(BaseModel):
    value: str


class Step(AgentTask[Val]):
    def __init__(
        self, step_id: str, *, resuming: bool, log: list[tuple[str, bool]], ready: ReadySignal
    ) -> None:
        super().__init__(instructions=f"step {step_id}")
        self.step_id = step_id
        self._resuming = resuming
        self._log = log
        self._ready = ready

    async def on_enter(self) -> None:
        self._ready.set(self)  # first statement, before any await
        self._log.append((self.step_id, self._resuming))
        if not self._resuming:
            self.session.generate_reply(instructions=f"ask {self.step_id}")

    @function_tool
    async def save(self, ctx: RunContext, value: str) -> str:
        """Save the user's answer for this step."""
        self.complete(Val(value=value))
        return f"saved {self.step_id}"


class Root(Agent):
    def __init__(self, group: ResumableTaskGroup) -> None:
        super().__init__(instructions="root")
        self._group = group
        self.result: object = None

    async def on_enter(self) -> None:
        try:
            self.result = await self._group
        except ToolError as e:
            # closing the session cancels the step that is still waiting
            self.result = e


def make_step(
    step_id: str, log: list[tuple[str, bool]] | None = None, ready: ReadySignal | None = None
) -> StepFactory:
    log = [] if log is None else log
    ready = ReadySignal() if ready is None else ready
    return lambda resuming: Step(step_id, resuming=resuming, log=log, ready=ready)


class Chain:
    """One cold start: fresh LLM, ReadySignal and Root -> group(a, b)."""

    def __init__(
        self,
        llm: ScriptedLLM,
        *,
        progress: GroupProgress | None = None,
        step_chat_ctx: ChatContext | None = None,
    ) -> None:
        self.log: list[tuple[str, bool]] = []
        self.ready = ReadySignal()
        self.llm = llm
        # no summary: it is inherited upstream code, and an extra LLM call here
        self.group = ResumableTaskGroup(
            progress=progress, step_chat_ctx=step_chat_ctx, summarize_chat_ctx=False
        )
        for sid in ("a", "b"):
            self.group.add_step(
                make_step(sid, self.log, self.ready),
                id=sid,
                description=f"step {sid}",
                result_type=Val,
            )
        self.root = Root(self.group)

    async def start(self, session: AgentSession) -> Agent:
        """Start the session and wait until the innermost step has taken over."""
        await asyncio.wait_for(session.start(self.root), T)
        return await self.ready.wait(T)


@pytest.fixture
def make_chain(scripted_llm):
    """`make_chain(progress=..., step_chat_ctx=...)`: a Chain with a fresh checked LLM."""

    def make(**kwargs) -> Chain:
        # default=Reply(): `save` returns a string, so the SDK calls the LLM again with
        # its output (voice/generation.py:1241); that call is not scripted
        return Chain(scripted_llm(SCRIPT, default=Reply()), **kwargs)

    return make


def assistant_or_user_texts(ctx: ChatContext) -> list[str | None]:
    return [i.text_content for i in ctx.items if i.type == "message"]


async def test_runs_steps_in_order_and_returns_typed_results(make_chain):
    chain = make_chain()
    async with AgentSession(llm=chain.llm) as s:
        await chain.start(s)
        await asyncio.wait_for(s.run(user_input="a1"), T)
        await asyncio.wait_for(s.run(user_input="b1"), T)
    assert chain.log == [("a", False), ("b", False)]
    assert chain.root.result.task_results == {"a": Val(value="a1"), "b": Val(value="b1")}


async def test_progress_reflects_current_step(make_chain):
    chain = make_chain()
    group = chain.group
    async with AgentSession(llm=chain.llm) as s:
        await chain.start(s)
        assert group.progress == GroupProgress(pending=["a", "b"], results={}, visited=["a"])
        assert group.current_step_id == "a"
        await asyncio.wait_for(s.run(user_input="a1"), T)
        # b is waiting for its answer
        assert group.progress == GroupProgress(
            pending=["b"], results={"a": {"value": "a1"}}, visited=["a", "b"]
        )
        assert group.current_step_id == "b"


async def test_resume_skips_completed_and_flags_first_step(make_chain):
    chain = make_chain(progress=AT_B)
    async with AgentSession(llm=chain.llm) as s:
        await chain.start(s)
        assert chain.log == [("b", True)]  # a never entered, b not asked
        assert chain.llm.calls == []
        await asyncio.wait_for(s.run(user_input="b1"), T)
    assert chain.root.result.task_results == {"a": Val(value="a1"), "b": Val(value="b1")}


async def test_resume_keeps_out_of_scope_targets(make_chain):
    chain = make_chain(progress=AT_B)
    async with AgentSession(llm=chain.llm) as s:
        await chain.start(s)
        await asyncio.wait_for(s.run(user_input="redo a"), T)
        assert chain.log[-1] == ("a", False)
        assert chain.group.current_step_id == "a"
        assert chain.group.progress.pending == ["a", "b"]


async def test_resumed_step_uses_step_chat_ctx(make_chain):
    step_ctx = ChatContext.empty()
    step_ctx.add_message(role="user", content="earlier text")
    chain = make_chain(progress=AT_B, step_chat_ctx=step_ctx)
    async with AgentSession(llm=chain.llm) as s:
        current_step = await chain.start(s)
        assert current_step is chain.group.current_task
        assert "earlier text" in assistant_or_user_texts(current_step.chat_ctx)


def offered(llm: ScriptedLLM) -> list[tuple[str, list[str], list[str] | None]]:
    """(key, tool names, sorted out_of_scope targets or None) of every call to `llm`."""
    out = []
    for key, tools in zip(llm.calls, llm.tools, strict=True):
        targets = None
        for schema in anthropic_tool_schemas(tools):
            if schema["name"] == "out_of_scope":
                targets = sorted(schema["input_schema"]["properties"]["task_ids"]["items"]["enum"])
        out.append((key, tool_names(tools), targets))
    return out


# "x" is unscripted: an empty reply, so each "x" only shows what the current step offers
EQUIVALENCE_INPUTS = ["x", "a1", "x", "redo a", "x"]


async def test_tools_offered_match_a_long_lived_stock_group(scripted_llm):
    # (a) a stock TaskGroup in one session that never restarts
    stock_llm = scripted_llm(SCRIPT, default=Reply())
    ready = ReadySignal()
    stock = TaskGroup(summarize_chat_ctx=False)
    for sid in ("a", "b"):
        stock.add(
            lambda sid=sid: Step(sid, resuming=False, log=[], ready=ready),
            id=sid,
            description=f"step {sid}",
        )
    async with AgentSession(llm=stock_llm) as s:
        run = await asyncio.wait_for(s.start(Root(stock), capture_run=True), T)
        await asyncio.wait_for(run, T)
        for text in EQUIVALENCE_INPUTS:
            await asyncio.wait_for(s.run(user_input=text), T)

    # (b) a new session per input, resumed from the progress the previous one ended with:
    # covers first entry, restart mid-step (in the first step), restart after a step
    # completed, and restart after a regression
    cold_llm = scripted_llm(SCRIPT, default=Reply())
    progress: GroupProgress | None = None
    for text in [None, *EQUIVALENCE_INPUTS]:
        chain = Chain(cold_llm, progress=progress)
        async with AgentSession(llm=cold_llm) as s:
            if text is None:
                run = await asyncio.wait_for(s.start(chain.root, capture_run=True), T)
                await asyncio.wait_for(run, T)
            else:
                await chain.start(s)
                await asyncio.wait_for(s.run(user_input=text), T)
            progress = chain.group.progress

    assert offered(cold_llm) == offered(stock_llm)
    # the points the comparison covers, as the stock group offers them
    assert [o for o in offered(stock_llm) if o[0] == "x"] == [
        ("x", ["save"], None),  # in the first step: no step to go back to
        ("x", ["out_of_scope", "save"], ["a"]),
        ("x", ["out_of_scope", "save"], ["b"]),  # a again, after the regression
    ]


# async although nothing is awaited: AgentTask.__init__ creates an asyncio.Future
# (voice/agent.py:934 in 1.8.11), which needs a current event loop
async def test_unknown_step_in_progress_raises():
    g = ResumableTaskGroup(progress=GroupProgress(pending=["zzz"], results={}, visited=[]))
    g.add_step(make_step("a"), id="a", description="A", result_type=Val)
    with pytest.raises(UnknownStepError):
        g.validate_progress()


async def test_unknown_step_in_results_raises():
    progress = GroupProgress(pending=["a"], results={"zzz": {"value": "x"}}, visited=["a"])
    g = ResumableTaskGroup(progress=progress)
    g.add_step(make_step("a"), id="a", description="A", result_type=Val)
    with pytest.raises(UnknownStepError, match="zzz"):
        g.validate_progress()


async def test_result_that_no_longer_parses_raises():
    # e.g. the result schema changed after the result was stored
    progress = GroupProgress(pending=["b"], results={"a": {"val": "a1"}}, visited=["a", "b"])
    g = ResumableTaskGroup(progress=progress)
    for sid in ("a", "b"):
        g.add_step(make_step(sid), id=sid, description=sid.upper(), result_type=Val)
    with pytest.raises(ValidationError):
        g.validate_progress()


async def test_ready_signal_first_call_wins():
    ready = ReadySignal()
    first, second = Agent(instructions="first"), Agent(instructions="second")
    ready.set(first)
    ready.set(second)
    assert await ready.wait(T) is first


async def test_ready_signal_wait_times_out():
    with pytest.raises(TimeoutError):
        await ReadySignal().wait(0.01)
