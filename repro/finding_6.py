"""FINDING-6 repro: an exception raised from a root agent's on_enter is logged twice.

Root(Agent).on_enter raises RuntimeError("boom"). The SDK runs on_enter in a task named
"AgentTask_on_enter", wrapped by `utils.log_exceptions` (voice/agent_activity.py:
1080-1098), which logs "Error in _traceable_on_enter" and re-raises into that task.
Nothing awaits the task or reads its exception, so when it is garbage collected asyncio
reports "Task exception was never retrieved" for that same task, at whatever later point
the collection happens. The control case catches and logs the error inside on_enter;
then neither record appears.

Self-contained: livekit-agents only, an inline fake LLM, no API key.
Run: uv run python repro/finding_6.py
Exits 1 while the finding reproduces, 0 once it no longer does.
"""

from __future__ import annotations

import asyncio
import gc
import importlib.metadata
import logging
import re
import sys
from typing import Any

from livekit.agents import Agent, AgentSession, llm
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


class Root(Agent):
    def __init__(self, *, catch: bool, done: asyncio.Event) -> None:
        super().__init__(instructions="root")
        self.catch = catch
        self.done = done

    async def on_enter(self) -> None:
        try:
            raise RuntimeError("boom")  # e.g. an awaited TaskGroup that failed
        except RuntimeError:
            if not self.catch:
                raise
            logging.getLogger("repro").exception("on_enter failed")
        finally:
            self.done.set()


class Messages(logging.Handler):
    """Keeps only message text: a stored record's traceback would keep the task alive.

    For asyncio's "never retrieved" record, the name of the task it reports is kept too.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        text = record.getMessage()
        line = f"{record.name}: {text.splitlines()[0]}"
        if task := re.search(r"<Task finished name='([^']+)'", text):
            line += f" (task {task.group(1)})"
        self.messages.append(line)


async def case(catch: bool) -> list[str]:
    handler = Messages()
    loggers = [logging.getLogger(n) for n in ("livekit.agents", "asyncio", "repro")]
    for lg in loggers:
        lg.addHandler(handler)
        lg.propagate = False
    try:
        done = asyncio.Event()
        session = AgentSession(llm=FakeLLM())
        await asyncio.wait_for(session.start(Root(catch=catch, done=done)), TIMEOUT)
        await asyncio.wait_for(done.wait(), TIMEOUT)
        await asyncio.sleep(0.1)  # let the on_enter task finish
        await asyncio.wait_for(session.aclose(), TIMEOUT)
        del session
        await asyncio.sleep(0.1)
        gc.collect()  # collect the finished on_enter task now rather than at exit
        await asyncio.sleep(0)
    finally:
        for lg in loggers:
            lg.removeHandler(handler)
            lg.propagate = True
    return handler.messages


async def main() -> int:
    print(f"livekit-agents {importlib.metadata.version('livekit-agents')}")
    raised = await case(catch=False)
    caught = await case(catch=True)
    print(f"on_enter raises          -> ERROR records: {raised}")
    print(f"on_enter catches and logs -> ERROR records: {caught}")
    sdk = any(m.startswith("livekit.agents: Error in _traceable_on_enter") for m in raised)
    again = (
        "asyncio: Task exception was never retrieved (task AgentTask_on_enter)" in raised
    )
    control_ok = caught == ["repro: on_enter failed"]
    if sdk and again and control_ok:
        print(
            "REPRODUCED: the SDK logs an exception from a root agent's on_enter and leaves it "
            "unretrieved in its task, so asyncio logs it a second time when the task is collected"
        )
        return 1
    print("NOT REPRODUCED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
