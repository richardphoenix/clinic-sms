"""FINDING-4 repro: closing a session cancels a waiting AgentTask with a plain ToolError.

Chain: Root(Agent).on_enter awaits a stock TaskGroup (summary off) with one step that
waits for an answer that never comes, as a step does at the end of every cold-started
SMS turn. What Root's `await group` raises is what the TaskGroup re-raises after its
step ends (task_group.py:134-140). Three cases:

  1. close   the session is closed while the step waits;
  2. failure the step is completed with ToolError("real failure") while the session is
             open. This failure is synthetic: a stand-in for a genuine tool error, raised
             by the script, not by the SDK;
  3. uncaught as case 1, but Root.on_enter does not catch what `await group` raises.

The script checks that:

  - in cases 1 and 2 Root catches the same class, livekit.agents.llm.ToolError, with no
    public attribute that tells a close from a failure (only the message differs, and
    the message is not a contract);
  - an owner-set flag does tell them apart: Root reads `closing`, which the script sets
    right before aclose() (the app-level workaround);
  - in case 3, the normal close makes the SDK log an ERROR with a traceback
    ("Error in _traceable_on_enter", voice/agent_activity.py:1080-1098). The script
    collects garbage before it stops listening, so asyncio's second report of the same
    exception (FINDING-6) shows up here too instead of at exit.

Not shown: SDK-internal state (such as AgentSession._is_closing) is not read.

Self-contained: livekit-agents only, an inline fake LLM, no API key.
Run: uv run python repro/finding_4.py
Exits 1 while the finding reproduces, 0 once it no longer does.
"""

from __future__ import annotations

import asyncio
import gc
import importlib.metadata
import logging
import sys
from typing import Any

from livekit.agents import Agent, AgentSession, AgentTask, llm
from livekit.agents.beta.workflows import TaskGroup
from livekit.agents.llm import ToolError
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
        return _OkStream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options)


class _OkStream(llm.LLMStream):
    async def _run(self) -> None:
        delta = llm.ChoiceDelta(role="assistant", content="ok")
        self._event_ch.send_nowait(llm.ChatChunk(id="fake", delta=delta))


class Step(AgentTask[str]):
    def __init__(self, root: Root) -> None:
        super().__init__(instructions="step")
        self._root = root

    async def on_enter(self) -> None:
        self._root.step = self
        self._root.entered.set()  # then wait for an answer that never comes


class Root(Agent):
    def __init__(self, *, catch: bool) -> None:
        super().__init__(instructions="root")
        self.catch = catch
        self.group = TaskGroup(summarize_chat_ctx=False)
        self.group.add(lambda: Step(self), id="step", description="the step")
        self.step: Step | None = None
        self.entered = asyncio.Event()
        self.closing = False  # set by the owner right before aclose()
        self.caught: BaseException | None = None
        self.closing_when_caught: bool | None = None

    async def on_enter(self) -> None:
        if not self.catch:
            await self.group
            return
        try:
            await self.group
        except ToolError as e:
            self.caught = e
            self.closing_when_caught = self.closing


class Errors(logging.Handler):
    """ERROR records: logger, first line, and whether a traceback is attached."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        tb = " +traceback" if record.exc_info else ""
        self.records.append(f"{record.name}: {record.getMessage().splitlines()[0]}{tb}")


async def case(name: str) -> tuple[BaseException | None, bool | None, list[str]]:
    """Run one case; return what Root caught, its owner flag then, and the ERROR records."""
    handler = Errors()
    loggers = [logging.getLogger(n) for n in ("livekit.agents", "asyncio")]
    for lg in loggers:
        lg.addHandler(handler)
        lg.propagate = False
    try:
        root = Root(catch=name != "uncaught")
        session = AgentSession(llm=FakeLLM())
        await asyncio.wait_for(session.start(root), TIMEOUT)
        await asyncio.wait_for(root.entered.wait(), TIMEOUT)
        if name == "failure":
            assert root.step is not None
            root.step.complete(ToolError("real failure"))  # synthetic
            await asyncio.sleep(0.2)
        root.closing = True
        await asyncio.wait_for(session.aclose(), TIMEOUT)
        await asyncio.sleep(0.2)
        caught, flag = root.caught, root.closing_when_caught
        del session, root
        gc.collect()  # anything left unretrieved is reported now, inside the capture
        await asyncio.sleep(0)
    finally:
        for lg in loggers:
            lg.removeHandler(handler)
            lg.propagate = True
    return caught, flag, handler.records


def public_attrs(e: BaseException | None) -> dict[str, Any] | None:
    return None if e is None else {k: v for k, v in vars(e).items() if not k.startswith("_")}


async def main() -> int:
    print(f"livekit-agents {importlib.metadata.version('livekit-agents')}")
    closed, closed_flag, closed_log = await case("close")
    failed, failed_flag, failed_log = await case("failure")
    _, _, uncaught_log = await case("uncaught")
    for name, e, flag, log in (
        ("1 close", closed, closed_flag, closed_log),
        ("2 failure", failed, failed_flag, failed_log),
    ):
        print(
            f"{name:9} -> caught {type(e).__name__}({e}) public attrs={public_attrs(e)} "
            f"owner flag when caught={flag} ERROR records={log}"
        )
    print(f"3 uncaught -> ERROR records={uncaught_log}")

    same_type = type(closed) is type(failed) is ToolError
    no_public_marker = public_attrs(closed) == public_attrs(failed) == {}
    flag_works = closed_flag is True and failed_flag is False
    logged_on_close = any(
        r.startswith("livekit.agents: Error in _traceable_on_enter") and r.endswith("+traceback")
        for r in uncaught_log
    )
    if same_type and no_public_marker and flag_works and logged_on_close:
        print(
            "REPRODUCED: a close-cancellation reaches the awaiting agent as a plain ToolError "
            "with no public marker (only an owner-set flag tells it from a failure), and an "
            "on_enter that does not catch it gets an ERROR with traceback on a normal close"
        )
        return 1
    print("NOT REPRODUCED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
