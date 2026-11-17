"""Text participant(s) for the text-channel spike: runs each scenario in a fresh room.

Each client joins with the livekit rtc Python SDK, sends on `lk.chat`, and reads the
agent's `lk.transcription` text streams chunk by chunk. After each scenario it asks the
worker for a dump of the session (history, events, WARNING+ logs).

Run (from the repo root, with `livekit-server --dev` and worker.py up):
    uv run python spikes/text_channel/driver.py            # all scenarios
    uv run python spikes/text_channel/driver.py rapid ...  # some of them
Results also go to var/spikes/text-channel/<scenario>.json (git-ignored).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import (  # noqa: E402
    AGENT_NAME,
    API_KEY,
    API_SECRET,
    TOPIC_CHAT,
    TOPIC_CTL,
    TOPIC_DUMP,
    TOPIC_TRANSCRIPTION,
    URL,
    Cfg,
    participant_token,
)
from livekit import api, rtc  # noqa: E402

HTTP_URL = URL.replace("ws://", "http://")
OUT = Path("var/spikes/text-channel")


@dataclass
class Seg:
    sender: str
    stream_id: str
    t_open: float
    text: str = ""
    t_end: float | None = None
    final: str | None = None
    error: str | None = None
    chunks: int = 0


@dataclass
class Client:
    identity: str
    t0: float
    room: rtc.Room = field(default_factory=rtc.Room)
    segs: list[Seg] = field(default_factory=list)
    sent: list[tuple[float, str]] = field(default_factory=list)
    dumps: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)

    def t(self) -> float:
        return round(time.monotonic() - self.t0, 3)

    def _register(self) -> None:
        self.room.register_text_stream_handler(TOPIC_TRANSCRIPTION, self._on_tr)
        self.room.register_text_stream_handler(TOPIC_DUMP, self._on_dump)

    async def join(self, room_name: str, cfg: Cfg | None) -> None:
        self.room = rtc.Room()
        self._register()
        await self.room.connect(URL, participant_token(room_name, self.identity, cfg))

    async def leave(self) -> None:
        await self.room.disconnect()

    def _on_tr(self, reader: rtc.TextStreamReader, sender: str) -> None:
        seg = Seg(sender=sender, stream_id=reader.info.stream_id, t_open=self.t())
        self.segs.append(seg)

        async def read() -> None:
            try:
                async for chunk in reader:
                    seg.text += chunk
                    seg.chunks += 1
            except Exception as e:  # StreamError: aborted / room gone mid-stream
                seg.error = f"{type(e).__name__}: {e}"
            seg.t_end = self.t()
            seg.final = (reader.info.attributes or {}).get("lk.transcription_final")

        asyncio.ensure_future(read())

    def _on_dump(self, reader: rtc.TextStreamReader, sender: str) -> None:
        async def read() -> None:
            await self.dumps.put(json.loads(await reader.read_all()))

        asyncio.ensure_future(read())

    async def send(self, text: str) -> None:
        self.sent.append((self.t(), text))
        await self.room.local_participant.send_text(text, topic=TOPIC_CHAT)

    def agent_identity(self) -> str | None:
        for p in self.room.remote_participants.values():
            if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
                return p.identity
        return None

    async def wait_agent(self, timeout: float = 10.0) -> str:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if ident := self.agent_identity():
                return ident
            await asyncio.sleep(0.05)
        raise TimeoutError("agent never joined")

    async def ctl(self, op: str, timeout: float = 10.0, **kw: Any) -> dict[str, Any]:
        agent = await self.wait_agent()
        await self.room.local_participant.send_text(
            json.dumps({"op": op, **kw}), topic=TOPIC_CTL, destination_identities=[agent]
        )
        return await asyncio.wait_for(self.dumps.get(), timeout)

    async def wait_text(self, needle: str, timeout: float = 5.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if any(needle in s.text and s.t_end is not None for s in self.segs):
                return True
            await asyncio.sleep(0.05)
        return False

    def agent_segs(self) -> list[dict[str, Any]]:
        return [
            {
                "t": [s.t_open, s.t_end],
                "text": s.text,
                "final": s.final,
                "chunks": s.chunks,
                **({"error": s.error} if s.error else {}),
            }
            for s in self.segs
            if s.sender.startswith("agent-")
        ]


def new_room(tag: str) -> str:
    return f"ts-{tag}-{uuid.uuid4().hex[:6]}"


def summarize(c: Client, dump: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {"sent": c.sent, "received": c.agent_segs()}
    if dump is not None:
        out["agent_side"] = {
            k: dump[k] for k in ("agent", "linked", "closed", "llm_calls", "logs", "history")
        }
        out["agent_events"] = [e for e in dump["events"] if e[1] != "agent_state"]
    return out


# --- scenarios ------------------------------------------------------------------------


async def _session(tag: str, cfg: Cfg | None = None, identity: str = "user-a") -> Client:
    """Join a fresh room (dispatching the agent) and wait for the greeting."""
    c = Client(identity=identity, t0=time.monotonic())
    await c.join(new_room(tag), cfg or Cfg())
    if (cfg or Cfg()).greet:
        await c.wait_text("hello", timeout=10)
    return c


async def sc_greet() -> dict[str, Any]:
    """Does the participant receive on_enter's proactive message?"""
    res: dict[str, Any] = {}
    for label, cfg in [
        ("connect_first", Cfg(connect_first=True)),
        ("start_then_autoconnect", Cfg(connect_first=False)),
    ]:
        runs = []
        for _ in range(3):
            c = Client(identity="user-a", t0=time.monotonic())
            await c.join(new_room("greet"), cfg)
            got = await c.wait_text("hello", timeout=5)
            dump = await c.ctl("dump")
            runs.append(
                {
                    "greeting_received": got,
                    "assistant_in_history": [
                        h[2] for h in dump["history"] if h[:2] == ["msg", "assistant"]
                    ],
                    "logs": dump["logs"],
                }
            )
            await c.leave()
        res[label] = runs

    # agent dispatched into an empty room; the user joins 1.5 s after the agent
    for label, cfg in [
        ("agent_first_default", Cfg()),
        ("agent_first_wait_for_participant", Cfg(wait_participant=True)),
    ]:
        room_name = new_room("greet-late")
        async with api.LiveKitAPI(HTTP_URL, API_KEY, API_SECRET) as lk:
            await lk.room.create_room(api.CreateRoomRequest(name=room_name, empty_timeout=60))
            await lk.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name=AGENT_NAME, room=room_name, metadata=cfg.to_json()
                )
            )
            for _ in range(100):
                ps = await lk.room.list_participants(api.ListParticipantsRequest(room=room_name))
                if ps.participants:
                    break
                await asyncio.sleep(0.1)
        await asyncio.sleep(1.5)
        c = Client(identity="user-a", t0=time.monotonic())
        await c.join(room_name, None)
        got = await c.wait_text("hello", timeout=4)
        await c.send("m1")
        await c.wait_text("got m1", timeout=4)
        dump = await c.ctl("dump")
        res[label] = {"greeting_received": got, **summarize(c, dump)}
        await c.leave()
    return res


