"""FINDING-1 repro: a stock TaskGroup cannot start from saved progress.

Two steps, a then b, each with a `save(value)` tool that completes the step. A long-lived
session runs both (control). The cold start is what the stock API allows after a restart
between the steps: a new session whose TaskGroup has only the remaining step b
registered, because TaskGroup.__init__ takes no progress and on_enter always starts with
every registered step and empty results (livekit-agents 1.8.11,
beta/workflows/task_group.py:47-71, 93-94). The script checks that, in the cold start:

  - the group's result lacks the completed step's result ('a');
  - the LLM in step b is not offered `out_of_scope`, so the user cannot go back to a
    (the visited set starts empty, task_group.py:69 and 167-168).

Self-contained: livekit-agents only, an inline fake LLM, no API key.
Run: uv run python repro/finding_1.py
Exits 1 while the finding reproduces, 0 once it no longer does.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import inspect
import json
import sys
from typing import Any

from livekit.agents import Agent, AgentSession, AgentTask, RunContext, function_tool, llm
from livekit.agents.beta.workflows import TaskGroup
from livekit.agents.llm import ToolError
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions

TIMEOUT = 5.0
ANSWERS = {"a1": ("save", {"value": "a1"}), "b1": ("save", {"value": "b1"})}


class FakeLLM(llm.LLM):
    """Calls `save` for a known answer, else replies "ok"; records each call's tools."""

    def __init__(self) -> None:
        super().__init__()
        self.offered: dict[str, list[llm.Tool]] = {}  # user text -> tools of that call
        self._ids = iter(range(1, 1000))

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        **_: Any,
    ) -> llm.LLMStream:
        last = chat_ctx.items[-1] if chat_ctx.items else None
        text = last.text_content if last is not None and last.type == "message" else None
        call = None
        if text in ANSWERS:
            self.offered[text] = list(tools or [])
            name, args = ANSWERS[text]
            call = llm.FunctionToolCall(
                name=name, arguments=json.dumps(args), call_id=f"call_{next(self._ids)}"
            )
        return _Stream(
            self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options, call=call
        )


class _Stream(llm.LLMStream):
    def __init__(self, *args: Any, call: llm.FunctionToolCall | None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._call = call

    async def _run(self) -> None:
        delta = (
            llm.ChoiceDelta(role="assistant", tool_calls=[self._call])
            if self._call is not None
            else llm.ChoiceDelta(role="assistant", content="ok")
        )
        self._event_ch.send_nowait(llm.ChatChunk(id="fake", delta=delta))


class Step(AgentTask[str]):
    def __init__(self, step_id: str, entered: dict[str, asyncio.Event]) -> None:
        super().__init__(instructions=f"step {step_id}")
        self._entered = entered[step_id]

    async def on_enter(self) -> None:
        self._entered.set()  # the step has taken over; it does not speak

    @function_tool
    async def save(self, ctx: RunContext, value: str) -> None:
        """Save the user's answer for this step."""
        self.complete(value)


class Root(Agent):
    def __init__(self, group: TaskGroup) -> None:
        super().__init__(instructions="root")
        self._group = group
        self.result: Any = None

    async def on_enter(self) -> None:
        try:
            self.result = await self._group
        except ToolError:
            pass  # closing the session cancels a step that is still waiting


def offer(tools: list[llm.Tool]) -> str:
    names = sorted(llm.ToolContext(tools).function_tools)
    targets = None
    for schema in llm.ToolContext(tools).parse_function_tools("anthropic"):
        if schema["name"] == "out_of_scope":
            targets = schema["input_schema"]["properties"]["task_ids"]["items"]["enum"]
    return f"tools {names}, out_of_scope targets {targets}"


async def run(step_ids: list[str], answers: list[str]) -> tuple[FakeLLM, Root]:
    entered = {sid: asyncio.Event() for sid in step_ids}
    group = TaskGroup(summarize_chat_ctx=False)
    for sid in step_ids:
        group.add(lambda sid=sid: Step(sid, entered), id=sid, description=f"step {sid}")
    fake, root = FakeLLM(), Root(group)
    async with AgentSession(llm=fake) as session:
        await asyncio.wait_for(session.start(root), TIMEOUT)
        for sid, answer in zip(step_ids, answers, strict=True):
            await asyncio.wait_for(entered[sid].wait(), TIMEOUT)
            await asyncio.wait_for(session.run(user_input=answer), TIMEOUT)
    return fake, root


async def main() -> int:
    print(f"livekit-agents {importlib.metadata.version('livekit-agents')}")
    params = [p for p in inspect.signature(TaskGroup.__init__).parameters if p != "self"]
    print(f"TaskGroup.__init__ parameters: {params}")
    long_lived, root_ll = await run(["a", "b"], ["a1", "b1"])
    cold, root_cold = await run(["b"], ["b1"])
    print(f"long-lived, a then b       : step b offered {offer(long_lived.offered['b1'])}; "
          f"result {root_ll.result.task_results}")
    print(f"cold start, only b left    : step b offered {offer(cold.offered['b1'])}; "
          f"result {root_cold.result.task_results}")

    control_ok = root_ll.result.task_results == {"a": "a1", "b": "b1"}
    if not control_ok:
        print("INCONCLUSIVE: the long-lived control did not collect both results")
        return 2
    lost_result = "a" not in root_cold.result.task_results
    no_regression = "out_of_scope" not in llm.ToolContext(cold.offered["b1"]).function_tools
    if lost_result and no_regression:
        print(
            "REPRODUCED: started from 'only the remaining steps', the stock TaskGroup's "
            "result lacks the completed step and out_of_scope cannot offer it"
        )
        return 1
    print("NOT REPRODUCED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
