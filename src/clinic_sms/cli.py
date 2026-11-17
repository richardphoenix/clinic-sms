"""Minimal terminal simulator: type as a patient, read the clinic's SMS replies."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from livekit.agents import llm

from clinic_sms.channel import Outbound
from clinic_sms.clock import Clock
from clinic_sms.fmt import format_phone, format_when, normalize_phone, sms_segments
from clinic_sms.jobs import Scheduler
from clinic_sms.llm import make_claude_llm
from clinic_sms.recording import RecordReplayLLM
from clinic_sms.runner import ConversationRunner
from clinic_sms.seed import seed_demo
from clinic_sms.sim import HELP_TEXT, START_INTAKE_NOTE, Simulator, load_clock  # noqa: F401
from clinic_sms.store import Store

DEFAULT_DB = "var/clinic.db"
DEFAULT_PHONE = "+15550100001"
DEFAULT_RECORDING = "var/recordings/session.jsonl"
SETTING_LLM_CURSOR = "llm_cursor"

CommandName = Literal[
    "say", "as", "start-intake", "state", "restart", "quit", "help", "advance", "time", "replay"
]
_SLASH_COMMANDS = (
    "as", "start-intake", "state", "restart", "quit", "help", "advance", "time", "replay"
)


@dataclass(frozen=True)
class Command:
    name: CommandName
    arg: str = ""


def parse_command(line: str) -> Command:
    """Parse one input line; plain text is a message, an unknown /x shows the help."""
    line = line.strip()
    if not line.startswith("/"):
        return Command("say", line)
    name, _, arg = line[1:].partition(" ")
    if name in _SLASH_COMMANDS:
        return Command(name, arg.strip())  # type: ignore[arg-type]
    return Command("help")


def format_message_line(
    *, at: datetime, direction: Literal["in", "out"], phone: str, text: str
) -> str:
    when = format_when(at)
    shown = format_phone(phone)
    if direction == "in":
        return f"[{when}] {shown} → clinic: {text}"
    return f"[{when}] clinic → {shown}: {text}  ({len(text)} chars, {sms_segments(text)} seg)"


class CliChannel:
    """Prints every outbound SMS to the operator's console."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    async def send(self, msg: Outbound) -> None:
        print(
            format_message_line(at=self._clock.now(), direction="out", phone=msg.to, text=msg.text),
            flush=True,
        )


async def _read_line(prompt: str) -> str:
    """Read one line on a daemon thread, so a blocked input() never delays shutdown.

    asyncio.to_thread would use the default executor, which asyncio.run joins on exit:
    Ctrl-C at the prompt would then hang until Enter. A daemon thread is never joined.
    Raises whatever input() raises (EOFError, KeyboardInterrupt).
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[str] = loop.create_future()

    def deliver(result: str | None, exc: BaseException | None) -> None:
        if fut.done():  # the waiter was cancelled
            return
        if exc is not None:
            fut.set_exception(exc)
        else:
            fut.set_result(result or "")

    def work() -> None:
        try:
            result, exc = input(prompt), None
        except BaseException as e:  # noqa: BLE001 - forwarded to the waiting coroutine
            result, exc = None, e
        try:
            loop.call_soon_threadsafe(deliver, result, exc)
        except RuntimeError:  # the loop is already closed
            pass

    threading.Thread(target=work, daemon=True).start()
    return await fut


def _configure_logging() -> None:
    # WARNING and above to stderr only. livekit puts message text into lk.pii.* extras
    # of DEBUG records, so no livekit DEBUG record may reach any sink.
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    logging.getLogger().setLevel(logging.WARNING)
    logging.getLogger("livekit").setLevel(logging.WARNING)


def _build_llm_factory(
    store: Store, mode: str, recording: str
) -> tuple[Callable[[], llm.LLM], RecordReplayLLM | None]:
    """The per-turn LLM factory, and the recorder behind it unless the mode is live.

    The wrapper is built once and shared by every turn; its cursor is saved in the store,
    so a replay carries on after a real /restart. Replay never builds the Claude LLM.
    """
    if mode == "live":
        return (lambda: make_claude_llm()), None
    wrapper = RecordReplayLLM(
        mode=mode,  # type: ignore[arg-type]
        path=Path(recording),
        inner_factory=(lambda: make_claude_llm()) if mode == "record" else None,
        cursor=int(store.get_setting(SETTING_LLM_CURSOR) or "0"),
        on_cursor=lambda n: store.set_setting(SETTING_LLM_CURSOR, str(n)),
    )
    return (lambda: wrapper), wrapper


async def _run(db: str, phone: str, script: str | None, llm_mode: str, recording: str) -> None:
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    store = Store(db)
    try:
        clock = load_clock(store)
        seed_demo(store, now=clock.now())
        # The LLM is built per turn, so starting up and /help need no API key.
        llm_factory, wrapper = _build_llm_factory(store, llm_mode, recording)
        runner = ConversationRunner(
            store=store,
            channel=CliChannel(clock),
            clock=clock,
            llm_factory=llm_factory,
        )
        # Jobs run only from this command loop (startup, /advance, after each command),
        # never concurrently with a message being handled.
        scheduler = Scheduler(store=store, runner=runner, clock=clock)
        sim = Simulator(
            store=store,
            clock=clock,
            runner=runner,
            scheduler=scheduler,
            phone=phone,
            out=lambda text: print(text, flush=True),
            replay_mismatch=(
                (lambda: wrapper.mismatch) if wrapper is not None and llm_mode == "replay" else None
            ),
        )
        # Once, before any input is read, so it never overlaps handle_inbound.
        await sim.startup()
        outcome = await sim.run_script(script) if script else await sim.resume_replay()
        while outcome == "continue":
            try:
                line = await _read_line(sim.prompt())
            except EOFError:
                break
            outcome = await sim.execute(parse_command(line))
        if outcome == "restart":
            print("restarting...", flush=True)
            store.close()  # os.execv skips cleanup
            os.execv(
                sys.executable,
                [
                    sys.executable, "-m", "clinic_sms.cli", "--db", db, "--as", sim.phone,
                    "--llm", llm_mode, "--recording", recording,
                ],
            )
    finally:
        store.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="clinic-sms", description="SMS clinic agent simulator")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite path (default {DEFAULT_DB})")
    parser.add_argument(
        "--as",
        dest="phone",
        default=DEFAULT_PHONE,
        help=f"patient number (default {DEFAULT_PHONE})",
    )
    parser.add_argument(
        "--llm",
        choices=["live", "record", "replay"],
        default="live",
        help="live: call Claude; record: call Claude and append each turn to --recording; "
        "replay: answer from --recording, no API key needed (default live)",
    )
    parser.add_argument(
        "--recording",
        default=DEFAULT_RECORDING,
        help=f"JSONL file for --llm record/replay (default {DEFAULT_RECORDING})",
    )
    parser.add_argument("--script", help="run this script file, print the smoke summary, exit")
    args = parser.parse_args(argv)
    try:
        phone = normalize_phone(args.phone)
    except ValueError:
        parser.error("--as must be a US phone number")
    _configure_logging()
    try:
        asyncio.run(_run(args.db, phone, args.script, args.llm, args.recording))
    except KeyboardInterrupt:
        print(flush=True)


if __name__ == "__main__":
    main()
