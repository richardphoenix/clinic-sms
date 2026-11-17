"""Shared pieces of the text-channel spike: the local server, a fake LLM, the agents.

Everything here is synthetic: messages are short markers like "m1" or "slow", never
patient text, and nothing reads an API key.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
from dataclasses import asdict, dataclass
from typing import Any

from livekit import api
from livekit.agents import Agent, AgentTask, RunContext, function_tool, llm
from livekit.agents.beta.workflows import TaskGroup
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions

# `livekit-server --dev` placeholder credentials; local only.
URL = os.environ.get("SPIKE_LIVEKIT_URL", "ws://127.0.0.1:7880")
API_KEY = "devkey"
API_SECRET = "secret"
AGENT_NAME = "text-spike"

TOPIC_CHAT = "lk.chat"
TOPIC_TRANSCRIPTION = "lk.transcription"
TOPIC_CTL = "spike.ctl"  # driver -> worker commands
TOPIC_DUMP = "spike.dump"  # worker -> driver state dumps

SLOW_CHUNKS = 8
SLOW_DELAY = 0.25  # seconds per chunk, so a slow reply takes ~2 s
FAST_DELAY = 0.05


@dataclass
class Cfg:
    """Per-room behavior, carried in the dispatch metadata."""

    greet: bool = True  # Reception.on_enter speaks first
    greet_slow: bool = False
    greet_interruptible: bool = True
    connect_first: bool = True  # entrypoint awaits ctx.connect() before session.start()
    wait_participant: bool = False  # entrypoint awaits ctx.wait_for_participant() first
    close_on_disconnect: bool = True
    user_away_timeout: float | None = 15.0
    slow_after: bool = False  # the old agent's reply after a handoff tool takes ~2 s

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> Cfg:
        return cls(**json.loads(raw)) if raw else cls()


def participant_token(room: str, identity: str, cfg: Cfg | None) -> str:
    """A token that joins `room`; with `cfg`, joining also dispatches the spike agent."""
    tok = (
        api.AccessToken(API_KEY, API_SECRET)
        .with_identity(identity)
        .with_grants(api.VideoGrants(room_join=True, room=room))
    )
    if cfg is not None:
        tok = tok.with_room_config(
            api.RoomConfiguration(
                agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME, metadata=cfg.to_json())]
            )
        )
    return tok.to_jwt()


# --- fake LLM -------------------------------------------------------------------------


def _agent_of(chat_ctx: llm.ChatContext) -> str:
    for item in chat_ctx.items:
        if item.type == "message" and item.role in ("system", "developer"):
            text = item.text_content or ""
            if text.startswith("agent="):
                return text.split("=", 1)[1].split()[0]
    return "?"


def _last_turn_item(chat_ctx: llm.ChatContext) -> Any:
    # skip agent_config_update and friends (see FINDING-5): only conversation items count
    for item in reversed(chat_ctx.items):
        if item.type in ("message", "function_call", "function_call_output"):
            return item
    return None


def plan_reply(chat_ctx: llm.ChatContext) -> tuple[list[str] | tuple[str, dict], float]:
    """Return (chunks or (tool, args), per-chunk delay) for this request."""
    agent = _agent_of(chat_ctx)
    last = _last_turn_item(chat_ctx)
    if last is not None and last.type == "function_call_output":
        if "slow" in last.output:
            return [f"[{agent}] after part {i}. " for i in range(SLOW_CHUNKS)], SLOW_DELAY
        return [f"[{agent}] after {last.output}"], FAST_DELAY
    if last is not None and last.type == "message" and last.role == "user":
        text = (last.text_content or "").strip()
        if text == "slow":
            return [f"[{agent}] slow part {i}. " for i in range(SLOW_CHUNKS)], SLOW_DELAY
        if text == "transfer":
            return ("transfer", {}), FAST_DELAY
        if text == "intake":
            return ("start_intake", {}), FAST_DELAY
        if text == "group":
            return ("start_group", {}), FAST_DELAY
        if text.startswith("save "):
            return ("save", {"value": text[5:]}), FAST_DELAY
        return [f"[{agent}] got {text}"], FAST_DELAY
    # no user turn last: an on_enter / instructions-only reply
    instr = (last.text_content or "") if last is not None and last.type == "message" else ""
    if "greet-slow" in instr:
        return [f"[{agent}] hello part {i}. " for i in range(SLOW_CHUNKS)], SLOW_DELAY
    return [f"[{agent}] hello"], FAST_DELAY


class FakeLLM(llm.LLM):
    def __init__(self) -> None:
        super().__init__()
        self._ids = itertools.count(1)
        self.calls: list[str] = []  # agent of each call, for the dump

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        **_: Any,
    ) -> llm.LLMStream:
        plan, delay = plan_reply(chat_ctx)
        # which agent answered, and the conversation it was shown (user/assistant only)
        seen = [
            f"{i.role[0]}:{i.text_content}"
            for i in chat_ctx.items
            if i.type == "message" and i.role in ("user", "assistant")
        ]
        self.calls.append(f"{_agent_of(chat_ctx)} sees {seen}")
        return _FakeStream(
            self,
            chat_ctx=chat_ctx,
            tools=tools or [],
            conn_options=conn_options,
            plan=plan,
            delay=delay,
            call_id=f"call_{next(self._ids)}",
        )


class _FakeStream(llm.LLMStream):
    def __init__(self, *args: Any, plan: Any, delay: float, call_id: str, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self._plan, self._delay, self._call_id = plan, delay, call_id

    async def _run(self) -> None:
        if isinstance(self._plan, tuple):
            name, args = self._plan
            await asyncio.sleep(self._delay)
            call = llm.FunctionToolCall(
                name=name, arguments=json.dumps(args), call_id=self._call_id
            )
            self._event_ch.send_nowait(
                llm.ChatChunk(id=self._call_id, delta=llm.ChoiceDelta(role="assistant", tool_calls=[call]))
            )
            return
        for chunk in self._plan:
            await asyncio.sleep(self._delay)
            self._event_ch.send_nowait(
                llm.ChatChunk(id=self._call_id, delta=llm.ChoiceDelta(role="assistant", content=chunk))
            )


# --- agents ---------------------------------------------------------------------------


class _Greeter:
    async def _greet(self: Any, *, slow: bool = False, interruptible: bool = True) -> None:
        self.session.generate_reply(
            instructions="greet-slow" if slow else "greet",
            allow_interruptions=interruptible,
        )


class StepTask(_Greeter, AgentTask[str]):
    def __init__(self, name: str) -> None:
        super().__init__(instructions=f"agent={name}")

    async def on_enter(self) -> None:
        await self._greet()

    @function_tool
    async def save(self, ctx: RunContext, value: str) -> None:
        """Save the answer and finish this step."""
        self.complete(value)


class Billing(_Greeter, Agent):
    def __init__(self, chat_ctx: llm.ChatContext | None = None) -> None:
        super().__init__(instructions="agent=billing", chat_ctx=chat_ctx)

    async def on_enter(self) -> None:
        await self._greet()


class Reception(_Greeter, Agent):
    def __init__(self, cfg: Cfg) -> None:
        super().__init__(instructions="agent=reception")
        self._cfg = cfg

    async def on_enter(self) -> None:
        if self._cfg.greet:
            await self._greet(slow=self._cfg.greet_slow, interruptible=self._cfg.greet_interruptible)

    @function_tool
    async def transfer(self, ctx: RunContext) -> Any:
        """Hand the user over to billing."""
        return Billing(chat_ctx=self.chat_ctx), (
            "transferred-slow" if self._cfg.slow_after else "transferred"
        )

    @function_tool
    async def start_intake(self, ctx: RunContext) -> str:
        """Run the intake task inline and report its result."""
        result = await StepTask("intake")
        return f"intake={result}"

    @function_tool
    async def start_group(self, ctx: RunContext) -> str:
        """Run a two-step TaskGroup inline and report its results."""
        group = TaskGroup()
        group.add(lambda: StepTask("step-a"), id="a", description="step a")
        group.add(lambda: StepTask("step-b"), id="b", description="step b")
        res = await group
        return f"group={sorted(res.task_results.items())}"
