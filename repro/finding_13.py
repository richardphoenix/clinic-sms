"""FINDING-13 repro: in a text-only room session, what the agent says before a participant
is linked (e.g. the on_enter greeting) is never sent, yet the chat history records it as
said.

With audio output disabled, RoomIO's agent text output is `_ParticipantTranscriptionOutput`
with no audio to pace it (livekit-agents 1.8.11, voice/room_io/room_io.py:157-184). Its
identity is set only once a participant is linked (room_io.py:390-392), and until then
`capture_text` returns at once (voice/room_io/_output.py:789-791); nothing waits, nothing
is logged, and the speech completes as if it had been delivered. The audio path waits
instead: `_ParticipantAudioOutput.capture_frame` awaits the track's first subscription
(_output.py:143-144). Even once linked, the stream goes to whoever is in the room at that
moment, so text produced while the user is away is gone too.

  late join   the agent starts and greets; the user joins 1 s later, then sends "m1".
  control     the user is in the room before the agent starts.

Unlike the other repros this one needs a LiveKit server: run `livekit-server --dev` (it
listens on ws://127.0.0.1:7880 with the dev key/secret). The agent runs in-process with
AgentSession + RoomIO (no worker, no job), and a second rtc.Room plays the user.

Self-contained otherwise: livekit-agents only, an inline fake LLM, no API key.
Run: uv run python repro/finding_13.py
Exits 1 while the finding reproduces, 0 once it no longer does, 2 if the control fails or
the server is not reachable.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import logging
import os
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
        text = f"got {last.text_content}" if last.role == "user" else "hello"
        return _Stream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options,
                       text=text)


class _Stream(llm.LLMStream):
    def __init__(self, *args: Any, text: str, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self._text = text

    async def _run(self) -> None:
        await asyncio.sleep(0.02)
        delta = llm.ChoiceDelta(role="assistant", content=self._text)
        self._event_ch.send_nowait(llm.ChatChunk(id="fake", delta=delta))


class Greeter(Agent):
    def __init__(self) -> None:
        super().__init__(instructions="clinic")

    async def on_enter(self) -> None:
        self.session.generate_reply(instructions="greet the user")


def token(room: str, identity: str) -> str:
    grants = api.VideoGrants(room_join=True, room=room)
    return api.AccessToken(KEY, SECRET).with_identity(identity).with_grants(grants).to_jwt()


async def join_user(room: str, received: list[str]) -> rtc.Room:
    user = rtc.Room()

    def on_tr(reader: rtc.TextStreamReader, sender: str) -> None:
        async def read() -> None:
            received.append(await reader.read_all())

        asyncio.ensure_future(read())

    user.register_text_stream_handler("lk.transcription", on_tr)
    await user.connect(URL, token(room, "user"))
    return user


async def until(pred: Any, timeout: float = 10.0) -> None:
    # the dev server announces a standard participant to the others ~3 s after it joins
    for _ in range(int(timeout / 0.05)):
        if pred():
            return
        await asyncio.sleep(0.05)
    raise TimeoutError


async def case(*, user_first: bool) -> dict[str, Any]:
    room = f"f13-{uuid.uuid4().hex[:6]}"
    received: list[str] = []
    linked_at_greeting: list[str | None] = []
    agent_room = rtc.Room()
    await agent_room.connect(URL, token(room, "agent"))
    user = None
    if user_first:
        user = await join_user(room, received)
        await until(lambda: "user" in agent_room.remote_participants)

    session = AgentSession(llm=FakeLLM())

    def on_item(ev: Any) -> None:
        if ev.item.type == "message" and ev.item.text_content == "hello":
            p = session.room_io.linked_participant
            linked_at_greeting.append(p.identity if p else None)

    session.on("conversation_item_added", on_item)
    try:
        await session.start(
            Greeter(),
            room=agent_room,
            room_options=RoomOptions(
                audio_input=False,
                audio_output=False,
                text_output=TextOutputOptions(sync_transcription=False),
            ),
        )
        if user is None:
            await asyncio.sleep(1.0)
            user = await join_user(room, received)
        # send only once the agent has linked the user and both see each other
        await until(lambda: session.room_io.linked_participant is not None)
        await until(lambda: "agent" in user.remote_participants)
        await asyncio.sleep(0.5)
        await user.local_participant.send_text("m1", topic="lk.chat")
        for _ in range(40):
            if "got m1" in received:
                break
            await asyncio.sleep(0.05)
        said = [
            (i.text_content, i.interrupted)
            for i in session.history.items
            if i.type == "message" and i.role == "assistant"
        ]
    finally:
        await session.aclose()
        if user is not None:
            await user.disconnect()
        await agent_room.disconnect()
    return {
        "user_received": received,
        "history_says_said": said,
        "linked_participant_at_greeting": linked_at_greeting,
    }


async def main() -> int:
    logging.getLogger("livekit.agents").setLevel(logging.CRITICAL)
    print(f"livekit-agents {importlib.metadata.version('livekit-agents')}")
    try:
        probe = rtc.Room()
        await asyncio.wait_for(probe.connect(URL, token("f13-probe", "probe")), 5)
        await probe.disconnect()
    except Exception as e:
        print(f"no LiveKit server at {URL} ({type(e).__name__}); run `livekit-server --dev`")
        return 2
    late = await case(user_first=False)
    control = await case(user_first=True)
    print(f"late join: {late}")
    print(f"control  : {control}")
    if control["user_received"][:1] != ["hello"]:
        print("INCONCLUSIVE: the control did not behave as expected")
        return 2
    if (
        "hello" not in late["user_received"]
        and ("hello", False) in late["history_says_said"]
        and "got m1" in late["user_received"]
    ):
        print(
            "REPRODUCED: the greeting was produced before a participant was linked; it never "
            "reached the user, yet the history records it as said (not interrupted)"
        )
        return 1
    print("NOT REPRODUCED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
