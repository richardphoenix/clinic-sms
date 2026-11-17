import builtins
import logging
import sqlite3
import sys
from contextlib import closing
from datetime import datetime

import pytest

from clinic_sms import cli
from clinic_sms.cli import Command, format_message_line, parse_command
from clinic_sms.clock import CLINIC_TZ
from clinic_sms.state import ConversationState, GroupProgress, IntakeState
from clinic_sms.store import Store
from clinic_sms.testing import Reply
from conftest import P

AT = datetime(2026, 11, 17, 9, 55, tzinfo=CLINIC_TZ)


def test_parse_command():
    assert parse_command("hello there") == Command("say", "hello there")
    assert parse_command("/as (555) 010-0002") == Command("as", "(555) 010-0002")
    assert parse_command("/restart") == Command("restart")
    assert parse_command("/nope") == Command("help")


def test_format_message_line():
    at = datetime(2026, 11, 19, 10, 0, tzinfo=CLINIC_TZ)
    assert format_message_line(at=at, direction="in", phone="+15550100001", text="Hi") == \
        "[Thu 11/19, 10:00 AM] (555) 010-0001 → clinic: Hi"
    assert format_message_line(at=at, direction="out", phone="+15550100001", text="Hello") == \
        "[Thu 11/19, 10:00 AM] clinic → (555) 010-0001: Hello  (5 chars, 1 seg)"


@pytest.fixture
def restore_logging():
    # main() configures logging process-wide; undo it so other tests see the defaults
    names = ["", "livekit"]
    saved = [(n, logging.getLogger(n).level) for n in names]
    handlers = list(logging.getLogger().handlers)
    yield
    for n, level in saved:
        logging.getLogger(n).setLevel(level)
    logging.getLogger().handlers[:] = handlers


def test_main_exits_cleanly_on_eof(tmp_path, monkeypatch, capsys, restore_logging):
    def raise_eof(prompt: str = "") -> str:
        raise EOFError

    def no_llm():
        raise AssertionError("the LLM must not be built when no message is handled")

    monkeypatch.setattr(builtins, "input", raise_eof)
    monkeypatch.setattr(cli, "make_claude_llm", no_llm)
    db = tmp_path / "sub" / "x.db"  # the directory does not exist yet

    assert cli.main(["--db", str(db)]) is None
    assert db.exists()
    assert capsys.readouterr().err == ""


def test_main_exits_cleanly_on_keyboard_interrupt(tmp_path, monkeypatch, restore_logging):
    def interrupt(prompt: str = "") -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(builtins, "input", interrupt)
    monkeypatch.setattr(cli, "make_claude_llm", lambda: None)

    assert cli.main(["--db", str(tmp_path / "x.db")]) is None


def feed_lines(monkeypatch, lines: list[str]) -> None:
    """input() returns `lines` one by one, then raises EOFError."""
    pending = list(lines)

    def fake_input(prompt: str = "") -> str:
        if not pending:
            raise EOFError
        return pending.pop(0)

    monkeypatch.setattr(builtins, "input", fake_input)


def store_state(db, state_json: str | None = None) -> None:
    """Store a started intake for P; then replace its JSON with `state_json`, if given."""
    with Store(db) as store:
        state = ConversationState(
            phone=P,
            intake=IntakeState(
                progress=GroupProgress(pending=["insurance"], results={}, visited=["insurance"])
            ),
        )
        store.commit_turn(
            phone=P, expected_version=None, state=state, inbound=None, outbound=[], at=AT
        )
    if state_json is not None:
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE conversations SET state_json = ?", (state_json,))


def test_state_command_reports_a_stored_state_that_does_not_parse(
    tmp_path, monkeypatch, capsys, restore_logging
):
    db = tmp_path / "x.db"
    store_state(db, '{"phone": "+15550100001", "intake": {"done": "My knee still hurts"}}')
    feed_lines(monkeypatch, ["/state", "/state"])
    monkeypatch.setattr(cli, "make_claude_llm", lambda: None)

    assert cli.main(["--db", str(db)]) is None  # no crash; EOF ends the loop
    out = capsys.readouterr().out
    assert out.count("error: the stored state for this number does not parse") == 2
    assert "knee" not in out


@pytest.mark.parametrize("started", [True, False], ids=["already-started", "no-key"])
def test_start_intake_without_a_reply_prints_a_note(
    tmp_path, monkeypatch, capsys, caplog, restore_logging, started
):
    db = tmp_path / "x.db"
    if started:
        store_state(db)

    def no_key():
        raise ValueError("Anthropic API key is required")  # what the plugin raises

    feed_lines(monkeypatch, ["/start-intake"])
    monkeypatch.setattr(cli, "make_claude_llm", no_key)

    assert cli.main(["--db", str(db)]) is None
    assert "note: intake already started, or it failed — see stderr" in capsys.readouterr().out
    failures = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(failures) == (0 if started else 1)


