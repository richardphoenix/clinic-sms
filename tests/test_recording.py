import json
import logging
from pathlib import Path

import pytest
from livekit.agents.llm import ChatContext, FunctionCall

from clinic_sms import cli
from clinic_sms.channel import RecordingChannel
from clinic_sms.recording import RecordReplayLLM, request_fingerprint
from clinic_sms.store import Store
from clinic_sms.testing import Reply
from conftest import P
from test_cli import _Exec, feed_lines, restore_logging  # noqa: F401

FIRST = "Aetna, member W123456789"
SECOND = "Follow-up on my knee"
SCENARIOS = Path(__file__).resolve().parent.parent / "scenarios"
MAINLINE_RECORDING = SCENARIOS / "mainline.jsonl"
MAINLINE_SCRIPT = SCENARIOS / "mainline.txt"


def lines_of(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text().splitlines()]


async def drive(make_runner, db: Path, llm, answers: list[str]) -> list[str]:
    """start_intake, then each answer, on one database; return every text sent."""
    ch = RecordingChannel()
    runner = make_runner(db, llm, ch)
    await runner.start_intake(P)
    for answer in answers:
        await runner.handle_inbound(P, answer)
    return [m.text for m in ch.sent]


async def record(make_runner, scripted_llm, intake_replies, tmp_path, answers) -> tuple[Path, list]:
    path = tmp_path / "rec.jsonl"
    recorder = RecordReplayLLM(
        mode="record", path=path, inner_factory=lambda: scripted_llm(intake_replies)
    )
    sent = await drive(make_runner, tmp_path / "db1", recorder, answers)
    return path, sent


def test_fingerprint_is_names_and_normalized_ctx():
    ctx = ChatContext.empty()
    ctx.add_message(role="user", content="hi")
    ctx.insert(FunctionCall(call_id="c", name="t", arguments='{"b":1,"a":2}'))
    assert request_fingerprint(ctx, []) == {
        "ctx": [["message", "user", "hi"], ["function_call", "t", '{"a":2,"b":1}']],
        "tools": [],
    }


def test_fingerprint_ignores_tool_call_ids_written_into_message_text():
    # the SDK's summarizer inlines call ids; a replay hands out other ids than Claude did
    def ctx(call_id: str) -> ChatContext:
        c = ChatContext.empty()
        c.add_message(
            role="user",
            content=f'<function_call name="t" call_id="{call_id}">\n{{}}\n</function_call>',
        )
        return c

    assert request_fingerprint(ctx("toolu_01ABC"), []) == request_fingerprint(ctx("replay_8_0"), [])


async def test_record_then_replay_gives_the_same_turns(
    tmp_path, scripted_llm, intake_replies, make_runner, seeded_db
):
    path, recorded = await record(
        make_runner, scripted_llm, intake_replies, tmp_path, [FIRST, SECOND]
    )
    rows = lines_of(path)
    assert [r["n"] for r in rows] == list(range(len(rows))) and len(rows) >= 3
    assert set(rows[0]) == {"n", "request", "response"}
    assert set(rows[0]["response"]) == {"text", "tools"}
    assert any(r["response"]["tools"] for r in rows)

    seen: list[int] = []
    replayer = RecordReplayLLM(mode="replay", path=path, on_cursor=seen.append)
    replayed = await drive(make_runner, tmp_path / "db2", replayer, [FIRST, SECOND])
    assert replayed == recorded
    assert replayer.mismatch is None
    assert replayer.cursor == len(rows) and seen[-1] == len(rows)


async def test_record_appends_and_starts_at_the_existing_line_count(
    tmp_path, scripted_llm, intake_replies, make_runner
):
    path, _ = await record(make_runner, scripted_llm, intake_replies, tmp_path, [])
    count = len(lines_of(path))
    again = RecordReplayLLM(
        mode="record", path=path, inner_factory=lambda: scripted_llm(intake_replies)
    )
    assert again.cursor == count


async def test_record_writes_nothing_when_the_inner_call_fails(tmp_path, scripted_llm, make_runner):
    from livekit.agents import APIError

    path = tmp_path / "rec.jsonl"
    inner = scripted_llm({}, default=Reply(error=APIError("down", retryable=False)))
    recorder = RecordReplayLLM(mode="record", path=path, inner_factory=lambda: inner)
    await drive(make_runner, tmp_path / "db1", recorder, [])  # the turn falls back
    assert not path.exists() or path.read_text() == ""
    assert recorder.cursor == 0


async def test_replay_mismatch_is_loud_and_carries_no_text(
    tmp_path, scripted_llm, intake_replies, make_runner, caplog
):
    caplog.set_level(logging.WARNING)  # what the CLI lets through; the SDK's DEBUG holds text
    path, _ = await record(make_runner, scripted_llm, intake_replies, tmp_path, [FIRST])
    other = "Blue Cross, member Z987654321"
    replayer = RecordReplayLLM(mode="replay", path=path)
    sent = await drive(make_runner, tmp_path / "db2", replayer, [other])
    assert replayer.mismatch is not None
    assert replayer.mismatch.startswith("replay mismatch at call 1: request differs at item ")
    assert len(sent) == 2  # the opening, then the fallback
    everything = replayer.mismatch + "".join(str(vars(r)) for r in caplog.records)
    for text in (FIRST, other, "W123456789", "Z987654321", "Blue Cross"):
        assert text not in everything


