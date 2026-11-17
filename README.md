# clinic-sms

A simulated-SMS clinic agent on unmodified `livekit-agents` 1.8.11: each inbound text cold-starts a session from SQLite; reception, rescheduling and a three-step intake (insurance, visit reason, allergies/meds) hand off to each other, reminders and follow-ups run as background jobs, and questions the agent cannot answer go to the front desk.

All data and phone numbers in this repo are fictional.

## Setup

```bash
uv python install 3.13
uv sync
```

Talking to Claude needs an Anthropic API key in the `ANTHROPIC_API_KEY` environment variable, the only place the code reads it from. Optionally, keep it in a `.env` file in the repo root (git-ignored) holding the line `ANTHROPIC_API_KEY=...`, and pass that file to each command with `uv run --env-file .env ...`. The tests, the offline replay and the repro scripts need no key.

## Tests

```bash
uv run pytest
```

The tests use a scripted LLM and need no key. One of them, `tests/test_recording.py::test_committed_mainline_recording_replays_offline`, replays the committed demo recording `scenarios/mainline.jsonl` offline; it fails once a prompt or tool change has made that recording stale (see "Re-recording the main line" below). `evals/` is not collected.

```bash
uv run pytest evals -s        # or: uv run --env-file .env pytest evals -s
```

A behavior eval of the demo main line against real Claude (about 35 calls, including the LLM judges). It prints each check and each judge's verdict and reasoning. Without `ANTHROPIC_API_KEY` it is skipped.

## Simulator

```bash
uv run clinic-sms
```

Type a message to send it as the current patient. The demo patient is Jane Doe, (555) 010-0001, booked Thu 11/19 10:00 AM with Dr. Lee; the simulated clock starts Tue 11/17 9:55 AM, and her 48-hour reminder is due at 10:00 AM. Commands:

| Command | What it does |
|---|---|
| `/as PHONE` | switch to another number, e.g. `/as (555) 010-0002`; the front desk is `(555) 010-0100` |
| `/start-intake` | have the clinic start the intake for the current patient |
| `/state [PHONE]` | show the conversation: active agent, appointment, intake, jobs, tickets; with no conversation yet, still the number's appointment, jobs and tickets |
| `/advance DUR` | move the simulated clock forward, e.g. `/advance 1h30m` (units `d`, `h`, `m`); each job that falls due on the way runs at its due time |
| `/time` | show the simulated time |
| `/replay FILE` | run a script file, one line at a time |
| `/restart` | re-launch the process; stored conversations, the clock and a running replay carry over (it prints `resuming <file> at line <n>`) |
| `/quit` | exit |
| `/help` | show the commands |

As the front desk, answer a ticket with `#<ticket> <answer>`, e.g. `#1 Your copay was $25.`; the patient's active agent passes the answer on. In a script file, a line starting with `# ` is a comment.

Flags:

| Flag | Default | |
|---|---|---|
| `--db PATH` | `var/clinic.db` | SQLite database; seeded with the demo data when new |
| `--as PHONE` | `(555) 010-0001` | the patient number to text as |
| `--llm live\|record\|replay` | `live` | `live` calls Claude; `record` calls Claude and appends each LLM turn to `--recording`; `replay` answers from `--recording`, with no key and no network |
| `--recording FILE` | `var/recordings/session.jsonl` | the JSONL file for `record` and `replay` |
| `--script FILE` | | run the file, print the smoke summary (`SMOKE: OK` or `SMOKE: FAIL`), exit (1 on FAIL) |

Without `ANTHROPIC_API_KEY`, the simulator in `live` mode still starts (the LLM is built per turn), but every turn fails: a patient's message gets the fallback reply, and the traceback goes to stderr.

## Demo

The main line, `scenarios/mainline.txt` (spec §2.1): the 48-hour reminder, a handoff to rescheduling, the intake, a restart and the next morning's follow-up, a regression to the insurance step, and a billing question answered by the front desk.

Live, with Claude, on a fresh database:

```bash
rm -f var/demo/live.db && uv run clinic-sms --db var/demo/live.db --script scenarios/mainline.txt
```

Offline, replaying the recorded Claude turns in `scenarios/mainline.jsonl` (no key, no network):

```bash
rm -f var/demo/replay.db && env -u ANTHROPIC_API_KEY uv run clinic-sms --db var/demo/replay.db --llm replay --recording scenarios/mainline.jsonl --script scenarios/mainline.txt
```

Replay on a fresh database, as above: an existing one carries on from its saved LLM cursor and state instead of replaying from the start. If a request differs from the recording, the run stops with `REPLAY MISMATCH ... call N ...`; N counts from 0, so it is the line with `"n": N` in the recording.

### Re-recording the main line

A replay compares each request (the context the agent sends, and the names of the tools it offers) with the recording. So any change to an agent's prompt or tools makes `scenarios/mainline.jsonl` stale: the offline replay stops with `REPLAY MISMATCH`, and so does the test `tests/test_recording.py::test_committed_mainline_recording_replays_offline`. Then re-record the main line with real Claude.

Record mode appends to an existing recording, so delete the old one first, and record on a fresh database. With `ANTHROPIC_API_KEY` set in the environment (see Setup):

```bash
rm -f scenarios/mainline.jsonl var/demo/rec.db && uv run clinic-sms --db var/demo/rec.db --llm record --recording scenarios/mainline.jsonl --script scenarios/mainline.txt
```

It must end with `SMOKE: OK (fallbacks=0, failed_jobs=0)`. Then run the offline replay above and `uv run pytest` before committing the new recording.

## Smoke scripts

Author-run with real Claude, each on a fresh database (`uv run clinic-sms --db var/smoke/<name>.db --script scenarios/<file>`); results are in [docs/findings.md](docs/findings.md) under "Manual check":

- `scenarios/plan1_intake.txt`: the intake end to end, with a mid-step restart, a regression and the summary
- `scenarios/reception.txt`: the patient texts first; restart; confirm the appointment
- `scenarios/reschedule.txt`: reception, then rescheduling, then the intake, then back to reception
- `scenarios/reminder.txt`: demo steps 1 to 3, up to the first intake question
- `scenarios/followup.txt`: stop at 5 PM, restart, the follow-up at 8 AM the next day
- `scenarios/mainline.txt`: the demo main line above

## SDK findings and repros

Each finding in [docs/findings.md](docs/findings.md) (FINDING-1 to FINDING-9) has a self-contained repro script in `repro/` (livekit-agents only, plus livekit-plugins-anthropic for FINDING-9; no key, no network):

```bash
uv run python repro/finding_1.py   # likewise finding_2.py ... finding_9.py
```

Each prints what it observed and exits 1 while the finding still reproduces, 0 once it no longer does. Workarounds in the code are marked `# SDK-FINDING-<n>`.

`repro/finding_10.py` to `finding_14.py` belong to the candidate findings of the text-channel spike ([docs/spikes/text-channel.md](docs/spikes/text-channel.md)), not yet in `docs/findings.md`. Same contract; `finding_13.py` and `finding_14.py` also need a local `livekit-server --dev`.

## Docs

- [Design](docs/superpowers/specs/2026-11-12-clinic-sms-agent-design.md)
- [Findings](docs/findings.md)
- [Text-channel spike](docs/spikes/text-channel.md): RoomIO text input/output in a real room (`spikes/text_channel/`)

## License

MIT; see [LICENSE](LICENSE).
