"""Helpers for reading, storing and comparing `ChatContext` contents."""

from __future__ import annotations

import json

from livekit.agents.llm import ChatContext
from pydantic import ValidationError


def assistant_texts(ctx: ChatContext) -> list[str]:
    """Text of every assistant message in `ctx`, in order.

    Assistant messages without text (for example a tool-call-only turn) are skipped.
    """
    return [
        text
        for item in ctx.items
        if item.type == "message" and item.role == "assistant" and (text := item.text_content)
    ]


def dump_ctx(ctx: ChatContext) -> dict:
    """JSON-able form of `ctx` for the store; `load_ctx` reverses it.

    Ids are kept: the SDK finds the instructions message by id and replaces it on each
    activation (voice/generation.py:1334-1346), and merges skip items by id
    (llm/chat_context.py:738-771). Timestamps are kept: merges and inserts place items
    by `created_at` (chat_context.py:901-912), so restored items keep their order
    relative to new ones.
    """
    # SDK-FINDING-5: agent_config_update items are not stored. Every activation (start
    # and resume) appends a new one (voice/agent_activity.py:1335-1350), they never reach
    # the LLM, and their tool definitions are not serializable (chat_context.py:512-513).
    # Stored ones would pile up, one more per layer per cold start.
    return ctx.to_dict(exclude_timestamp=False, exclude_config_update=True)


def load_ctx(data: dict) -> ChatContext:
    """Rebuild a context stored by `dump_ctx`; raise ValueError if it no longer parses.

    The error does not carry the stored values: they are the patient's messages, and
    pydantic would print them (`input_value=`). ChatContext is an SDK model, so its
    config cannot hide them.
    """
    try:
        return ChatContext.from_dict(data)
    except ValidationError as e:
        raise ValueError(f"stored context does not parse ({e.error_count()} errors)") from None


def normalize_ctx(ctx: ChatContext) -> list[tuple]:
    """Content of `ctx` without ids and timestamps, for comparing two contexts.

    ("message", role, text) | ("function_call", name, canonical JSON args, or the raw string if it is not JSON)
    | ("function_call_output", name, output) | (item.type,) for any other item,
    except agent_config_update, which is left out (SDK-FINDING-5).
    """
    out: list[tuple] = []
    for item in ctx.items:
        if item.type == "message":
            out.append(("message", item.role, item.raw_text_content))
        elif item.type == "function_call":
            try:
                args = json.dumps(json.loads(item.arguments), sort_keys=True, separators=(",", ":"))
            except ValueError:
                args = item.arguments  # not JSON: keep the string as it is
            out.append(("function_call", item.name, args))
        elif item.type == "function_call_output":
            out.append(("function_call_output", item.name, item.output))
        elif item.type == "agent_config_update":
            # SDK-FINDING-5: a restored context always gets a fresh config record at the
            # end when its agent is re-activated, where a long-lived session has it before
            # the agent's later messages; no stored form can match that. Only this item
            # class is left out of the comparison.
            continue
        else:
            out.append((item.type,))
    return out
