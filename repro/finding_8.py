"""FINDING-8 repro: a failed reply in a handoff turn does not fail session.run().

A tool on agent Old returns `(New(), "moved")`. The SDK hands off to New and has Old
answer the tool output first, on the same speech handle as the tool call (livekit-agents
1.8.11, voice/agent_activity.py:4131-4134 and 4173-4199); New's on_enter then speaks on
its own handle. When Old's reply fails, the error is stored only on Old's handle
(agent_activity.py:3661-3669), and RunResult raises only the error of the last speech
handle to finish (voice/run_result.py:268, 281-289), which is New's. So `run()` returns
normally, New's message is delivered, and the failed reply is only visible on its
speech handle (`exception()`) and as an unrecoverable session `error` event.

  no handoff (control)  the tool returns only "moved"; Old's reply fails. run() raises.
  handoff, reply fails  the case above. run() returns.
  handoff, all ok       (control) run() returns; no handle error, no error event.

The SDK's own ERROR record for the failed call ("Error in _llm_inference_task", with a
traceback) is expected and silenced, so the output is only the three outcomes.

Self-contained: livekit-agents only, an inline scripted fake LLM, no API key.
Run: uv run python repro/finding_8.py
Exits 1 while the finding reproduces, 0 once it no longer does, 2 if a control fails.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import itertools
import logging
import sys
from dataclasses import dataclass, field
from typing import Any

from livekit.agents import (
    Agent,
    AgentSession,
    APIError,
    ConversationItemAddedEvent,
    ErrorEvent,
    RunContext,
    SpeechCreatedEvent,
    function_tool,
    llm,
)
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions

TIMEOUT = 5.0
ASK = "Ask what the user wants next."
FAIL = object()  # script value: the call raises a non-retryable APIError


class ScriptedLLM(llm.LLM):
    """Answers each call from a script keyed by the last item's text or tool output.

    A script value is reply text, ("tool", name) for a tool call, or FAIL.
    """

    def __init__(self, script: dict[str, Any]) -> None:
        super().__init__()
        self._script = script
        self._ids = itertools.count(1)

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        **_: Any,
    ) -> llm.LLMStream:
        last = chat_ctx.items[-1]
        key = last.output if last.type == "function_call_output" else last.text_content
        reply = self._script[key]
        return _Stream(
            self,
            chat_ctx=chat_ctx,
            tools=tools or [],
            conn_options=conn_options,
            reply=reply,
            call_id=f"call_{next(self._ids)}",
        )


class _Stream(llm.LLMStream):
    def __init__(self, *args: Any, reply: Any, call_id: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._reply = reply
        self._call_id = call_id

    async def _run(self) -> None:
        if self._reply is FAIL:
            raise APIError("down", retryable=False)
        if isinstance(self._reply, tuple):
            call = llm.FunctionToolCall(name=self._reply[1], arguments="{}", call_id=self._call_id)
            delta = llm.ChoiceDelta(role="assistant", tool_calls=[call])
        else:
            delta = llm.ChoiceDelta(role="assistant", content=self._reply)
        self._event_ch.send_nowait(llm.ChatChunk(id="fake", delta=delta))


class New(Agent):
    def __init__(self) -> None:
        super().__init__(instructions="new")

    async def on_enter(self) -> None:
        self.session.generate_reply(instructions=ASK)


class Old(Agent):
    def __init__(self, *, handoff: bool) -> None:
        super().__init__(instructions="old")
        self._handoff = handoff

    @function_tool
    async def move(self, ctx: RunContext) -> Any:
        """Move the user on."""
        return (New(), "moved") if self._handoff else "moved"


@dataclass
class Outcome:
    raised: str | None = None  # type name of what run() raised
    sent: list[str] = field(default_factory=list)  # assistant messages, in order
    handle_errors: list[str] = field(default_factory=list)  # type names of exception()
    error_events: list[str] = field(default_factory=list)  # unrecoverable `error` events


async def case(*, handoff: bool, old_reply: Any) -> Outcome:
    out = Outcome()
    handles = []
    script = {"go": ("tool", "move"), "moved": old_reply, ASK: "What next?"}
    session = AgentSession(llm=ScriptedLLM(script))

    def on_item(ev: ConversationItemAddedEvent) -> None:
        if ev.item.type == "message" and ev.item.role == "assistant":
            out.sent.append(ev.item.text_content or "")

    def on_error(ev: ErrorEvent) -> None:
        if not ev.error.recoverable:
            out.error_events.append(type(ev.error.error).__name__)

    session.on("conversation_item_added", on_item)
    session.on("speech_created", lambda ev: handles.append(ev.speech_handle))
    session.on("error", on_error)
    try:
        await asyncio.wait_for(session.start(Old(handoff=handoff)), TIMEOUT)
        try:
            await asyncio.wait_for(session.run(user_input="go"), TIMEOUT)
        except APIError as e:
            out.raised = type(e).__name__
        for handle in handles:
            await asyncio.wait_for(handle.wait_for_playout(), TIMEOUT)
            if (exc := handle.exception()) is not None:
                out.handle_errors.append(type(exc).__name__)
    finally:
        await asyncio.wait_for(session.aclose(), TIMEOUT)
    return out


def show(label: str, o: Outcome) -> None:
    print(
        f"{label:22} run() raised: {o.raised}  sent: {o.sent}  "
        f"handle errors: {o.handle_errors}  unrecoverable error events: {o.error_events}"
    )


async def main() -> int:
    logging.getLogger("livekit.agents").setLevel(logging.CRITICAL)  # see the docstring
    print(f"livekit-agents {importlib.metadata.version('livekit-agents')}")
    plain = await case(handoff=False, old_reply=FAIL)
    failed = await case(handoff=True, old_reply=FAIL)
    ok = await case(handoff=True, old_reply="Moving you.")
    show("no handoff (control)", plain)
    show("handoff, reply fails", failed)
    show("handoff, all ok", ok)
    if not (
        plain.raised == "APIError"
        and ok.raised is None
        and ok.sent == ["Moving you.", "What next?"]
        and not ok.handle_errors
        and not ok.error_events
    ):
        print("INCONCLUSIVE: a control did not behave as expected")
        return 2
    if (
        failed.raised is None
        and failed.sent == ["What next?"]
        and "APIError" in failed.handle_errors
        and "APIError" in failed.error_events
    ):
        print(
            "REPRODUCED: in a handoff turn, the old agent's failed reply is stored only on its "
            "speech handle; run() returns normally and only the new agent's message is sent"
        )
        return 1
    print("NOT REPRODUCED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
