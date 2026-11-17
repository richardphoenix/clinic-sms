"""What reaches the logs about an exception: its type and traceback, never its text.

An exception's message can carry a patient's or a staff member's words (an LLM error
that echoes its input, a validation error that shows a value). Failure logs therefore
record `describe_exception(e)`, never `str(e)` or `exc_info`.
"""

from __future__ import annotations

import logging
import re
import traceback

REDACTED = "<redacted>"

# The SDK loggers on this app's code paths (livekit/agents/log.py,
# livekit/plugins/anthropic/log.py). A logger-level filter only sees records created on
# that exact logger, so each one gets its own.
SDK_LOGGERS = ("livekit.agents", "livekit.plugins.anthropic")

# SDK sites that put an exception's text into the message itself, without exc_info
_SDK_TEXT_SITES = (
    # livekit/agents/llm/llm.py:371, the retry warning
    (
        re.compile(r"^(failed to generate LLM completion): .*(, retrying in \S+s)$", re.DOTALL),
        rf"\1: {REDACTED}\2",
    ),
    # livekit/agents/voice/agent_session.py:2081
    (
        re.compile(r"^(AgentSession is closing due to unrecoverable error): .*$", re.DOTALL),
        rf"\1: {REDACTED}",
    ),
)


def describe_exception(exc: BaseException) -> str:
    """The qualified type name and traceback frames of `exc` and its chain; never its text.

    One block per exception: a `module.QualName` line, then its `traceback.format_tb`
    lines. The chain follows `__cause__`, or `__context__` when there is no cause;
    blocks are joined by "\\n-- caused by --\\n".
    """
    blocks: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:  # a chain can loop
        seen.add(id(current))
        kind = type(current)
        frames = "".join(traceback.format_tb(current.__traceback__))
        blocks.append(f"{kind.__module__}.{kind.__qualname__}\n{frames}".rstrip("\n"))
        current = current.__cause__ if current.__cause__ is not None else current.__context__
    return "\n-- caused by --\n".join(blocks)


# App log policy: SDK log sites may include exception text (see docs/findings.md "Notes (Plan 2)").
class SdkLogRedaction(logging.Filter):
    """Rewrites SDK records at WARNING and above so they carry no exception text.

    - A record with an exception: `exc_text` becomes `describe_exception(exc)` and
      `exc_info` is cleared; where the exception's text appears in the message, it is
      replaced by REDACTED.
    - The known SDK sites that format the text into the message are rewritten.

    Records are redacted, never dropped. Records below WARNING pass unchanged.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.WARNING:
            return True
        try:
            _redact(record)
        except Exception:  # noqa: BLE001 - must not raise into the SDK's logging call
            record.msg, record.args = f"{REDACTED} (the record could not be redacted)", ()
            record.exc_info, record.exc_text = None, None
        return True


def _redact(record: logging.LogRecord) -> None:
    message = record.getMessage()
    redacted = message
    exc = record.exc_info[1] if isinstance(record.exc_info, tuple) else None
    if exc is not None:
        record.exc_text = describe_exception(exc)
        record.exc_info = None
        text = str(exc)
        if text and text in redacted:
            redacted = redacted.replace(text, REDACTED)
    for pattern, replacement in _SDK_TEXT_SITES:
        redacted = pattern.sub(replacement, redacted)
    if redacted != message:
        record.msg, record.args = redacted, ()


def install_sdk_log_redaction() -> None:
    """Attach SdkLogRedaction to each SDK logger, once.

    Logger-level filters run before the record propagates to any handler, so every
    sink (stderr, pytest's caplog) gets the redacted record.
    """
    for name in SDK_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, SdkLogRedaction) for f in logger.filters):
            logger.addFilter(SdkLogRedaction())
