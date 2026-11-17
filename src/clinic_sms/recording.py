"""Record the LLM's answers to a JSONL file and replay them offline.

One line per `chat()` call: {"n": int, "request": fingerprint, "response": {"text": str,
"tools": [[name, args], ...]}}. A replay serves line `cursor` to the next call after checking
that the request matches what was recorded; on a difference it fails the call with a
non-retryable APIError whose text names only the call number and item index, never message
text. The recording itself holds the (fictional) conversation, so it is not log-safe to print.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from livekit.agents import APIError
from livekit.agents.llm import (
    LLM,
    ChatChunk,
    ChatContext,
    ChoiceDelta,
    FunctionToolCall,
    LLMStream,
    Tool,
    ToolChoice,
)
from livekit.agents.types import (
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    APIConnectOptions,
    NotGivenOr,
)

from clinic_sms.ctx import normalize_ctx
from clinic_sms.testing import tool_names

_CHUNK_SIZE = 3


# The SDK's summarizer (ChatContext._summarize, used when the intake group resumes) writes
# the history into one message as text, with each tool call's id inline. Real ids come from
# Claude and a replay hands out its own, so the ids must not take part in the comparison.
_CALL_ID_RE = re.compile(r'call_id="[^"]*"')


def _scrub(item: list) -> list:
    return [_CALL_ID_RE.sub('call_id="<id>"', v) if isinstance(v, str) else v for v in item]


def request_fingerprint(chat_ctx: ChatContext, tools: list[Tool]) -> dict:
    """What a request is compared by: its normalized context and the names of its tools.

    Tool call ids inside message text are replaced by a placeholder (see `_CALL_ID_RE`).
    """
    return {"ctx": [_scrub(list(t)) for t in normalize_ctx(chat_ctx)], "tools": tool_names(tools)}


def _describe_difference(call: int, recorded: dict, now: dict) -> str | None:
    """The mismatch text for `now` against `recorded`, or None if they are equal.

    Only the call number and the first differing item's index are named.
    """
    # scrub the recorded side too: a recording made before the scrub keeps the real ids
    a, b = [_scrub(list(t)) for t in recorded.get("ctx", [])], now["ctx"]
    if a == b and recorded.get("tools") == now["tools"]:
        return None
    if a != b:
        pairs = enumerate(zip(a, b, strict=False))
        index = next((i for i, (x, y) in pairs if x != y), min(len(a), len(b)))
        return f"replay mismatch at call {call}: request differs at item {index}"
    return f"replay mismatch at call {call}: request differs in the tools offered"


class RecordReplayLLM(LLM):
    def __init__(
        self,
        *,
        mode: Literal["record", "replay"],
        path: Path,
        inner_factory: Callable[[], LLM] | None = None,
        cursor: int = 0,
        on_cursor: Callable[[int], None] | None = None,
    ) -> None:
        super().__init__()
        if mode == "record" and inner_factory is None:
            raise ValueError("record mode needs an inner_factory")
        self._mode = mode
        self._path = Path(path)
        self._inner_factory = inner_factory
        self._inner: LLM | None = None
        self._on_cursor = on_cursor
        self._rows: list[dict] | None = None
        self.mismatch: str | None = None
        # Recording appends, so it carries on after the lines already there.
        self.cursor = self._count_lines() if mode == "record" else cursor

    @property
    def model(self) -> str:
        return "record-replay"

    @property
    def provider(self) -> str:
        return "record-replay"

    def _count_lines(self) -> int:
        try:
            with self._path.open(encoding="utf-8") as f:
                return sum(1 for line in f if line.strip())
        except FileNotFoundError:
            return 0

    def _advance(self) -> None:
        self.cursor += 1
        if self._on_cursor is not None:
            self._on_cursor(self.cursor)

    def _recorded(self) -> list[dict]:
        if self._rows is None:
            try:
                text = self._path.read_text(encoding="utf-8")
                self._rows = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
            except (OSError, ValueError):
                self._rows = []
        return self._rows

    def chat(
        self,
        *,
        chat_ctx: ChatContext,
        tools: list[Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls: NotGivenOr[bool] = NOT_GIVEN,
        tool_choice: NotGivenOr[ToolChoice] = NOT_GIVEN,
        extra_kwargs: NotGivenOr[dict[str, Any]] = NOT_GIVEN,
    ) -> LLMStream:
        tools = tools or []
        fingerprint = request_fingerprint(chat_ctx, tools)
        if self._mode == "record":
            if self._inner is None:
                assert self._inner_factory is not None
                self._inner = self._inner_factory()
            inner_stream = self._inner.chat(
                chat_ctx=chat_ctx,
                tools=tools,
                conn_options=conn_options,
                parallel_tool_calls=parallel_tool_calls,
                tool_choice=tool_choice,
                extra_kwargs=extra_kwargs,
            )
            return _RecordStream(
                self,
                chat_ctx=chat_ctx,
                tools=tools,
                conn_options=conn_options,
                inner_stream=inner_stream,
                fingerprint=fingerprint,
            )

        # Replay: resolve here, so a retried attempt sees the same answer.
        error: APIError | None = None
        response: dict = {"text": "", "tools": []}
        rows = self._recorded()
        if self.mismatch is not None:
            problem: str | None = self.mismatch  # the first problem stays the one reported
        elif self.cursor >= len(rows):
            problem = f"replay mismatch at call {self.cursor}: the recording has no such call"
        else:
            row = rows[self.cursor]
            # round-trip through JSON so tuples compare equal to the stored lists
            problem = _describe_difference(
                self.cursor, row.get("request", {}), json.loads(json.dumps(fingerprint))
            )
            response = row.get("response", response)
        if problem is not None:
            self.mismatch = self.mismatch or problem
            error = APIError(problem, retryable=False)
        else:
            self._advance()
        return _ReplayStream(
            self,
            chat_ctx=chat_ctx,
            tools=tools,
            conn_options=conn_options,
            response=response,
            error=error,
            call_no=self.cursor,
        )


def _send(stream: LLMStream, chunk_id: str, delta: ChoiceDelta) -> None:
    stream._event_ch.send_nowait(ChatChunk(id=chunk_id, delta=delta))


class _RecordStream(LLMStream):
    def __init__(
        self,
        llm: RecordReplayLLM,
        *,
        chat_ctx: ChatContext,
        tools: list[Tool],
        conn_options: APIConnectOptions,
        inner_stream: LLMStream,
        fingerprint: dict,
    ) -> None:
        super().__init__(llm, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options)
        self._recorder = llm
        self._inner_stream = inner_stream
        self._fingerprint = fingerprint

    async def _run(self) -> None:
        text: list[str] = []
        calls: list[list] = []
        async with self._inner_stream as inner:
            async for chunk in inner:
                self._event_ch.send_nowait(chunk)
                if chunk.delta is None:
                    continue
                if chunk.delta.content:
                    text.append(chunk.delta.content)
                for call in chunk.delta.tool_calls:
                    try:
                        args = json.loads(call.arguments or "{}")
                    except ValueError:
                        args = {}
                    calls.append([call.name, args])
        # Only a stream that ended normally is written; an error above wrote nothing.
        row = {
            "n": self._recorder.cursor,
            "request": self._fingerprint,
            "response": {"text": "".join(text), "tools": calls},
        }
        self._recorder._path.parent.mkdir(parents=True, exist_ok=True)
        with self._recorder._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._recorder._advance()


class _ReplayStream(LLMStream):
    def __init__(
        self,
        llm: RecordReplayLLM,
        *,
        chat_ctx: ChatContext,
        tools: list[Tool],
        conn_options: APIConnectOptions,
        response: dict,
        error: APIError | None,
        call_no: int,
    ) -> None:
        super().__init__(llm, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options)
        self._response = response
        self._error = error
        self._chunk_id = f"replay_{call_no}"

    async def _run(self) -> None:
        if self._error is not None:
            raise self._error
        text = self._response.get("text", "")
        for i in range(0, len(text), _CHUNK_SIZE):
            delta = ChoiceDelta(role="assistant", content=text[i : i + _CHUNK_SIZE])
            _send(self, self._chunk_id, delta)
        calls = [
            FunctionToolCall(
                name=name, arguments=json.dumps(args), call_id=f"{self._chunk_id}_{i}"
            )
            for i, (name, args) in enumerate(self._response.get("tools", []))
        ]
        if calls:
            _send(self, self._chunk_id, ChoiceDelta(role="assistant", tool_calls=calls))
