"""Spike 2: multi-turn tool use through livekit-plugins-anthropic on claude-opus-5-5 (R6).

Throwaway script, not maintained. Three cold starts, each with a brand-new
AgentSession, mirroring spikes/cold_start_spike.py (the structure is duplicated
on purpose):
  1. start: step a asks a question
  2. resume from a, user answers a: a's tool completes it, step b asks
  3. resume from b, user answers b: b's tool completes it, the group finishes

Needs ANTHROPIC_API_KEY in the environment (exit code 2 without it). Costs a few
cents. Everything printed is also written to var/spikes/claude_roundtrip.log.

Run: uv run python spikes/claude_roundtrip.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent.parent / "var" / "spikes" / "claude_roundtrip.log"
TURN_TIMEOUT = 90.0  # thinking adds latency; be generous
STEP_IDS = ["a", "b"]
QUESTIONS = {
    "a": "What is your favorite color?",
    "b": "Which city do you live in?",
}
TOOL_NAMES = {"a": "save_color", "b": "save_city"}


class Out:
    """Print to stdout and mirror to the log file."""

    def __init__(self) -> None:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._f = LOG_PATH.open("w", encoding="utf-8")  # overwrite on each run

    def __call__(self, line: str = "") -> None:
        print(line, flush=True)
        self._f.write(line + "\n")
        self._f.flush()


out: Out | None = None


def say(line: str = "") -> None:
    assert out is not None
    out(line)


class TeeLogHandler(logging.Handler):
    """Route log records (WARNING and above) through say()."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = f"[log] {record.levelname} {record.name}: {record.getMessage()}"
            if record.exc_info:
                msg += "\n" + "".join(traceback.format_exception(*record.exc_info)).rstrip()
            say(msg)
        except Exception:  # never let logging break the spike
            pass


def main_sync() -> int:
    global out
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set; export it in your shell and rerun (exit 2).")
        return 2
    out = Out()
    handler = TeeLogHandler(level=logging.WARNING)
    # One handler on the root logger: livekit.* loggers propagate to it (a second handler on
    # "livekit" would print every record twice).
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    return asyncio.run(main())


# Imports that need the SDK come after the key check so the no-key exit is instant.
from livekit.agents import (  # noqa: E402
    Agent,
    AgentSession,
    AgentTask,
    ConversationItemAddedEvent,
    RunContext,
    function_tool,
    llm,
)
from livekit.agents.beta.workflows import TaskGroup  # noqa: E402
from livekit.agents.llm import ToolError  # noqa: E402
from livekit.agents.types import NOT_GIVEN, NotGivenOr  # noqa: E402

from clinic_sms.llm import MAX_TOKENS, MODEL, make_claude_llm  # noqa: E402


class Probe:
    """Collects conversation_item_added events of one session."""

    def __init__(self, session: AgentSession) -> None:
        self.items: list[llm.ChatItem] = []
        session.on("conversation_item_added", self._on_item)

    def _on_item(self, ev: ConversationItemAddedEvent) -> None:
        self.items.append(ev.item)

    def outbound(self) -> list[str]:
        return [
            i.text_content
            for i in self.items
            if i.type == "message" and i.role == "assistant" and i.text_content
        ]



def tool_calls_of(result: object) -> list[tuple[str, str]]:
    """(name, arguments) of every tool call in a RunResult.

    Reads RunResult.events: FunctionCallEvent is built from function_call chat items in
    RunResult._item_added (livekit/agents/voice/run_result.py:234-235). The session's
    conversation_item_added event does not carry them, so Probe.items never sees them.
    """
    return [(e.item.name, e.item.arguments) for e in result.events if e.type == "function_call"]


class Step(AgentTask[str]):
    def __init__(
        self,
        step_id: str,
        *,
        resuming: bool,
        entered: asyncio.Event,
        chat_ctx: NotGivenOr[llm.ChatContext] = NOT_GIVEN,
    ) -> None:
        super().__init__(
            instructions=(
                "You are a friendly clinic front-desk assistant chatting with a patient by SMS. "
                "Keep every message to one short sentence of plain text. "
                f"Your current job: ask the patient this question: {QUESTIONS[step_id]!r} "
                f"When they answer, call {TOOL_NAMES[step_id]} with their answer."
            ),
            chat_ctx=chat_ctx,
        )
        self.step_id = step_id
        self.resuming = resuming
        self._entered = entered

    async def on_enter(self) -> None:
        self._entered.set()  # first line, before any await (see spike 1)
        if not self.resuming:
            self.session.generate_reply(instructions=f"Ask: {QUESTIONS[self.step_id]}")

    async def _save(self, value: str) -> str:
        self.complete(value)
        return f"saved {self.step_id}"


# One tool per step, with the real tool name the model sees.
class StepA(Step):
    @function_tool
    async def save_color(self, ctx: RunContext, color: str) -> str:
        """Save the patient's favorite color."""
        return await self._save(color)


class StepB(Step):
    @function_tool
    async def save_city(self, ctx: RunContext, city: str) -> str:
        """Save the city the patient lives in."""
        return await self._save(city)


STEP_CLASSES = {"a": StepA, "b": StepB}


class Root(Agent):
    def __init__(self, group: TaskGroup) -> None:
        super().__init__(instructions="root")
        self._group = group
        self.result: object = None

    async def on_enter(self) -> None:
        try:
            self.result = await self._group
        except ToolError as e:  # session.aclose() cancels the waiting step
            self.result = e


