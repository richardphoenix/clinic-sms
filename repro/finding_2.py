"""FINDING-2 repro: the stock TaskGroup discards a step's own chat_ctx.

A step task is built with its own chat_ctx (here: the turns it had in an earlier
session). Awaited directly, the LLM sees those turns. Run inside a stock TaskGroup,
the LLM does not: TaskGroup.on_enter replaces the task's context with a copy of the
group's (livekit-agents 1.8.11, beta/workflows/task_group.py:102-103).

Self-contained: livekit-agents only, an inline fake LLM, no API key.
Run: uv run python repro/finding_2.py
Exits 1 while the finding reproduces, 0 once it no longer does.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from livekit.agents import Agent, AgentSession, AgentTask, llm
from livekit.agents.beta.workflows import TaskGroup
from livekit.agents.llm import ToolError
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions

TIMEOUT = 5.0
EARLIER = "Aetna, member ID W123456789"  # what the user told the step in an earlier session


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
    def __init__(self, chat_ctx: llm.ChatContext, entered: asyncio.Event) -> None:
        super().__init__(instructions="step", chat_ctx=chat_ctx)
        self._entered = entered

    async def on_enter(self) -> None:
        self._entered.set()  # the step has taken over; it does not speak


class Root(Agent):
    def __init__(self, inner: AgentTask) -> None:
        super().__init__(instructions="root")
        self._inner = inner

    async def on_enter(self) -> None:
        try:
            await self._inner
        except ToolError:
            pass  # closing the session cancels the step that is still waiting


def step_ctx() -> llm.ChatContext:
    ctx = llm.ChatContext.empty()
    ctx.add_message(role="assistant", content="What is your insurance carrier and member ID?")
    ctx.add_message(role="user", content=EARLIER)
    return ctx


async def llm_messages(*, via_task_group: bool) -> list[str]:
    """Message texts the LLM gets for the step's next turn."""
    entered = asyncio.Event()
    if via_task_group:
        group = TaskGroup(summarize_chat_ctx=False)
        group.add(lambda: Step(step_ctx(), entered), id="step", description="the step")
        inner: AgentTask = group
    else:
        inner = Step(step_ctx(), entered)

    fake = FakeLLM()
    async with AgentSession(llm=fake) as session:
        await asyncio.wait_for(session.start(Root(inner)), TIMEOUT)
        await asyncio.wait_for(entered.wait(), TIMEOUT)
        await asyncio.wait_for(session.run(user_input="next answer"), TIMEOUT)
    return [i.text_content or "" for i in fake.contexts[-1].items if i.type == "message"]


async def main() -> int:
    import livekit.agents

    print(f"livekit-agents {livekit.agents.__version__}")
    control = await llm_messages(via_task_group=False)
    grouped = await llm_messages(via_task_group=True)
    print(f"step awaited directly  -> LLM messages: {control}")
    print(f"step inside TaskGroup  -> LLM messages: {grouped}")

    if EARLIER not in control:
        print("INCONCLUSIVE: the control run lost the step's context too")
        return 2
    if EARLIER in grouped:
        print("PASS: the step kept its own chat_ctx inside the TaskGroup")
        return 0
    print(
        "REPRODUCED: inside the TaskGroup the LLM never sees the step's own chat_ctx "
        "(replaced by the group's context, task_group.py:102-103)"
    )
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
