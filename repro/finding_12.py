"""FINDING-12 repro: a burst of text messages gets one reply per message, and each reply
is generated without the other messages of the burst.

RoomIO runs the text input callback for each `lk.chat` stream in its own task
(livekit-agents 1.8.11, voice/room_io/room_io.py:533-547). The default callback
(`TextInputOptions().text_input_cb`, voice/room_io/types.py:46-49) is
`await session.interrupt()` then `session.generate_reply(user_input=text)`, and the
"user turn claim" around it is a counter, not a lock (voice/agent_session.py:1742-1766).
When messages arrive together, every callback's interrupt() runs before any of the
others' generate_reply(), so nothing interrupts the replies of the later messages; and
each generate_reply() builds its request from the agent's chat context at that moment
plus its own message (voice/agent_activity.py:1894-1900), so a reply sees neither the
other messages of the burst nor the replies to them. The user messages are committed as
their replies run, so their order in the chat context need not be the send order.

  burst       "m1", "m2", "m3" delivered at once, the way RoomIO does for streams that
              arrive within a few ms (one task each).
  control     the same three, each after the previous reply has finished.

The spike in spikes/text_channel/ shows the same end to end through a real
livekit-server room (scenario "rapid": 5 texts sent back to back, 4 replies, each LLM
call seeing only the first text and its own).

Self-contained: livekit-agents only, an inline fake LLM, no API key, no network.
Run: uv run python repro/finding_12.py
Exits 1 while the finding reproduces, 0 once it no longer does, 2 if the control fails.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import logging
from typing import Any

from livekit.agents import Agent, AgentSession, llm
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions
from livekit.agents.voice.room_io import TextInputEvent, TextInputOptions

MSGS = ("m1", "m2", "m3")


class FakeLLM(llm.LLM):
    """Answers 'got <last user text>' and records the user texts each call was shown."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[tuple[str, list[str]]] = []

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        **_: Any,
    ) -> llm.LLMStream:
        users = [i.text_content for i in chat_ctx.items if i.type == "message" and i.role == "user"]
        self.seen.append((users[-1], users))
        return _Stream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options,
                       text=f"got {users[-1]}")


class _Stream(llm.LLMStream):
    def __init__(self, *args: Any, text: str, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self._text = text

    async def _run(self) -> None:
        await asyncio.sleep(0.05)
        delta = llm.ChoiceDelta(role="assistant", content=self._text)
        self._event_ch.send_nowait(llm.ChatChunk(id="fake", delta=delta))


async def deliver(session: AgentSession, text: str) -> None:
    """RoomIO._read_text minus the room (its except only logs, nothing raises here)."""
    result = TextInputOptions().text_input_cb(session, TextInputEvent(text=text))
    if asyncio.iscoroutine(result):
        await result


async def case(*, burst: bool) -> dict[str, Any]:
    fake = FakeLLM()
    session = AgentSession(llm=fake)
    await session.start(Agent(instructions="clinic"))
    try:
        if burst:
            await asyncio.gather(*(deliver(session, m) for m in MSGS))  # one task per stream
        else:
            for m in MSGS:
                await deliver(session, m)
                await asyncio.sleep(0.3)  # the reply to m has finished
        await asyncio.sleep(0.5)
        msgs = [i for i in session.history.items if i.type == "message"]
        replies = [i.text_content for i in msgs if i.role == "assistant" and not i.interrupted]
        user_order = [i.text_content for i in msgs if i.role == "user"]
    finally:
        await session.aclose()
    # for each delivered reply: the user texts its LLM call was shown
    shown = {f"got {last}": users for last, users in fake.seen}
    return {
        "user_order_in_history": user_order,
        "replies": replies,
        "each_reply_saw": {r: shown.get(r) for r in replies},
    }


async def main() -> int:
    logging.getLogger("livekit.agents").setLevel(logging.CRITICAL)
    print(f"livekit-agents {importlib.metadata.version('livekit-agents')}")
    burst = await case(burst=True)
    control = await case(burst=False)
    print(f"burst  : {burst}")
    print(f"control: {control}")
    if control["each_reply_saw"].get("got m3") != list(MSGS):
        print("INCONCLUSIVE: the control did not behave as expected")
        return 2
    partial = [r for r, saw in burst["each_reply_saw"].items() if saw != list(MSGS)]
    reordered = burst["user_order_in_history"] != list(MSGS)
    if (len(burst["replies"]) > 1 and partial) or reordered:
        print(
            f"REPRODUCED: the burst got {len(burst['replies'])} separate replies, "
            f"{len(partial)} of them generated without the rest of the burst {partial}; "
            f"user texts in the chat context in send order: {not reordered}"
        )
        return 1
    print("NOT REPRODUCED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
