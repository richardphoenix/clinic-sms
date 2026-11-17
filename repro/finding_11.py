"""FINDING-11 repro: a text message that arrives during an agent handoff is dropped.

A tool on agent Old returns `(New(), "moved")`. Until New's activity has started, the
session is between agents: Old's activity is draining (it still streams its reply to the
tool output) and New's activity exists but has not resumed scheduling. A text message
that lands in this window goes through RoomIO's default callback
(`TextInputOptions().text_input_cb`, livekit-agents 1.8.11 voice/room_io/types.py:46-49):
`session.interrupt()` cuts Old's reply, then `session.generate_reply()` picks the next
activity because the current one is paused (voice/agent_session.py:1677), and that
activity refuses the speech, still paused since construction
(voice/agent_activity.py:357, 2036-2040). RoomIO logs the RuntimeError as a WARNING and
drops the message (voice/room_io/room_io.py:533-543): it never reaches the chat context
and is never answered. The window lasts as long as Old's reply after the tool, so with a
real LLM it is the whole of that reply.

  in window   "m2" is delivered d seconds after the tool ran, while Old's 2 s reply streams.
  control     "m2" is delivered after New's greeting, once the handoff is over.

`deliver()` below is RoomIO's `_read_text` without the room (same call, same except).
The spike in spikes/text_channel/ shows the same drop end to end through a real
livekit-server room (scenarios "handoff" and "handoff_window").

Self-contained: livekit-agents only, an inline fake LLM, no API key, no network.
Run: uv run python repro/finding_11.py
Exits 1 while the finding reproduces, 0 once it no longer does, 2 if the control fails.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import logging
from typing import Any

from livekit.agents import Agent, AgentSession, RunContext, function_tool, llm
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions
from livekit.agents.voice.room_io import TextInputEvent, TextInputOptions

TIMEOUT = 10.0
DELAYS = (0.1, 0.5, 1.0, 1.5)  # seconds after function_tools_executed


class FakeLLM(llm.LLM):
    """'go' calls the tool; the tool output gets a slow reply (8 x 0.25 s); New greets."""

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        **_: Any,
    ) -> llm.LLMStream:
        items = [i for i in chat_ctx.items if i.type in ("message", "function_call_output")]
        last = items[-1]
        plan: Any
        delay = 0.01
        if last.type == "function_call_output":
            plan, delay = [f"moving you, part {i}. " for i in range(8)], 0.25
        elif last.role == "user" and last.text_content == "go":
            plan = ("move",)
        elif last.role == "user":
            plan = [f"got {last.text_content}"]
        else:
            plan = ["hello from new"]
        return _Stream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options,
                       plan=plan, delay=delay)


class _Stream(llm.LLMStream):
    def __init__(self, *args: Any, plan: Any, delay: float, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self._plan, self._delay = plan, delay

    async def _run(self) -> None:
        if isinstance(self._plan, tuple):
            call = llm.FunctionToolCall(name=self._plan[0], arguments="{}", call_id="call_1")
            delta = llm.ChoiceDelta(role="assistant", tool_calls=[call])
            self._event_ch.send_nowait(llm.ChatChunk(id="fake", delta=delta))
            return
        for c in self._plan:
            await asyncio.sleep(self._delay)
            delta = llm.ChoiceDelta(role="assistant", content=c)
            self._event_ch.send_nowait(llm.ChatChunk(id="fake", delta=delta))


class New(Agent):
    def __init__(self) -> None:
        super().__init__(instructions="new")

    async def on_enter(self) -> None:
        self.session.generate_reply(instructions="greet")


class Old(Agent):
    def __init__(self) -> None:
        super().__init__(instructions="old")

    @function_tool
    async def move(self, ctx: RunContext) -> Any:
        """Move the user to the next agent."""
        return New(), "moved"


async def deliver(session: AgentSession, text: str) -> str | None:
    """RoomIO._read_text minus the room: returns the error RoomIO would log and swallow."""
    cb = TextInputOptions().text_input_cb  # the stock default callback
    try:
        result = cb(session, TextInputEvent(text=text))
        if asyncio.iscoroutine(result):
            await result
    except Exception as e:  # room_io.py:542-543 logs "failed to handle chat text stream"
        return f"{type(e).__name__}: {e}"
    return None


async def case(delay: float | None) -> dict[str, Any]:
    """delay: seconds after the tool ran; None = after New's greeting (control)."""
    session = AgentSession(llm=FakeLLM())
    tools_ran = asyncio.Event()
    greeted = asyncio.Event()
    session.on("function_tools_executed", lambda _: tools_ran.set())
    session.on(
        "conversation_item_added",
        lambda ev: greeted.set() if ev.item.type == "message"
        and ev.item.text_content == "hello from new" else None,
    )
    await session.start(Old())
    try:
        assert await deliver(session, "go") is None
        await asyncio.wait_for(tools_ran.wait(), TIMEOUT)
        if delay is None:
            await asyncio.wait_for(greeted.wait(), TIMEOUT)
        else:
            await asyncio.sleep(delay)
        dropped = await deliver(session, "m2")
        await asyncio.sleep(2.5 if delay is not None else 0.3)
        msgs = [(i.role, i.text_content) for i in session.history.items if i.type == "message"]
        agent = type(session.current_agent).__name__
    finally:
        await session.aclose()
    return {
        "dropped_with": dropped,
        "m2_in_history": ("user", "m2") in msgs,
        "m2_answered": ("assistant", "got m2") in msgs,
        "agent_after": agent,
    }


async def main() -> int:
    logging.getLogger("livekit.agents").setLevel(logging.CRITICAL)  # the SDK's own ERROR too
    print(f"livekit-agents {importlib.metadata.version('livekit-agents')}")
    lost = 0
    for d in DELAYS:
        r = await case(d)
        lost += not r["m2_in_history"]
        print(f"in window, d={d:<4}: {r}")
    control = await case(None)
    print(f"control (after greeting): {control}")
    if not (control["m2_in_history"] and control["m2_answered"] and not control["dropped_with"]):
        print("INCONCLUSIVE: the control did not behave as expected")
        return 2
    if lost:
        print(
            f"REPRODUCED: {lost}/{len(DELAYS)} messages sent during the handoff were dropped "
            "(generate_reply raised; never in the chat context, never answered)"
        )
        return 1
    print("NOT REPRODUCED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