async def sc_rapid() -> dict[str, Any]:
    """Five messages back to back, without waiting for replies."""
    c = await _session("rapid")
    for i in range(1, 6):
        await c.send(f"m{i}")
    await asyncio.sleep(3)
    dump = await c.ctl("dump")
    await c.leave()
    return summarize(c, dump)


async def sc_overlap() -> dict[str, Any]:
    """A new message while a (slow, ~2 s) reply is still streaming."""
    c = await _session("overlap")
    await c.send("slow")
    await asyncio.sleep(0.7)
    await c.send("m2")
    await asyncio.sleep(3)
    dump = await c.ctl("dump")
    await c.leave()
    return summarize(c, dump)


async def sc_handoff() -> dict[str, Any]:
    """A message sent d seconds after the one that triggers a handoff."""
    res: dict[str, Any] = {}
    for d in (0.0, 0.03, 0.06, 0.1, 0.15, 0.25, 0.5):
        c = await _session("handoff")
        await c.send("transfer")
        await asyncio.sleep(d)
        await c.send("m2")
        await asyncio.sleep(2)
        dump = await c.ctl("dump")
        await c.leave()
        s = summarize(c, dump)
        res[f"d={d}"] = {
            "received": [r["text"] for r in s["received"]],
            "m2_in_history": any(h[:3] == ["msg", "user", "m2"] for h in dump["history"]),
            "m2_answered": any("got m2" in r["text"] for r in s["received"]),
            "agent": dump["agent"],
            "logs": dump["logs"],
        }
    return res


