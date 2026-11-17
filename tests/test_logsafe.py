import logging
import sys

from livekit.agents import APIError

from clinic_sms.logsafe import SdkLogRedaction, describe_exception, install_sdk_log_redaction

SECRET = "Jane says her knee hurts"


def sdk_record(
    level: int, msg: str, args: tuple = (), exc: BaseException | None = None
) -> logging.LogRecord:
    """A record as the SDK's `livekit.agents` logger creates it, `exc` raised and caught."""
    exc_info = None
    if exc is not None:
        try:
            raise exc
        except BaseException:
            exc_info = sys.exc_info()
    logger = logging.getLogger("livekit.agents")
    return logger.makeRecord(logger.name, level, "sdk.py", 1, msg, args, exc_info)


def test_describe_exception_has_types_and_frames_but_no_text():
    try:
        try:
            raise ValueError(SECRET)
        except ValueError as e:
            raise RuntimeError(SECRET) from e
    except RuntimeError as e:
        text = describe_exception(e)
    first, second = text.split("\n-- caused by --\n")
    assert first.splitlines()[0] == "builtins.RuntimeError"
    assert second.splitlines()[0] == "builtins.ValueError"
    assert "test_describe_exception_has_types_and_frames_but_no_text" in first
    assert "knee hurts" not in text


def test_sdk_log_redaction_rewrites_known_sites():
    install_sdk_log_redaction()
    install_sdk_log_redaction()  # idempotent
    for name in ("livekit.agents", "livekit.plugins.anthropic"):
        filters = logging.getLogger(name).filters
        assert sum(isinstance(f, SdkLogRedaction) for f in filters) == 1, name
    sdk = logging.getLogger("livekit.agents")
    exc = APIError(SECRET, retryable=False)
    retry = sdk_record(
        logging.WARNING, f"failed to generate LLM completion: {exc}, retrying in 2.0s"
    )
    closing = sdk_record(
        logging.ERROR, f"AgentSession is closing due to unrecoverable error: {exc}"
    )
    logged = sdk_record(logging.ERROR, "Error in _llm_inference_task", exc=exc)
    echoed = sdk_record(logging.ERROR, "tool failed: %s", (exc,), exc=exc)
    for record in (retry, closing, logged, echoed):
        assert sdk.filter(record)  # kept, never dropped
    assert retry.getMessage() == "failed to generate LLM completion: <redacted>, retrying in 2.0s"
    assert closing.getMessage() == "AgentSession is closing due to unrecoverable error: <redacted>"
    assert logged.getMessage() == "Error in _llm_inference_task"
    assert echoed.getMessage() == "tool failed: <redacted>" and echoed.args == ()
    for record in (logged, echoed):
        assert record.exc_info is None
        assert record.exc_text.splitlines()[0] == "livekit.agents._exceptions.APIError"
    for record in (retry, closing, logged, echoed):
        formatted = logging.Formatter().format(record)
        for text in (record.getMessage(), record.exc_text or "", formatted):
            assert "knee hurts" not in text


def test_sdk_log_redaction_passes_debug_records_unchanged():
    install_sdk_log_redaction()
    exc = APIError(SECRET, retryable=False)
    record = sdk_record(logging.DEBUG, "debug: %s", (SECRET,), exc=exc)
    before = (record.msg, record.args, record.exc_info, record.exc_text)
    assert logging.getLogger("livekit.agents").filter(record)
    assert (record.msg, record.args, record.exc_info, record.exc_text) == before
