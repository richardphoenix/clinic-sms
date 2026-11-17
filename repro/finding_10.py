"""FINDING-10 repro: a text message that arrives while the agent says something it marked
allow_interruptions=False is dropped.

RoomIO hands every `lk.chat` text stream to the text input callback in its own task and,
if the callback raises, logs a WARNING and drops the message
(livekit-agents 1.8.11, voice/room_io/room_io.py:533-543). The default callback,
`TextInputOptions().text_input_cb` (voice/room_io/types.py:46-49), first awaits
`session.interrupt()`, which raises RuntimeError when the current speech disallows
interruptions (voice/agent_activity.py:1963-1964, voice/speech_handle.py:222-223). The
`generate_reply()` after it never runs, so the message never reaches the chat context and
is never answered.

  locked      the agent is speaking with allow_interruptions=False; the user sends "m1".
  control     the same, with allow_interruptions=True.

`deliver()` below is RoomIO's `_read_text` without the room (same call, same except).
The spike in spikes/text_channel/ shows the same drop end to end through a real
livekit-server room (scenario "locked").

Self-contained: livekit-agents only, an inline fake LLM, no API key, no network.
Run: uv run python repro/finding_10.py
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

TIMEOUT = 10.0


class FakeLLM(llm.LLM):
    """A slow 'notice' (5 x 0.2 s) for the instructions-only turn; 'got <text>' otherwise."""

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        **_: Any,
    ) -> llm.LLMStream:
        last = [i for i in chat_ctx.items if i.type == "message"][-1]
        if last.role == "user":
            chunks, delay = [f"got {last.text_content}"], 0.01
        else:
            chunks, delay = [f"notice {i}. " for i in range(5)], 0.2
        return _Stream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options,
                       chunks=chunks, delay=delay)


class _Stream(llm.LLMStream):
    def __init__(self, *args: Any, chunks: list[str], delay: float, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self._chunks, self._delay = chunks, delay

    async def _run(self) -> None:
        for c in self._chunks:
            await asyncio.sleep(self._delay)
            self._event_ch.send_nowait(
                llm.ChatChunk(id="fake", delta=llm.ChoiceDelta(role="assistant", content=c))
            )


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


async def case(*, interruptible: bool) -> dict[str, Any]:
    session = AgentSession(llm=FakeLLM())
    await session.start(Agent(instructions="clinic"))
    try:
        notice = session.generate_reply(
            instructions="read the notice", allow_interruptions=interruptible
        )
        await asyncio.sleep(0.3)  # the notice is now streaming
        dropped = await deliver(session, "m1")
        await asyncio.wait_for(notice.wait_for_playout(), TIMEOUT)
        await asyncio.sleep(0.3)  # let any reply to m1 finish
        msgs = [(i.role, i.text_content) for i in session.history.items if i.type == "message"]
    finally:
        await session.aclose()
    return {
        "dropped_with": dropped,
        "m1_in_history": ("user", "m1") in msgs,
        "m1_answered": ("assistant", "got m1") in msgs,
    }


async def main() -> int:
    logging.getLogger("livekit.agents").setLevel(logging.CRITICAL)
    print(f"livekit-agents {importlib.metadata.version('livekit-agents')}")
    locked = await case(interruptible=False)
    control = await case(interruptible=True)
    print(f"locked   (allow_interruptions=False): {locked}")
    print(f"control  (allow_interruptions=True) : {control}")
    if not (control["m1_in_history"] and control["m1_answered"] and not control["dropped_with"]):
        print("INCONCLUSIVE: the control did not behave as expected")
        return 2
    if locked["dropped_with"] and not locked["m1_in_history"] and not locked["m1_answered"]:
        print(
            "REPRODUCED: the default text input callback raised from session.interrupt(); "
            "RoomIO would log a WARNING and drop 'm1' (never in the chat context, never answered)"
        )
        return 1
    print("NOT REPRODUCED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