async def sc_task() -> dict[str, Any]:
    """AgentTask and TaskGroup run inline from a tool; messages inside and at the edges."""
    res: dict[str, Any] = {}
    c = await _session("task")
    await c.send("intake")
    await c.wait_text("[intake] hello")
    await c.send("m2")
    await c.wait_text("[intake] got m2")
    await c.send("save x")
    await c.wait_text("after intake=x")
    await asyncio.sleep(0.5)
    res["agent_task"] = summarize(c, await c.ctl("dump"))
    await c.leave()

    c = await _session("group")
    await c.send("group")
    await c.wait_text("[step-a] hello")
    await c.send("save 1")
    await c.wait_text("[step-b] hello")
    await c.send("save 2")
    await c.wait_text("after group=")
    await asyncio.sleep(0.5)
    res["task_group"] = summarize(c, await c.ctl("dump"))
    await c.leave()

    # edges: a message right behind the one that starts / finishes a task
    for label, first, then_wait in [
        ("msg_right_after_start", "intake", None),
        ("msg_right_after_complete", "save x", "[intake] hello"),
        ("msg_right_after_step_switch", "save 1", "[step-a] hello"),
    ]:
        runs = []
        for d in (0.0, 0.1):
            c = await _session("task-edge")
            if then_wait == "[intake] hello":
                await c.send("intake")
                await c.wait_text(then_wait)
            elif then_wait == "[step-a] hello":
                await c.send("group")
                await c.wait_text(then_wait)
            await c.send(first)
            await asyncio.sleep(d)
            await c.send("m9")
            await asyncio.sleep(2)
            dump = await c.ctl("dump")
            await c.leave()
            s = summarize(c, dump)
            runs.append(
                {
                    "d": d,
                    "received": [r["text"] for r in s["received"]],
                    "m9_in_history": any(h[:3] == ["msg", "user", "m9"] for h in dump["history"]),
                    "agent": dump["agent"],
                    "logs": dump["logs"],
                }
            )
        res[label] = runs
    return res


async def sc_idle() -> dict[str, Any]:
    """Idle past user_away_timeout (15 s default), then send."""
    c = await _session("idle")
    await asyncio.sleep(20)
    await c.send("m1")
    await c.wait_text("got m1")
    await asyncio.sleep(0.5)
    dump = await c.ctl("dump")
    await c.leave()
    return summarize(c, dump)


async def sc_reconnect() -> dict[str, Any]:
    res: dict[str, Any] = {}
    # (a) default options: the user leaves (CLIENT_INITIATED) and comes back
    # (the dev server announces participant changes ~3 s late; rejoining sooner than that
    # is coalesced into "disconnected (UNKNOWN_REASON) + connected" on the agent side)
    for wait in (1.5, 5.0):
        c = await _session("reconnect-default")
        room_name = c.room.name
        await c.leave()
        await asyncio.sleep(wait)
        await c.join(room_name, None)
        await asyncio.sleep(1.0)
        agent = c.agent_identity()
        got = False
        if agent:
            await c.send("m1")
            got = await c.wait_text("got m1", timeout=3)
        res[f"default_leave_rejoin_after_{wait}s"] = {
            "agent_present_after_rejoin": agent,
            "m1_answered": got,
            **(summarize(c, await c.ctl("dump")) if agent else {}),
        }
        await c.leave()

    # (b) close_on_disconnect=False: leave mid-reply, rejoin, send again
    c = await _session("reconnect-keep", Cfg(close_on_disconnect=False))
    room_name = c.room.name
    await c.send("slow")
    await asyncio.sleep(0.6)
    await c.leave()
    await asyncio.sleep(1.5)
    await c.join(room_name, None)
    await asyncio.sleep(0.5)
    await c.send("m2")
    await c.wait_text("got m2", timeout=3)
    await asyncio.sleep(0.3)
    res["keep_leave_mid_reply_rejoin"] = summarize(c, await c.ctl("dump"))
    await c.leave()

    # (c) a transport-level full reconnect while a reply is streaming
    c = await _session("reconnect-net")
    await c.send("slow")
    await asyncio.sleep(0.6)
    await c.room.simulate_scenario(rtc.SimulateScenarioKind.SIMULATE_FULL_RECONNECT)
    await asyncio.sleep(4)
    await c.send("m3")
    await c.wait_text("got m3", timeout=3)
    await asyncio.sleep(0.3)
    res["full_reconnect_mid_reply"] = summarize(c, await c.ctl("dump"))
    await c.leave()
    return res


