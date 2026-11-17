"""Text-only agent worker for the text-channel spike.

Registers as agent "text-spike" (explicit dispatch only) on the local dev server and runs
a Reception agent with RoomIO text input/output and no audio. The dispatch metadata is a
`common.Cfg`. A control topic lets the driver dump the session's state or close it.

Run (from the repo root, with `livekit-server --dev` up):
    uv run python spikes/text_channel/worker.py start
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import (  # noqa: E402
    AGENT_NAME,
    API_KEY,
    API_SECRET,
    TOPIC_CTL,
    TOPIC_DUMP,
    URL,
    Cfg,
    FakeLLM,
    Reception,
)
from livekit import rtc  # noqa: E402
from livekit.agents import (  # noqa: E402
    AgentServer,
    AgentSession,
    JobContext,
    JobExecutorType,
    cli,
)
from livekit.agents.voice.room_io import RoomOptions, TextOutputOptions  # noqa: E402


class _ThreadLogCapture(logging.Handler):
    """Collect WARNING+ records from this job's thread (jobs run as threads here)."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self._thread = threading.get_ident()
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread != self._thread:
            return
        msg = record.getMessage().splitlines()[0][:200]
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            msg += f" [{type(exc).__name__}: {str(exc)[:160]}]"
        self.records.append(f"{record.levelname} {record.name}: {msg}")


def _item_row(item: Any) -> list[Any]:
    if item.type == "message":
        return ["msg", item.role, item.text_content, bool(getattr(item, "interrupted", False))]
    if item.type == "function_call":
        return ["call", item.name, item.arguments]
    if item.type == "function_call_output":
        return ["out", item.name, item.output, item.is_error]
    if item.type == "agent_handoff":
        return ["handoff", item.old_agent_id, item.new_agent_id]
    return [item.type]


class Probe:
    """Records what the session did and answers the driver's control commands."""

    def __init__(self, ctx: JobContext, session: AgentSession, fake: FakeLLM) -> None:
        self._ctx, self._session, self._fake = ctx, session, fake
        self._t0 = time.monotonic()
        self.events: list[list[Any]] = []
        self.closed: str | None = None
        self.logs = _ThreadLogCapture()
        logging.getLogger("livekit").addHandler(self.logs)
        ctx.room.register_text_stream_handler(TOPIC_CTL, self._on_ctl)

        session.on("conversation_item_added", self._on_item)
        session.on("user_state_changed", lambda ev: self._ev("user_state", ev.new_state))
        session.on("agent_state_changed", lambda ev: self._ev("agent_state", ev.new_state))
        session.on("close", self._on_close)
        ctx.room.on(
            "participant_disconnected",
            lambda p: self._ev(
                "participant_disconnected",
                p.identity,
                rtc.DisconnectReason.Name(p.disconnect_reason or 0),
            ),
        )
        ctx.room.on("participant_connected", lambda p: self._ev("participant_connected", p.identity))

    def _t(self) -> float:
        return round(time.monotonic() - self._t0, 3)

    def _ev(self, *row: Any) -> None:
        self.events.append([self._t(), *row])

    def _on_item(self, ev: Any) -> None:
        self._ev("item", *_item_row(ev.item))

    def _on_close(self, ev: Any) -> None:
        self.closed = ev.reason.value if hasattr(ev.reason, "value") else str(ev.reason)
        self._ev("close", self.closed)

    def snapshot(self) -> dict[str, Any]:
        s = self._session
        try:
            agent = type(s.current_agent).__name__
        except Exception as e:  # session closed
            agent = f"<{type(e).__name__}>"
        return {
            "agent": agent,
            "history": [_item_row(i) for i in s.history.items],
            "events": self.events,
            "llm_calls": self._fake.calls,
            "logs": self.logs.records,
            "closed": self.closed,
            "linked": (
                s._room_io.linked_participant.identity
                if s._room_io and s._room_io.linked_participant
                else None
            ),
        }

    def _on_ctl(self, reader: rtc.TextStreamReader, sender: str) -> None:
        async def run() -> None:
            cmd = json.loads(await reader.read_all())
            op = cmd["op"]
            if op == "close":
                await self._session.aclose()
            elif op == "drain":
                self._session.shutdown(drain=True)
            elif op == "close_soon":
                asyncio.get_running_loop().call_later(
                    cmd.get("delay", 0.0),
                    lambda: asyncio.ensure_future(self._session.aclose()),
                )
            elif op == "say_locked":
                # a speech the user cannot interrupt, like a disclaimer
                self._session.generate_reply(instructions="greet-slow", allow_interruptions=False)
            # every command answers with a dump, so the driver can sequence on it
            await self._ctx.room.local_participant.send_text(
                json.dumps({"op": op, **self.snapshot()}),
                topic=TOPIC_DUMP,
                destination_identities=[sender],
            )

        asyncio.ensure_future(run())


server = AgentServer(
    job_executor_type=JobExecutorType.THREAD,
    ws_url=URL,
    api_key=API_KEY,
    api_secret=API_SECRET,
)


@server.rtc_session(agent_name=AGENT_NAME)
async def entry(ctx: JobContext) -> None:
    cfg = Cfg.from_json(ctx.job.metadata)
    fake = FakeLLM()
    session = AgentSession(llm=fake, user_away_timeout=cfg.user_away_timeout)
    Probe(ctx, session, fake)
    if cfg.connect_first:
        await ctx.connect()
    if cfg.wait_participant:
        await ctx.wait_for_participant()
    await session.start(
        Reception(cfg),
        room=ctx.room,
        room_options=RoomOptions(
            audio_input=False,
            audio_output=False,
            text_output=TextOutputOptions(sync_transcription=False),
            close_on_disconnect=cfg.close_on_disconnect,
        ),
    )


if __name__ == "__main__":
    cli.run_app(server)