class ColdStart:
    """One simulated cold start: fresh session, events and agent chain."""

    def __init__(
        self, *, resume_from: str | None = None, group_ctx: llm.ChatContext | None = None
    ) -> None:
        self.session = AgentSession(llm=make_claude_llm())
        self.probe = Probe(self.session)
        self.entered = {sid: asyncio.Event() for sid in STEP_IDS}
        pending = STEP_IDS if resume_from is None else STEP_IDS[STEP_IDS.index(resume_from) :]
        self.group = TaskGroup(
            summarize_chat_ctx=False,
            chat_ctx=group_ctx if group_ctx is not None else NOT_GIVEN,
        )
        for i, sid in enumerate(pending):
            resuming = resume_from is not None and i == 0
            self.group.add(self._factory(sid, resuming), id=sid, description=f"step {sid}")
        self.root = Root(self.group)

    def _factory(self, sid: str, resuming: bool) -> Callable[[], AgentTask]:
        def make() -> AgentTask:
            return STEP_CLASSES[sid](sid, resuming=resuming, entered=self.entered[sid])

        return make

    async def close(self) -> None:
        try:
            await asyncio.wait_for(self.session.aclose(), 30.0)
        except Exception as e:
            say(f"  | session.aclose() failed: {type(e).__name__}: {e}")


def step_id_of(agent: Agent) -> str:
    return agent.step_id if isinstance(agent, Step) else type(agent).__name__


def current_ctx(cs: ColdStart) -> llm.ChatContext:
    return cs.session.current_agent.chat_ctx.copy()


async def turn(
    name: str,
    *,
    resume_from: str | None,
    ctx: llm.ChatContext | None,
    user_input: str | None,
    failures: list[str],
    expect_tools: list[str],
    expect_agent: str,
    expect_result_keys: set[str] | None,
) -> llm.ChatContext | None:
    """One cold start plus one inbound message. Returns the chat ctx to carry forward."""
    say(f"== {name}: resume_from={resume_from} user_input={user_input!r}")
    cs = ColdStart(resume_from=resume_from, group_ctx=ctx)
    next_ctx: llm.ChatContext | None = None
    t0 = time.monotonic()
    try:
        if user_input is None:
            result = await asyncio.wait_for(
                cs.session.start(cs.root, capture_run=True), TURN_TIMEOUT
            )
            await asyncio.wait_for(result, TURN_TIMEOUT)
        else:
            await asyncio.wait_for(cs.session.start(cs.root), TURN_TIMEOUT)
            await asyncio.wait_for(cs.entered[resume_from or "a"].wait(), TURN_TIMEOUT)
            t0 = time.monotonic()  # latency of the inbound message, not of the start
            result = await asyncio.wait_for(cs.session.run(user_input=user_input), TURN_TIMEOUT)
        elapsed = time.monotonic() - t0
        outbound = cs.probe.outbound()
        calls = tool_calls_of(result)
        say(f"  | latency={elapsed:.2f}s")
        say(f"  | RunResult events={[e.type for e in result.events]}")
        say(f"  | outbound texts={outbound}")
        say(f"  | tool calls={calls}")
        say(f"  | current_agent={step_id_of(cs.session.current_agent)}")
        say(f"  | root.result={cs.root.result!r}")
        say(f"  | items: {[(i.type, getattr(i, 'role', None)) for i in cs.probe.items]}")
        if not outbound:
            failures.append(f"{name}: no non-empty outbound text")
        # State transitions: this turn must call exactly its own tool(s), land on the expected
        # agent, and (final turn only) finish the group with a TaskGroupResult holding b.
        got_tools = [n for n, _ in calls]
        if got_tools != expect_tools:
            failures.append(f"{name}: tool calls {got_tools} != expected {expect_tools}")
        got_agent = step_id_of(cs.session.current_agent)
        if got_agent != expect_agent:
            failures.append(f"{name}: current_agent {got_agent!r} != expected {expect_agent!r}")
        task_results = getattr(cs.root.result, "task_results", None)
        if expect_result_keys is None:
            if cs.root.result is not None:
                failures.append(f"{name}: root.result {cs.root.result!r} != None")
        elif not isinstance(task_results, dict) or not expect_result_keys <= set(task_results):
            failures.append(
                f"{name}: root.result {cs.root.result!r} lacks keys {sorted(expect_result_keys)}"
            )
        next_ctx = current_ctx(cs)
    except BaseException as e:
        if isinstance(e, asyncio.CancelledError):
            raise
        say(f"  | EXCEPTION {type(e).__name__}: {e}")
        say(traceback.format_exc().rstrip())
        failures.append(f"{name}: {type(e).__name__}: {e}")
    finally:
        await cs.close()
    return next_ctx


async def main() -> int:
    say(f"model={MODEL} max_tokens={MAX_TOKENS} log={LOG_PATH}")
    failures: list[str] = []

    ctx_a = await turn(
        "T1 start (a asks)",
        resume_from=None,
        ctx=None,
        user_input=None,
        failures=failures,
        expect_tools=[],
        expect_agent="a",
        expect_result_keys=None,
    )
    ctx_b = None
    if ctx_a is not None:
        ctx_b = await turn(
            "T2 answer a (b asks)",
            resume_from="a",
            ctx=ctx_a,
            user_input="Blue",
            failures=failures,
            expect_tools=["save_color"],
            expect_agent="b",
            expect_result_keys=None,
        )
    if ctx_b is not None:
        await turn(
            "T3 answer b (done)",
            resume_from="b",
            ctx=ctx_b,
            user_input="Portland",
            failures=failures,
            expect_tools=["save_city"],
            expect_agent="Root",
            expect_result_keys={"b"},
        )
    else:
        failures.append("T3 skipped: earlier turn failed")

    if failures:
        say(f"SUMMARY: FAIL - {'; '.join(failures)}")
        return 1
    say("SUMMARY: PASS - outbound text, expected tool, current agent and root.result on every turn; no exception")
    return 0


if __name__ == "__main__":
    sys.exit(main_sync())
