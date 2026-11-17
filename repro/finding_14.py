"""FINDING-14 repro: a text a user sends right after joining a room where the agent is
already waiting is dropped, because the agent has not yet been told about that user.

RoomIO's `lk.chat` handler looks the sender up in `room.remote_participants` and, if it
is not there, logs WARNING "participant not found, ignoring text input" and drops the
message (livekit-agents 1.8.11, voice/room_io/room_io.py:521-524), even though no
participant is linked yet (so any sender would be accepted, lines 516-519) and
`TextInputEvent.participant` is optional (voice/room_io/types.py:24-28). The text stream
can arrive before the participant update: on livekit-server 1.13.11 in dev mode the agent
learned of a newly joined standard participant about 3 s after it joined, while the
user's text reached the agent's handler at once.

  early       the user sends "m0" 0.2 s and 1.0 s after joining.
  control     the user sends "m0" once the agent has linked them.

Needs a LiveKit server: run `livekit-server --dev` (ws://127.0.0.1:7880, dev key/secret).
The agent runs in-process with AgentSession + RoomIO (no worker, no job).

Self-contained otherwise: livekit-agents only, an inline fake LLM, no API key.
Run: uv run python repro/finding_14.py
Exits 1 while the finding reproduces, 0 once it no longer does, 2 if the control fails or
the server is not reachable.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import logging
import os
import time
import uuid
from typing import Any

from livekit import api, rtc
from livekit.agents import Agent, AgentSession, llm
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions
from livekit.agents.voice.room_io import RoomOptions, TextOutputOptions

URL = os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880")
KEY, SECRET = "devkey", "secret"  # livekit-server --dev placeholders


class FakeLLM(llm.LLM):
    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        **_: Any,
    ) -> llm.LLMStream:
        last = [i for i in chat_ctx.items if i.type == "message"][-1]
        return _Stream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options,
                       text=f"got {last.text_content}")


class _Stream(llm.LLMStream):
    def __init__(self, *args: Any, text: str, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self._text = text

    async def _run(self) -> None:
        await asyncio.sleep(0.02)
        delta = llm.ChoiceDelta(role="assistant", content=self._text)
        self._event_ch.send_nowait(llm.ChatChunk(id="fake", delta=delta))


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.msgs: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.msgs.append(record.getMessage()[:80])


def token(room: str, identity: str) -> str:
    grants = api.VideoGrants(room_join=True, room=room)
    return api.AccessToken(KEY, SECRET).with_identity(identity).with_grants(grants).to_jwt()


async def case(send_after: float | None) -> dict[str, Any]:
    """send_after: seconds after the user joined; None = once the agent linked the user."""
    room = f"f14-{uuid.uuid4().hex[:6]}"
    received: list[str] = []
    cap = Capture()
    sdk_log = logging.getLogger("livekit.agents")
    sdk_log.addHandler(cap)

    agent_room = rtc.Room()
    await agent_room.connect(URL, token(room, "agent"))
    session = AgentSession(llm=FakeLLM())
    await session.start(
        Agent(instructions="clinic"),
        room=agent_room,
        room_options=RoomOptions(
            audio_input=False,
            audio_output=False,
            text_output=TextOutputOptions(sync_transcription=False),
        ),
    )

    user = rtc.Room()

    def on_tr(reader: rtc.TextStreamReader, sender: str) -> None:
        async def read() -> None:
            received.append(await reader.read_all())

        asyncio.ensure_future(read())

    user.register_text_stream_handler("lk.transcription", on_tr)
    linked_after: float | None = None
    try:
        await user.connect(URL, token(room, "user"))
        t0 = time.monotonic()
        if send_after is None:
            while session.room_io.linked_participant is None and time.monotonic() - t0 < 10:
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.3)
        else:
            await asyncio.sleep(send_after)
        await user.local_participant.send_text("m0", topic="lk.chat")
        while time.monotonic() - t0 < 8 and "got m0" not in received:
            if linked_after is None and session.room_io.linked_participant is not None:
                linked_after = round(time.monotonic() - t0, 2)
            await asyncio.sleep(0.05)
        users = [
            i.text_content
            for i in session.history.items
            if i.type == "message" and i.role == "user"
        ]
    finally:
        await session.aclose()
        await user.disconnect()
        await agent_room.disconnect()
        sdk_log.removeHandler(cap)
    return {
        "agent_linked_user_after_s": linked_after,
        "m0_in_history": "m0" in users,
        "m0_answered": "got m0" in received,
        "warnings": cap.msgs,
    }


async def main() -> int:
    logging.getLogger("livekit.agents").setLevel(logging.WARNING)
    logging.getLogger("livekit.agents").propagate = False  # keep stderr quiet
    print(f"livekit-agents {importlib.metadata.version('livekit-agents')}")
    try:
        probe = rtc.Room()
        await asyncio.wait_for(probe.connect(URL, token("f14-probe", "probe")), 5)
        await probe.disconnect()
    except Exception as e:
        print(f"no LiveKit server at {URL} ({type(e).__name__}); run `livekit-server --dev`")
        return 2
    early = {d: await case(d) for d in (0.2, 1.0)}
    control = await case(None)
    for d, r in early.items():
        print(f"early, sent {d} s after joining: {r}")
    print(f"control, sent once linked      : {control}")
    if not (control["m0_in_history"] and control["m0_answered"]):
        print("INCONCLUSIVE: the control did not behave as expected")
        return 2
    lost = [d for d, r in early.items() if not r["m0_in_history"]]
    if lost:
        print(
            f"REPRODUCED: the text sent {lost} s after joining was dropped "
            "('participant not found, ignoring text input'); never in the chat context"
        )
        return 1
    print("NOT REPRODUCED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
