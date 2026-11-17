"""FINDING-7 repro: a TaskGroup resumed in its first visited step offers `out_of_scope`
with an empty enum, which Anthropic rejects.

`TaskGroup._build_out_of_scope_tool` returns None only when the visited set is empty,
then discards the active step from the targets (livekit-agents 1.8.11,
beta/workflows/task_group.py:166-172). Stock `on_enter` builds the tool before it adds
the active step to the visited set (lines 105 and 111), so in the stock flow the active
step is never the only visited one and the enum is never empty. A subclass that restores
the visited set to resume a group (the FINDING-1 use case) can be in that state: resumed
in step a with visited = {a}, the LLM is offered `out_of_scope` with `"enum": []`.

The script runs steps a then b in three groups and formats the tools the LLM was offered
in each step the way livekit-plugins-anthropic 1.8.11 does
(`ToolContext(tools).parse_function_tools("anthropic")`, plugins/anthropic/llm.py:199-202).
Nothing is sent to Anthropic; the 400 it returns for such a schema is "tools.1.custom:
Invalid schema: Enum must be a non-empty array".

  stock             a stock TaskGroup in one session (control)
  visited restored  the same, with the private `_visited_tasks` set to {"a"} before
                    on_enter, as a group resumed in the middle of a must (1.8.11 has no
                    public way to restore it)
  one-line guard    as "visited restored", with the builder returning None when no
                    target is left after the discard

Self-contained: livekit-agents only, an inline fake LLM, no API key.
Run: uv run python repro/finding_7.py
Exits 1 while the finding reproduces, 0 once it no longer does.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import sys
from typing import Any

from livekit.agents import Agent, AgentSession, AgentTask, RunContext, function_tool, llm
from livekit.agents.beta.workflows import TaskGroup
from livekit.agents.llm import ToolError
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions

TIMEOUT = 5.0


class FakeLLM(llm.LLM):
    """Calls `save` when the user says "save", else replies "ok"; records each call's tools."""

    def __init__(self) -> None:
        super().__init__()
        self.offered: list[list[llm.Tool]] = []  # tools of each "hello" call, in order
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
        if text == "hello":
            self.offered.append(list(tools or []))
        call = None
        if text == "save":
            call = llm.FunctionToolCall(
                name="save", arguments=json.dumps({}), call_id=f"call_{next(self._ids)}"
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


class Step(AgentTask[None]):
    def __init__(self, entered: asyncio.Event) -> None:
        super().__init__(instructions="step")
        self._entered = entered

    async def on_enter(self) -> None:
        self._entered.set()  # the step has taken over; it does not speak

    @function_tool
    async def save(self, ctx: RunContext) -> None:
        """Finish this step."""
        self.complete(None)


class VisitedRestored(TaskGroup):
    """Restores the visited set, as a group resumed from saved progress must."""

    def __init__(self, visited: set[str]) -> None:
        super().__init__(summarize_chat_ctx=False)
        self._visited_tasks = set(visited)  # private: no public way in 1.8.11


class WithGuard(VisitedRestored):
    """The one-line hardening: no tool when no target is left after the discard."""

    def _build_out_of_scope_tool(self, *, active_task_id: str) -> llm.FunctionTool | None:
        if not self._visited_tasks - {active_task_id}:
            return None
        return super()._build_out_of_scope_tool(active_task_id=active_task_id)


class Root(Agent):
    def __init__(self, group: TaskGroup) -> None:
        super().__init__(instructions="root")
        self._group = group

    async def on_enter(self) -> None:
        try:
            await self._group
        except ToolError:
            pass  # closing the session cancels a step that is still waiting


def empty_enums(schema: Any, path: str) -> list[str]:
    if isinstance(schema, dict):
        found = [f"{path}.enum" for k, v in schema.items() if k == "enum" and v == []]
        for k, v in schema.items():
            found += empty_enums(v, f"{path}.{k}")
        return found
    if isinstance(schema, list):
        return [p for i, v in enumerate(schema) for p in empty_enums(v, f"{path}[{i}]")]
    return []


def describe(tools: list[llm.Tool]) -> tuple[str, list[str]]:
    """What the LLM was offered, and the paths of empty enums in the Anthropic schemas."""
    schemas = llm.ToolContext(tools).parse_function_tools("anthropic")
    parts = []
    for schema in schemas:
        if schema["name"] == "out_of_scope":
            enum = schema["input_schema"]["properties"]["task_ids"]["items"]["enum"]
            parts.append(f"out_of_scope(enum={enum})")
        else:
            parts.append(schema["name"])
    return ", ".join(parts), empty_enums(schemas, "tools")


async def run(group: TaskGroup) -> list[tuple[str, list[str]]]:
    """Say "hello" in step a, finish a, say "hello" in step b; describe both offers."""
    entered = {sid: asyncio.Event() for sid in ("a", "b")}
    for sid in ("a", "b"):
        group.add(lambda sid=sid: Step(entered[sid]), id=sid, description=f"step {sid}")
    fake = FakeLLM()
    async with AgentSession(llm=fake) as session:
        await asyncio.wait_for(session.start(Root(group)), TIMEOUT)
        await asyncio.wait_for(entered["a"].wait(), TIMEOUT)
        await asyncio.wait_for(session.run(user_input="hello"), TIMEOUT)
        await asyncio.wait_for(session.run(user_input="save"), TIMEOUT)
        await asyncio.wait_for(entered["b"].wait(), TIMEOUT)
        await asyncio.wait_for(session.run(user_input="hello"), TIMEOUT)
    return [describe(tools) for tools in fake.offered]


async def main() -> int:
    print(f"livekit-agents {importlib.metadata.version('livekit-agents')}")
    results = {
        "stock": await run(TaskGroup(summarize_chat_ctx=False)),
        "visited restored": await run(VisitedRestored({"a"})),
        "one-line guard": await run(WithGuard({"a"})),
    }
    for name, offers in results.items():
        for step, (offered, empty) in zip(("a", "b"), offers, strict=True):
            print(f"{name:16} step {step}: offered [{offered}]  empty enums: {empty}")

    def has_empty(name: str) -> bool:
        return any(empty for _, empty in results[name])

    if has_empty("stock") or has_empty("one-line guard"):
        print("INCONCLUSIVE: an empty enum outside the restored case")
        return 2
    if has_empty("visited restored"):
        print(
            "REPRODUCED: resumed in its first visited step, the TaskGroup offers out_of_scope "
            "with an empty enum (Anthropic: 400 Enum must be a non-empty array); the stock "
            "flow never does, and the one-line guard removes the tool"
        )
        return 1
    print("NOT REPRODUCED: no empty enum in the restored case")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