def capture_prompts(monkeypatch, lines: list[str]) -> list[str]:
    """Like feed_lines, but records the prompt passed to input()."""
    pending = list(lines)
    prompts: list[str] = []

    def fake_input(prompt: str = "") -> str:
        prompts.append(prompt)
        if not pending:
            raise EOFError
        return pending.pop(0)

    monkeypatch.setattr(builtins, "input", fake_input)
    return prompts


class _Exec(Exception):
    pass


def test_prompt_shows_sim_time_and_identity(tmp_path, monkeypatch, restore_logging):
    prompts = capture_prompts(monkeypatch, [])
    monkeypatch.setattr(cli, "make_claude_llm", lambda: None)
    assert cli.main(["--db", str(tmp_path / "x.db")]) is None
    assert prompts[0] == "[Tue 11/17, 9:55 AM] (555) 010-0001> "


def script_the_reminder(monkeypatch, scripted_llm) -> None:
    """Advancing past Tue 10:00 AM runs the seeded reminder: give it a scripted LLM."""
    llm = scripted_llm({}, default=Reply(text="Reminder: Thu 11/19, 10:00 AM with Dr. Lee."))
    monkeypatch.setattr(cli, "make_claude_llm", lambda: llm)


def test_advance_and_time_survive_a_restart(
    tmp_path, monkeypatch, capsys, restore_logging, scripted_llm
):
    db = str(tmp_path / "x.db")
    script_the_reminder(monkeypatch, scripted_llm)
    feed_lines(monkeypatch, ["/advance 1h", "/time"])
    assert cli.main(["--db", db]) is None
    assert "Tue 11/17, 10:55 AM" in capsys.readouterr().out
    feed_lines(monkeypatch, ["/time"])
    assert cli.main(["--db", db]) is None
    assert "Tue 11/17, 10:55 AM" in capsys.readouterr().out


def test_bad_duration_and_bad_as_change_nothing(tmp_path, monkeypatch, capsys, restore_logging):
    monkeypatch.setattr(cli, "make_claude_llm", lambda: None)
    prompts = capture_prompts(monkeypatch, ["/advance soon", "/as 12345", "/time"])
    assert cli.main(["--db", str(tmp_path / "x.db")]) is None
    out = capsys.readouterr().out
    assert sum(1 for ln in out.splitlines() if ln.startswith("error:")) == 2
    assert "Tue 11/17, 9:55 AM" in out.splitlines()[-1]
    assert all(p == "[Tue 11/17, 9:55 AM] (555) 010-0001> " for p in prompts)


def test_advance_too_far_is_an_error_and_changes_nothing(
    tmp_path, monkeypatch, capsys, restore_logging
):
    # too large for a timedelta; and a timedelta past the last date a datetime holds
    monkeypatch.setattr(cli, "make_claude_llm", lambda: None)
    feed_lines(monkeypatch, ["/advance 99999999999d", "/advance 999999999d", "/time"])
    db = tmp_path / "x.db"
    assert cli.main(["--db", str(db)]) is None
    out = capsys.readouterr().out.splitlines()
    assert out == ["error: that is too far ahead"] * 2 + ["Tue 11/17, 9:55 AM"]
    with Store(db) as store:
        assert store.get_setting("sim_now") == "2026-11-17T17:55:00+00:00"


def test_restart_closes_store_then_execs(tmp_path, monkeypatch, restore_logging):
    db = str(tmp_path / "x.db")
    events: list = []
    real_close = Store.close

    def close(self):
        events.append("close")
        real_close(self)

    def execv(path, argv):
        events.append(("execv", argv))
        raise _Exec

    monkeypatch.setattr(Store, "close", close)
    monkeypatch.setattr(cli.os, "execv", execv)
    monkeypatch.setattr(cli, "make_claude_llm", lambda: None)
    feed_lines(monkeypatch, ["/restart"])
    with pytest.raises(_Exec):
        cli.main(["--db", db])
    argv = [
        sys.executable, "-m", "clinic_sms.cli", "--db", db, "--as", P,
        "--llm", "live", "--recording", "var/recordings/session.jsonl",
    ]
    assert ("execv", argv) in events
    assert events.index("close") < events.index(("execv", argv))


def test_script_resumes_after_restart(
    tmp_path, monkeypatch, capsys, restore_logging, scripted_llm
):
    db = str(tmp_path / "x.db")
    script = tmp_path / "s.txt"
    script.write_text("/time\n/restart\n/advance 30m\n/time\n")

    def execv(path, argv):
        raise _Exec

    monkeypatch.setattr(cli.os, "execv", execv)
    script_the_reminder(monkeypatch, scripted_llm)
    with pytest.raises(_Exec):
        cli.main(["--db", db, "--script", str(script)])
    capsys.readouterr()
    feed_lines(monkeypatch, [])
    assert cli.main(["--db", db]) is None
    assert "Tue 11/17, 10:25 AM" in capsys.readouterr().out
    with Store(db) as s:
        assert s.get_setting("replay_file") is None


