"""FINDING-5 repro: every activation appends a fresh agent_config_update after a restored
chat_ctx, so no stored form gives back the context of a session that never restarted.

Session 1 (long-lived): agent "a" starts and asks a question in on_enter. Its chat_ctx
is then: system 'a', agent_config_update, assistant 'What is A?'.

Cold start: a new agent "a" is built with that context after a to_dict/from_dict round
trip (what a store keeps) and started in a new session; its on_enter stays silent, as a
resumed step does. Two ways to store the context are tried:

  - keep: to_dict(exclude_timestamp=False), every item;
  - drop: to_dict(exclude_timestamp=False, exclude_config_update=True).

The script checks that:

  - after start, neither stored form has session 1's item sequence: the SDK appends an
    agent_config_update stamped "now", after every restored item
    (voice/agent_activity.py:1335-1350, run on start and on resume);
  - "keep" grows by one agent_config_update on every cold start;
  - the Anthropic request built from each context is identical, so the LLM never sees
    the difference (only message / function_call / function_call_output items are sent).

Self-contained: livekit-agents only, an inline fake LLM, no API key.
Run: uv run python repro/finding_5.py
Exits 1 while the finding reproduces, 0 once it no longer does.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import sys
from typing import Any

from livekit.agents import Agent, AgentSession, llm
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions

TIMEOUT = 5.0


class FakeLLM(llm.LLM):
    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        **_: Any,
    ) -> llm.LLMStream:
        return _Stream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options)


class _Stream(llm.LLMStream):
    async def _run(self) -> None:
        delta = llm.ChoiceDelta(role="assistant", content="What is A?")
        self._event_ch.send_nowait(llm.ChatChunk(id="fake", delta=delta))


class Step(Agent):
    def __init__(self, *, resuming: bool, chat_ctx: llm.ChatContext | None = None) -> None:
        super().__init__(instructions="a", chat_ctx=chat_ctx)
        self.resuming = resuming
        self.entered = asyncio.Event()

    async def on_enter(self) -> None:
        self.entered.set()
        if not self.resuming:
            self.session.generate_reply(instructions="ask a")


async def long_lived() -> llm.ChatContext:
    agent = Step(resuming=False)
    session = AgentSession(llm=FakeLLM())
    try:
        result = await asyncio.wait_for(session.start(agent, capture_run=True), TIMEOUT)
        await asyncio.wait_for(result, TIMEOUT)  # the question has been asked
        return agent.chat_ctx.copy()
    finally:
        await asyncio.wait_for(session.aclose(), TIMEOUT)


async def cold_start(stored: dict[str, Any]) -> llm.ChatContext:
    agent = Step(resuming=True, chat_ctx=llm.ChatContext.from_dict(stored))
    session = AgentSession(llm=FakeLLM())
    try:
        await asyncio.wait_for(session.start(agent), TIMEOUT)
        await asyncio.wait_for(agent.entered.wait(), TIMEOUT)
        return agent.chat_ctx.copy()
    finally:
        await asyncio.wait_for(session.aclose(), TIMEOUT)


def kinds(ctx: llm.ChatContext) -> list[str]:
    return [f"{i.type}/{i.role}" if i.type == "message" else i.type for i in ctx.items]


def anthropic_request(ctx: llm.ChatContext) -> tuple[list[dict], list[str] | None]:
    messages, data = ctx.to_provider_format("anthropic")
    return messages, data.system_messages


async def main() -> int:
    print(f"livekit-agents {importlib.metadata.version('livekit-agents')}")
    original = await long_lived()
    print(f"long-lived session      : {kinds(original)}")

    keep_1 = await cold_start(original.to_dict(exclude_timestamp=False))
    keep_2 = await cold_start(keep_1.to_dict(exclude_timestamp=False))
    drop_1 = await cold_start(
        original.to_dict(exclude_timestamp=False, exclude_config_update=True)
    )
    print(f"cold start, keep        : {kinds(keep_1)}")
    print(f"2nd cold start, keep    : {kinds(keep_2)}")
    print(f"cold start, drop        : {kinds(drop_1)}")

    restored = (keep_1, keep_2, drop_1)
    none_match = all(kinds(ctx) != kinds(original) for ctx in restored)
    keep_grows = (
        kinds(keep_1) == [*kinds(original), "agent_config_update"]
        and kinds(keep_2) == [*kinds(keep_1), "agent_config_update"]
    )
    same_request = all(anthropic_request(ctx) == anthropic_request(original) for ctx in restored)
    print(f"Anthropic request identical for all four: {same_request}")
    if none_match and keep_grows:
        print(
            "REPRODUCED: after a cold start the restored context never has the long-lived "
            "item sequence (a new agent_config_update is appended at activation); keeping "
            "the stored ones adds one more per cold start"
        )
        return 1
    print("NOT REPRODUCED: a stored context comes back with the long-lived item sequence")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