async def sc_multi() -> dict[str, Any]:
    """Two text participants in one room; only A dispatched the agent."""
    a = await _session("multi", identity="user-a")
    b = Client(identity="user-b", t0=a.t0)
    await b.join(a.room.name, None)
    await asyncio.sleep(0.5)
    await b.send("from-b")
    await asyncio.sleep(1.0)
    await a.send("from-a")
    await asyncio.sleep(1.0)
    dump = await a.ctl("dump")
    out = {"a": summarize(a, dump), "b": summarize(b, None)}
    await b.leave()
    await a.leave()
    return out


async def sc_close() -> dict[str, Any]:
    """Close the session while a reply is streaming: aclose() and shutdown(drain=True)."""
    res: dict[str, Any] = {}
    for op in ("close", "drain"):
        c = await _session(f"close-{op}")
        await c.send("slow")
        await asyncio.sleep(0.6)
        try:
            dump = await c.ctl(op, timeout=6)
        except (TimeoutError, asyncio.TimeoutError):
            dump = None
        await asyncio.sleep(3)
        res[op] = {**summarize(c, dump), "agent_still_in_room": c.agent_identity()}
        await c.leave()
    return res


async def sc_locked() -> dict[str, Any]:
    """A message while the agent speaks with allow_interruptions=False."""
    c = await _session("locked")
    await c.ctl("say_locked")
    await asyncio.sleep(0.4)
    await c.send("m1")
    await asyncio.sleep(3.5)
    await c.send("m2")
    await c.wait_text("got m2")
    await asyncio.sleep(0.3)
    dump = await c.ctl("dump")
    await c.leave()
    return summarize(c, dump)


async def sc_handoff_window() -> dict[str, Any]:
    """Like handoff, but the old agent's reply after the tool takes ~2 s; 3 runs per d."""
    res: dict[str, Any] = {}
    for slow in (False, True):
        for d in ((0.08, 0.1, 0.12) if not slow else (0.1, 0.5, 1.0, 1.5, 2.5)):
            lost = 0
            runs = 3
            detail = []
            for _ in range(runs):
                c = await _session("handoff-win", Cfg(slow_after=slow))
                await c.send("transfer")
                await asyncio.sleep(d)
                await c.send("m2")
                await asyncio.sleep(3.5 if slow else 1.5)
                dump = await c.ctl("dump")
                await c.leave()
                in_hist = any(h[:3] == ["msg", "user", "m2"] for h in dump["history"])
                lost += not in_hist
                detail.append([r["text"][:40] for r in c.agent_segs()] + dump["logs"][:1])
            res[f"slow_after={slow} d={d}"] = {"m2_lost": f"{lost}/{runs}", "runs": detail}
    return res


SCENARIOS = {
    "handoff_window": sc_handoff_window,
    "greet": sc_greet,
    "rapid": sc_rapid,
    "overlap": sc_overlap,
    "handoff": sc_handoff,
    "task": sc_task,
    "idle": sc_idle,
    "reconnect": sc_reconnect,
    "multi": sc_multi,
    "close": sc_close,
    "locked": sc_locked,
}


async def main(names: list[str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name in names or list(SCENARIOS):
        print(f"=== {name}", flush=True)
        try:
            res = await SCENARIOS[name]()
        except Exception as e:
            res = {"driver_error": f"{type(e).__name__}: {e}"}
        (OUT / f"{name}.json").write_text(json.dumps(res, indent=1))
        show(res)


def show(obj: Any, pad: str = "") -> None:
    """Compact print: dicts nest, a list prints one element per line."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                print(f"{pad}{k}:")
                show(v, pad + "  ")
            else:
                print(f"{pad}{k}: {json.dumps(v)}")
    elif isinstance(obj, list):
        for v in obj:
            if isinstance(v, dict):
                print(f"{pad}-")
                show(v, pad + "  ")
            else:
                print(f"{pad}{json.dumps(v)}")
    else:
        print(f"{pad}{json.dumps(obj)}")
    sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
