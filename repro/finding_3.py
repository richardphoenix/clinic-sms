"""FINDING-3 repro: run() fed right after a plain start() raises or is misrouted.

Chain: Root(Agent).on_enter awaits a stock TaskGroup with one step; the step does not
speak in on_enter (like a step resumed in the middle). After session.start() returns,
the script yields to the event loop n times and then calls
session.run(user_input=...). Each n gets a fresh session. Outcomes:

  raised     run() raised RuntimeError ("speech scheduling is draining"); in some windows
             the LLM is still called with the input (see "LLM calls by")
  misrouted  run() returned, but an agent other than the step answered (the TaskGroup,
             instructions "*empty*", or a call with no instructions at all)
  ok         run() returned and only the step answered

Control A waits for the step's on_enter to have been called before run(). Control B
uses session.start(..., capture_run=True), awaits that RunResult, then calls run() at once;
it also checks that the last agent_handoff in that RunResult's events is to the step.
SDK log records (WARNING and up) are counted per feed and listed once at the end.

Self-contained: livekit-agents only, an inline fake LLM, no API key.
Run: uv run python repro/finding_3.py
Exits 1 while the finding reproduces, 0 once it no longer does.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections import Counter
from typing import Any

from livekit.agents import Agent, AgentSession, AgentTask, llm
from livekit.agents.beta.workflows import TaskGroup
from livekit.agents.llm import ToolError
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions

TIMEOUT = 5.0
MAX_YIELDS = 40
CONTROL_RUNS = 20


class FakeLLM(llm.LLM):
    """Replies "ok" to every call and records the context it was given."""

    def __init__(self) -> None:
        super().__init__()
        self.contexts: list[llm.ChatContext] = []

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        **_: Any,
    ) -> llm.LLMStream:
        self.contexts.append(chat_ctx.copy())
        return _OkStream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options)


class _OkStream(llm.LLMStream):
    async def _run(self) -> None:
        delta = llm.ChoiceDelta(role="assistant", content="ok")
        self._event_ch.send_nowait(llm.ChatChunk(id="fake", delta=delta))


class Step(AgentTask[None]):
    def __init__(self, entered: asyncio.Event) -> None:
        super().__init__(instructions="step")
        self._entered = entered

    async def on_enter(self) -> None:
        self._entered.set()  # the step has taken over; it does not speak


class Root(Agent):
    def __init__(self, group: TaskGroup) -> None:
        super().__init__(instructions="root")
        self._group = group

    async def on_enter(self) -> None:
        try:
            await self._group
        except ToolError:
            pass  # closing the session cancels the step that is still waiting


class LogCapture(logging.Handler):
    """Keeps SDK log records as one-line strings."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        line = f"{record.levelname} {record.name}: {record.getMessage()}"
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            line += f" ({type(exc).__name__}: {exc})"
        self.lines.append(line)


LOGS = LogCapture()
RUN_ERRORS: Counter[str] = Counter()  # message of every RuntimeError raised by run()
HANDOFF_TO_STEP: Counter[bool] = Counter()  # control B: last handoff in start's RunResult


def answered_by(ctx: llm.ChatContext) -> str:
    """Instructions of the agent that made this LLM call."""
    for item in ctx.items:
        if item.type == "message" and item.role == "system":
            return item.text_content or ""
    return "<no instructions>"


async def feed(*, wait: str, yields: int = 0) -> tuple[str, bool, list[str], list[str]]:
    """One fresh session; `wait` is "yields", "on_enter" or "capture_run"."""
    LOGS.lines.clear()
    entered = asyncio.Event()
    group = TaskGroup(summarize_chat_ctx=False)
    group.add(lambda: Step(entered), id="step", description="the step")
    fake = FakeLLM()
    async with AgentSession(llm=fake) as session:
        if wait == "capture_run":
            started = await asyncio.wait_for(session.start(Root(group), capture_run=True), TIMEOUT)
            await asyncio.wait_for(started, TIMEOUT)
            handoffs = [ev for ev in started.events if ev.type == "agent_handoff"]
            HANDOFF_TO_STEP[bool(handoffs) and isinstance(handoffs[-1].new_agent, Step)] += 1
        else:
            await asyncio.wait_for(session.start(Root(group)), TIMEOUT)
        if wait == "on_enter":
            await asyncio.wait_for(entered.wait(), TIMEOUT)
        for _ in range(yields):
            await asyncio.sleep(0)
        was_entered = entered.is_set()
        try:
            await asyncio.wait_for(session.run(user_input="hello"), TIMEOUT)
            raised = False
        except RuntimeError as e:
            raised = True
            RUN_ERRORS[str(e)] += 1
        # let the chain settle before closing, so closing does not race the handoff
        await asyncio.wait_for(entered.wait(), TIMEOUT)
        await asyncio.sleep(0.05)
    callers = [answered_by(ctx) for ctx in fake.contexts]
    if raised:
        outcome = "raised"
    elif callers and all(c == "step" for c in callers):
        outcome = "ok"
    else:
        outcome = "misrouted"
    return outcome, was_entered, callers, list(LOGS.lines)


async def main() -> int:
    import livekit.agents

    print(f"livekit-agents {livekit.agents.__version__}")
    print(f"run() after n x asyncio.sleep(0) following start(), n = 0..{MAX_YIELDS - 1}:")
    early: Counter[str] = Counter()
    logged: Counter[str] = Counter()
    for n in range(MAX_YIELDS):
        outcome, was_entered, callers, logs = await feed(wait="yields", yields=n)
        early[outcome] += 1
        logged.update(logs)
        print(
            f"  n={n:2d}: step entered={was_entered!s:5}  {outcome:9}  "
            f"LLM calls by {callers}  SDK log records={len(logs)}"
        )

    controls: dict[str, Counter[str]] = {"on_enter": Counter(), "capture_run": Counter()}
    for wait, counts in controls.items():
        for _ in range(CONTROL_RUNS):
            outcome, _, _, logs = await feed(wait=wait)
            counts[outcome] += 1
            logged.update(logs)

    for msg, count in RUN_ERRORS.items():
        print(f"run() raised x{count}: RuntimeError: {msg}")
    for line, count in logged.items():
        print(f"SDK log record x{count}: {line}")
    on_enter, capture_run = controls["on_enter"], controls["capture_run"]
    print(f"early feeds after plain start(): {dict(early)}")
    print(f"control A, run() after the step's on_enter, {CONTROL_RUNS} runs: {dict(on_enter)}")
    print(
        f"control B, run() right after awaiting start(capture_run=True), "
        f"{CONTROL_RUNS} runs: {dict(capture_run)}; last agent_handoff in its events "
        f"is to the step in {HANDOFF_TO_STEP[True]}/{CONTROL_RUNS}"
    )
    if on_enter["ok"] != CONTROL_RUNS:
        print("INCONCLUSIVE: control A did not always reach the step")
        return 2
    bad = early["raised"] + early["misrouted"]
    if bad == 0:
        print(f"PASS: all {MAX_YIELDS} early feeds after plain start() reached the step")
        return 0
    print(
        f"REPRODUCED: {bad}/{MAX_YIELDS} early feeds after plain start() went wrong "
        f"(raised={early['raised']}, misrouted={early['misrouted']}); "
        f"control A {on_enter['ok']}/{CONTROL_RUNS} ok, "
        f"control B {capture_run['ok']}/{CONTROL_RUNS} ok"
    )
    return 1


if __name__ == "__main__":
    logging.getLogger().addHandler(LOGS)
    sys.exit(asyncio.run(main()))