async def test_replay_past_the_recording_is_a_mismatch(
    tmp_path, scripted_llm, intake_replies, make_runner
):
    path, _ = await record(make_runner, scripted_llm, intake_replies, tmp_path, [])
    count = len(lines_of(path))
    replayer = RecordReplayLLM(mode="replay", path=path)
    await drive(make_runner, tmp_path / "db2", replayer, [FIRST])
    assert replayer.mismatch == f"replay mismatch at call {count}: the recording has no such call"


async def test_replay_with_a_missing_file_is_a_mismatch(tmp_path, make_runner):
    replayer = RecordReplayLLM(mode="replay", path=tmp_path / "none.jsonl")
    await drive(make_runner, tmp_path / "db", replayer, [])
    assert replayer.mismatch == "replay mismatch at call 0: the recording has no such call"


def run_cli(db: Path, rec: Path, script: Path, mode: str) -> None:
    """Run the CLI on `script`, and again after each real /restart until it finishes."""
    args = ["--db", str(db), "--llm", mode, "--recording", str(rec)]
    try:
        cli.main([*args, "--script", str(script)])
    except _Exec:
        cli.main(args)


def test_replay_cursor_survives_a_real_restart(
    tmp_path, monkeypatch, capsys, restore_logging, scripted_llm, intake_replies  # noqa: F811
):
    script = tmp_path / "s.txt"
    script.write_text(f"/start-intake\n{FIRST}\n/restart\n{SECOND}\n")
    rec = tmp_path / "rec.jsonl"

    def execv(path, argv):
        raise _Exec

    monkeypatch.setattr(cli.os, "execv", execv)
    feed_lines(monkeypatch, [])

    llm = scripted_llm(intake_replies)
    monkeypatch.setattr(cli, "make_claude_llm", lambda: llm)
    run_cli(tmp_path / "rec.db", rec, script, "record")
    recorded_out = capsys.readouterr().out
    count = len(lines_of(rec))
    assert "SMOKE: OK" in recorded_out

    def no_claude():
        raise AssertionError("replay must not build the Claude LLM")

    monkeypatch.setattr(cli, "make_claude_llm", no_claude)
    run_cli(tmp_path / "play.db", rec, script, "replay")
    assert capsys.readouterr().out == recorded_out
    with Store(tmp_path / "play.db") as store:
        assert store.get_setting("llm_cursor") == str(count)
    assert len(lines_of(rec)) == count  # replay never writes


def test_cli_exits_2_on_a_replay_mismatch(
    tmp_path, monkeypatch, capsys, restore_logging  # noqa: F811
):
    db = tmp_path / "x.db"
    from test_cli import store_state

    store_state(db)
    script = tmp_path / "s.txt"
    script.write_text("hello\n")
    rec = tmp_path / "empty.jsonl"
    rec.write_text("")
    with pytest.raises(SystemExit) as exc:
        cli.main(["--db", str(db), "--llm", "replay", "--recording", str(rec),
                  "--script", str(script)])
    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert "REPLAY MISMATCH: replay mismatch at call 0" in out
    assert "SMOKE" not in out


def test_committed_mainline_recording_replays_offline(
    tmp_path, monkeypatch, capsys, restore_logging  # noqa: F811
):
    """The committed demo recording replays offline: every request matches the recording.

    The recording compares each request's context and tool names, so any change to an
    agent's prompt or tools makes it stale. If this fails after a prompt/tool change,
    re-record the main line (see README, "Re-recording the main line").
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def execv(path, argv):
        raise _Exec

    def no_claude():
        raise AssertionError("replay must not build the Claude LLM")

    monkeypatch.setattr(cli.os, "execv", execv)
    monkeypatch.setattr(cli, "make_claude_llm", no_claude)
    feed_lines(monkeypatch, [])
    db = tmp_path / "replay.db"
    code = None
    try:
        # the script's /restart ends the first run; the second carries on after it
        run_cli(db, MAINLINE_RECORDING, MAINLINE_SCRIPT, "replay")
    except SystemExit as exc:  # 2 on a replay mismatch, 1 on a failed smoke check
        code = exc.code
    out = capsys.readouterr().out
    mismatches = [ln for ln in out.splitlines() if ln.startswith("REPLAY MISMATCH")]
    assert mismatches == [], f"{mismatches[0]}: stale recording, re-record it (see README)"
    assert code is None
    assert "SMOKE: OK (fallbacks=0, failed_jobs=0)" in out
    # every recorded call was used: nothing left over from an earlier recording
    with Store(db) as store:
        assert store.get_setting("llm_cursor") == str(len(lines_of(MAINLINE_RECORDING)))
