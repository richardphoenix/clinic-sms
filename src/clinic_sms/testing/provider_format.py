"""Check the tools a ScriptedLLM was offered as the Anthropic plugin would send them.

ScriptedLLM accepts any tool schema; Claude does not. This formats each call's tools
with the same SDK call livekit-plugins-anthropic 1.8.11 makes, so tests catch a schema
Anthropic rejects without a key.
"""

from __future__ import annotations

from typing import Any

from livekit.agents import llm

from clinic_sms.testing.scripted_llm import ScriptedLLM


def anthropic_tool_schemas(tools: list[llm.Tool]) -> list[dict[str, Any]]:
    """The function-tool schemas the Anthropic plugin sends for `tools`.

    Same call as livekit-plugins-anthropic 1.8.11 (plugins/anthropic/llm.py:199-202),
    with the plugin's default `_strict_tool_schema=True`.
    """
    return llm.ToolContext(tools).parse_function_tools("anthropic", strict=True)


def tool_names(tools: list[llm.Tool]) -> list[str]:
    """Sorted names of the function tools in `tools`, i.e. what the LLM can call."""
    return sorted(llm.ToolContext(tools).function_tools)


def empty_enums(schema: Any, path: str = "") -> list[str]:
    """Paths in `schema` whose `enum` is an empty list.

    Anthropic rejects such a tool with 400 "Invalid schema: Enum must be a non-empty
    array".
    """
    found: list[str] = []
    if isinstance(schema, dict):
        for key, value in schema.items():
            sub = f"{path}.{key}" if path else str(key)
            if key == "enum" and isinstance(value, list) and not value:
                found.append(sub)
            found.extend(empty_enums(value, sub))
    elif isinstance(schema, list):
        for i, value in enumerate(schema):
            found.extend(empty_enums(value, f"{path}[{i}]"))
    return found


def anthropic_tool_problems(scripted: ScriptedLLM) -> list[str]:
    """One line per empty enum in the tools of any call `scripted` received."""
    problems: list[str] = []
    for n, (key, tools) in enumerate(zip(scripted.calls, scripted.tools, strict=True)):
        for schema in anthropic_tool_schemas(tools):
            for path in empty_enums(schema):
                problems.append(f"call {n} ({key[:40]!r}): tool {schema['name']}: empty {path}")
    return problems