def test_script_prints_smoke_summary_and_fails_on_fallback(
    tmp_path, monkeypatch, capsys, restore_logging
):
    db = tmp_path / "x.db"
    store_state(db)
    script = tmp_path / "s.txt"
    script.write_text("hello\n")

    def no_key():
        raise ValueError("Anthropic API key is required")

    monkeypatch.setattr(cli, "make_claude_llm", no_key)
    with pytest.raises(SystemExit) as exc:
        cli.main(["--db", str(db), "--script", str(script)])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "SMOKE: FAIL" in out and "fallbacks=1" in out


@pytest.mark.parametrize("ok", [True, False], ids=["ok", "fallback"])
def test_script_quit_still_prints_the_smoke_summary(
    tmp_path, monkeypatch, capsys, restore_logging, ok
):
    db = tmp_path / "x.db"
    store_state(db)
    script = tmp_path / "s.txt"
    script.write_text(("/time" if ok else "hello") + "\n/quit\n/advance 1h\n")

    def no_key():
        raise ValueError("Anthropic API key is required")

    monkeypatch.setattr(cli, "make_claude_llm", no_key)
    if ok:
        assert cli.main(["--db", str(db), "--script", str(script)]) is None
    else:
        with pytest.raises(SystemExit) as exc:
            cli.main(["--db", str(db), "--script", str(script)])
        assert exc.value.code == 1
    out = capsys.readouterr().out
    fallbacks = 0 if ok else 1
    summary = f"SMOKE: {'OK' if ok else 'FAIL'} (fallbacks={fallbacks}, failed_jobs=0)"
    assert out.splitlines()[-1] == summary
    assert "/advance 1h" not in out  # nothing after /quit runs
    with Store(db) as s:
        assert s.get_setting("replay_file") is None


def test_startup_delivery_failure_is_logged_not_raised(
    tmp_path, monkeypatch, caplog, restore_logging
):
    async def boom(self):
        raise RuntimeError("x")

    monkeypatch.setattr(cli.ConversationRunner, "deliver_pending", boom)
    monkeypatch.setattr(cli, "make_claude_llm", lambda: None)
    feed_lines(monkeypatch, [])
    assert cli.main(["--db", str(tmp_path / "x.db")]) is None
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    msg = errors[0].getMessage()
    assert "RuntimeError" in msg and "x" not in msg.replace("RuntimeError", "")
    assert errors[0].exc_info is None


def test_new_db_is_seeded(tmp_path, monkeypatch, restore_logging):
    db = tmp_path / "x.db"
    feed_lines(monkeypatch, [])
    monkeypatch.setattr(cli, "make_claude_llm", lambda: None)

    assert cli.main(["--db", str(db)]) is None
    with Store(db) as store:
        assert store.clinic.patient_by_phone(P).label == "Jane D."


def test_start_intake_as_the_front_desk_is_an_error(
    tmp_path, monkeypatch, capsys, caplog, restore_logging
):
    def no_llm():
        raise AssertionError("no LLM call for the front desk's /start-intake")

    monkeypatch.setattr(cli, "make_claude_llm", no_llm)
    feed_lines(monkeypatch, ["/as (555) 010-0100", "/start-intake", "/state"])
    db = tmp_path / "x.db"
    assert cli.main(["--db", str(db)]) is None
    out = capsys.readouterr().out.splitlines()
    errors = [ln for ln in out if ln.startswith("error:")]
    assert errors == [
        "error: the front desk has no intake; switch to a patient first, e.g. /as (555) 010-0001"
    ]
    assert out[-1] == "no conversation for this number"
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


def test_resuming_a_replay_at_startup_says_where(tmp_path, monkeypatch, capsys, restore_logging):
    db = str(tmp_path / "x.db")
    script = tmp_path / "s.txt"
    script.write_text("/time\n/restart\n# a comment\n/time\n")

    def execv(path, argv):
        raise _Exec

    monkeypatch.setattr(cli.os, "execv", execv)
    monkeypatch.setattr(cli, "make_claude_llm", lambda: None)
    with pytest.raises(_Exec):
        cli.main(["--db", db, "--script", str(script)])
    assert "resuming" not in capsys.readouterr().out  # a new replay starts silently
    feed_lines(monkeypatch, [])
    assert cli.main(["--db", db]) is None
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"resuming {script.resolve()} at line 3"
    assert out[-1] == "SMOKE: OK (fallbacks=0, failed_jobs=0)"
    # nothing left to resume
    feed_lines(monkeypatch, [])
    assert cli.main(["--db", db]) is None
    assert "resuming" not in capsys.readouterr().out
