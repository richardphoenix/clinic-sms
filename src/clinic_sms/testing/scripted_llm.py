# Portions of this file (the lookup-key rule in `lookup_key` and the shape of
# `ScriptedLLMStream`) are derived from `tests/fake_llm.py` in livekit/agents:
#
# Copyright 2023 LiveKit, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""A deterministic fake LLM for tests, spikes and offline CLI runs.

Each `chat()` call is answered from a script keyed by the text of the last
chat item (see `lookup_key`). A call whose last item has no key is recorded as
`<no key: <item type>>` and answered by `default`, or fails like an unscripted
input. Every call's key, context and tools are recorded. Nothing is sent over the
network.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from dataclasses import dataclass, field
from typing import Any

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

_CHUNK_SIZE = 3


@dataclass
class Reply:
    """One scripted LLM turn."""

    text: str = ""
    tools: list[tuple[str, dict]] = field(default_factory=list)  # (tool name, JSON-able args)
    # Raised instead of replying. Note the SDK retries an APIError whose
    # retryable flag is True (the APIError default), so pass retryable=False
    # unless retries are what the test wants.
    error: Exception | None = None
    delay: float = 0.0  # seconds before the first chunk


def lookup_key(chat_ctx: ChatContext) -> str:
    """Return the script key for a chat context.

    Same rule as upstream `FakeLLMStream._get_index_text`: look at the last item;
    a user/system message (user input or `generate_reply(instructions=...)`) keys
    on its text, a `function_call_output` keys on its `output`.
    """
    if not chat_ctx.items:
        raise ValueError("no input text found: chat context is empty")

    item = chat_ctx.items[-1]
    if item.type == "message" and item.role in ("user", "system") and (text := item.text_content):
        return text
    if item.type == "function_call_output":
        return item.output

    raise ValueError(f"no input text found: {item!r}")


class ScriptedLLM(LLM):
    def __init__(self, replies: dict[str, Reply], *, default: Reply | None = None) -> None:
        super().__init__()
        self._replies = dict(replies)
        self._default = default
        self._call_ids = itertools.count(1)
        self._request_ids = itertools.count(1)
        self.calls: list[str] = []  # key of every chat() call, in order
        self.contexts: list[ChatContext] = []  # chat_ctx.copy() of every call
        self.tools: list[list[Tool]] = []  # the tools offered to every call

    @property
    def model(self) -> str:
        return "scripted"

    @property
    def provider(self) -> str:
        return "scripted"

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
        # Resolve the reply here, not in the stream, so a retried attempt replays
        # the same reply and `calls` records exactly one entry per chat() call.
        try:
            key: str | None = lookup_key(chat_ctx)
            label = key
        except ValueError:
            # The SDK does call the LLM with contexts that have no key, e.g. ending
            # in an agent_config_update. Record the call and treat it as unscripted.
            key = None
            last = chat_ctx.items[-1].type if chat_ctx.items else "empty"
            label = f"<no key: {last}>"
        self.calls.append(label)
        self.contexts.append(chat_ctx.copy())
        self.tools.append(list(tools or []))

        reply = self._default if key is None else self._replies.get(key, self._default)
        if reply is None:
            reply = Reply(error=APIError(f"unscripted input: {label!r}", retryable=False))

        # Tool call ids are allocated up front so they stay unique per instance.
        tool_calls = [
            FunctionToolCall(
                name=name, arguments=json.dumps(args), call_id=f"call_{next(self._call_ids)}"
            )
            for name, args in reply.tools
        ]
        return ScriptedLLMStream(
            self,
            chat_ctx=chat_ctx,
            tools=tools or [],
            conn_options=conn_options,
            reply=reply,
            tool_calls=tool_calls,
            request_id=f"scripted_{next(self._request_ids)}",
        )


class ScriptedLLMStream(LLMStream):
    def __init__(
        self,
        llm: ScriptedLLM,
        *,
        chat_ctx: ChatContext,
        tools: list[Tool],
        conn_options: APIConnectOptions,
        reply: Reply,
        tool_calls: list[FunctionToolCall],
        request_id: str,
    ) -> None:
        super().__init__(llm, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options)
        self._reply = reply
        self._tool_calls = tool_calls
        self._request_id = request_id

    async def _run(self) -> None:
        if self._reply.delay > 0:
            await asyncio.sleep(self._reply.delay)

        if self._reply.error is not None:
            raise self._reply.error

        text = self._reply.text
        for i in range(0, len(text), _CHUNK_SIZE):
            self._send(ChoiceDelta(role="assistant", content=text[i : i + _CHUNK_SIZE]))

        if self._tool_calls:
            self._send(ChoiceDelta(role="assistant", tool_calls=list(self._tool_calls)))

    def _send(self, delta: ChoiceDelta) -> None:
        self._event_ch.send_nowait(ChatChunk(id=self._request_id, delta=delta))
