# Clinic SMS agent · Plan 2: handoff chain, reminders and follow-ups, escalation, simulator and record-and-replay, real-model evals

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On top of the Plan 1 cold-start engine, complete the six steps of the demo main line (design §2.1): the reception → rescheduling → intake handoff chain, the 48-hour reminder and the intake follow-up (including quiet hours), escalation to a human and relaying the staff reply, the simulator's time fast-forward, script replay and LLM record and replay, and behavior evals run against real Claude.

**Architecture:** Every turn is still one cold start. A turn can be an inbound SMS from a patient, or a proactive message triggered by a due job. The runner reads the conversation state from SQLite, rebuilds the current top-level agent according to `active` (Reception / Reschedule / Intake → TaskGroup → step), waits for the ReadySignal, and then calls `run(user_input=...)` or `generate_reply(instructions=...)`. Tools do not write to the database directly: domain writes such as rescheduling, confirming, creating a ticket and scheduling a job are recorded as effects on this turn's `Turn` (the session userdata). The runner commits these effects together with the new state, the message log and the outbox in a single transaction. The `Scheduler` runs due jobs in order against the simulated clock, and each job is also one cold-start turn.

**Tech Stack:** Same as Plan 1: Python 3.13 (uv), `livekit-agents==1.8.11`, `livekit-plugins-anthropic==1.8.11`, pydantic v2, the standard-library `sqlite3`, pytest + pytest-asyncio. One new dev dependency, `pytest-timeout`.

**Spec:** `docs/superpowers/specs/2026-11-12-clinic-sms-agent-design.md`

**Scope:** Steps 3 to 7 of design §5.12. It also handles the deferred items in `docs/superpowers/plans/2026-11-12-clinic-sms-plan-1-followups.md` that fall within the scope of this plan; where each one goes is under "Where the Plan 1 deferred items go" below.

**One deviation from the order in §5.12:** the simulated clock, `/advance`, `/time`, `/replay` and script mode from step 6 (the simulator) are pulled forward to Task 2, to serve as a smoke-check tool. The reason is that the final Plan 1 review recommended running real-model checks as early as possible: when FINDING-7 appeared, the scripted tests were all green while real Claude returned a 400. After that, every feature task ends with "the author runs a script with real Claude". The rest of step 6, the LLM record and replay, stays in its original order in Task 10.

**Interface convention:** All changes to Plan 1 code below only add optional parameters or fields, so existing call sites are unchanged: `Store.commit_turn` / `log_only` gain `effects=()`, `IntakeAgent` gains `turn=None`, and `ConversationState` gains fields with defaults. Plan 1's tests do not relax any assertion; they are tightened in only two places (both deferred items): the time limit of `test_unknown_step_in_saved_state_falls_back_fast` goes from 2 seconds to 0.5 seconds (Task 4), and `test_unscripted_input_raises` gains a `match` (Task 10).

## Global Constraints

- Python 3.13, installed through uv; `requires-python = ">=3.13,<3.15"`.
- Dependencies are pinned exactly: `livekit-agents==1.8.11` and `livekit-plugins-anthropic==1.8.11`. Official PyPI releases only: no patches, no local clone.
- Model `claude-opus-5-5`, `max_tokens=8000`, no temperature set.
- Credentials are read only from the environment variable `ANTHROPIC_API_KEY`. They are never written to any file and never requested in the conversation.
- **Every step that needs the key is run by the author in their own terminal.** The implementer writes the command, then stops and waits for the author to paste the result back. Save the output under `var/` (git-ignored) with `tee`. The implementer does not run these commands and does not ask the author for the key.
- All data is fictional. Phone numbers use only the 555-0100 to 555-0199 range, are stored internally as E.164 (e.g. `+15550100001`) and displayed as `(555) 010-0001`. Times are displayed as `Thu 11/19, 10:00 AM`, time zone `America/Los_Angeles`.
- SMS sent to patients and to the front desk are always in English.
- Fallback SMS text, verbatim: `Sorry, something went wrong on our end — please reply again.`
- Phone numbers in logs always go through `mask_phone`. Logs must not contain what the patient said, what the front desk said, or the message text of an exception raised while a turn is being handled; only the exception type name and the traceback are logged (`describe_exception`, Task 4). The CLI prints no livekit DEBUG records.
- All code that works around an SDK problem is tagged `# SDK-FINDING-<n>` and gets an entry in `docs/findings.md` in the §5.10 format, with a standalone repro script. New FINDINGs start at 8, numbered in order of discovery.
- Do not file issues or PRs against livekit/agents. Each one needs the author's separate approval and is outside this plan.
- Local commits only, no push.
- If a spike shows that some mechanism does not work on the released SDK and no workaround can be found, stop and report the conclusion to the author. Do not change the design on your own.
- Code comments are in English.
- "Now" in business logic always comes from `Clock`. The simulated clock starts at Tue 11/17/2026 9:55 AM (clinic time zone). `slots.start_at` and `jobs.due_at` are always stored as UTC ISO 8601 strings, so string order is time order. Quiet hours are computed in the clinic time zone.
- One turn = one cold-started session. The new state, message log, outbox and domain effects are committed in a single transaction, and delivery happens after the commit. On failure nothing is written: an inbound message gets the fallback SMS, while a proactive message is only logged and counted toward the job's failed attempts.

## Review Focus

1. **The LLM passes `book_slot` a slot id that does not exist, is already taken, or is in the past.** The tool should return a clear error so the LLM offers a different slot instead; the appointment, the reminder job and the current agent in the conversation all stay unchanged. Covered by `test_book_slot_rejects_unknown_taken_or_past_slot` in Task 6.
2. **One `/advance` crosses several due jobs**, e.g. `/advance 2d`. Each job runs exactly once, in due order, with message timestamps equal to their own due times, and the clock ends at the target time. Covered by `test_advance_runs_each_due_job_once_at_its_due_time` in Task 7.
3. **A due follow-up is already stale**: intake is already finished, or the conversation is no longer in intake. The job should be cancelled outright, with no LLM call and no SMS. Covered by `test_stale_followup_is_cancelled_without_llm_call` in Task 8.
4. **The front desk's reply has the wrong format, the ticket number does not exist, the ticket is already closed, or the same ticket is answered twice.** The front desk receives a fixed usage message or a "no such ticket" message; the patient receives nothing; no extra relay job appears. Covered by `test_bad_staff_replies_get_usage_or_unknown_ticket_text` in Task 9.
5. **An exception message carries the patient's own words**, e.g. a tool-argument validation failure or an LLM error that echoes the input. The logs may contain only the exception type name, the traceback and the masked phone number. Covered by `test_failure_logs_carry_no_exception_text` in Task 4.

---

## File structure

```
src/clinic_sms/
  sim.py                 NEW  Simulator: SimClock persistence, command execution, /advance, script replay and resume, smoke summary
  cli.py                 MOD  Argument parsing, input reading and execv restart only
  clinic_db.py           NEW  Clinic table schema, data classes, ClinicView (read-only queries), apply_effect
  effects.py             NEW  Domain effects (immutable dataclasses)
  jobs.py                NEW  Timing rules for reminders/follow-ups/relays, and the Scheduler
  seed.py                NEW  Demo seed data
  logsafe.py             NEW  describe_exception: type name and traceback only
  turn.py                NEW  Turn: userdata of one cold-started session
  recording.py           NEW  RecordReplayLLM (live / record / replay)
  store.py               MOD  settings, effects committed in the same transaction, job failure counting
  state.py               MOD  Conversation state gains active / agent_ctx / appointment_id / intake_followups_sent
  runner.py              MOD  Restores agents from the registry, proactive messages, follow-up effects, front-desk routing, safe logging
  llm.py                 MOD  (depends on the spike results) SDK-FINDING workaround
  ctx.py                 MOD  normalize_ctx tolerates non-JSON arguments
  agents/common.py       NEW  SMS_STYLE (moved from intake.py), escalate_to_staff
  agents/reception.py    NEW  ReceptionAgent
  agents/reschedule.py   NEW  RescheduleAgent
  agents/registry.py     NEW  new_agent / restore_agent / conversation_snapshot
  agents/intake.py       MOD  turn parameter, steps carry the escalation tool, deferred-item fixes
scenarios/               NEW  Smoke and demo scripts (*.txt), demo recording (mainline.jsonl)
evals/                   NEW  Real-model behavior evals (not in the default testpaths)
spikes/plan2_mechanics.py NEW One-off spike
```

## Where the Plan 1 deferred items go

Folded into this plan:

| Deferred item | Where it goes |
|---|---|
| T1 `requires-python` has no upper bound | Task 2 |
| T8 tests depend on the runner's internal timeout; there is no pytest-timeout | Task 2 |
| T9 argv and database-close order of `/restart` untested; bad `/as` untested; `deliver_pending` at startup not wrapped | Task 2 |
| T4 no test for rollback when "state written, a later step fails"; tests read `s._conn`; local imports inside tests | Task 3 (new tests use a second sqlite3 connection, and cover whole-turn rollback when an effect fails) |
| T8 no tests for ready timeout, run timeout, `StaleStateError`; the time limit for the unknown step relaxed to 2 seconds | Task 4 |
| T8 `log_only` / `_deliver` inside the fallback branch can themselves raise | Task 4 |
| T8 a turn that fails before commit and then errors again while closing the session has no test | Task 4 |
| T8 exceptions inside `session.run` may carry the patient's own words into the logs | Task 4 (`describe_exception`, Review Focus 5) |
| T5 `ReadySignal` is one-shot; every session needs a new one | Task 4 (a new Turn per turn, stated in the docstring) |
| T7 `_group_ctx` is not cleared after use; T8 C8-1: an explicit `_failed` flag, with `update_chat_ctx` moved into the try | Task 6 (the same `IntakeAgent.on_enter` has to change anyway) |
| T8 Ruling C8-3: deliver after commit; rows whose delivery failed stay in the outbox and are resent at the next startup; scheduling runs only in the command loop | Task 7 |
| T2 ScriptedLLM's `Reply.error/delay/default`, unique call ids and `contexts` have no tests; `test_unscripted_input_raises` has no `match` | Task 10 (together with the record-and-replay LLM tests) |
| T7 `normalize_ctx` crashes on non-JSON tool arguments | Task 10 (replay comparison needs it) |

Not folded in, left in the followups file:

- Findings wording and line-number items: T2's S4 count, the ready precondition, the FINDING-1 merge; every item under T3 claude_roundtrip; the line-number deviations in T5/T6/T8; M1–M3. These are handled when the findings are tidied up for upstream submission.
- Test gaps in code this plan does not change: several items of T5 ResumableTaskGroup, the 0.1 second sleep in the T6 shutdown test, `async with AgentSession` having no timeout.
- Not needing a change: T4's busy timeout / WAL / `check_same_thread` (this plan is still one connection per thread); T8's unbounded `_locks` (the simulator has only a few numbers); T9's `parse_command` stripping (intentional); T7's context timestamps relying on a monotonic wall clock (SimClock does not affect `created_at`, which uses real time).

---

### Task 1: Spike 4: SDK mechanics for handoffs and proactive messages

The handoff chain that follows (Task 6) and proactive messages (Tasks 7–9) depend on three things that Plan 1 did not verify:

1. When a tool returns a new agent, can the new agent's opening message be collected in the same `run()`;
2. When the new agent's `on_enter` then awaits a TaskGroup, can the innermost step's question be collected in the same `run()`;
3. After a cold start, when a proactive message is sent with `generate_reply(instructions=...)`, how are the outbound text and errors obtained.

One more thing needs to be checked with real Claude. When a tool returns `(agent, text)`, the old agent's reply is generated with `tool_choice="none"` (1.8.11 `voice/agent_activity.py` lines 4131–4134: on handoff `draining = True`; line 4184: when `draining`, the reply uses `"none"`). When the plugin sees `"none"` it sends `tools: []` (`plugins/anthropic/llm.py` lines 229–231), while the context still contains a tool_use. Whether Anthropic accepts such a request has to be tested.

**Files:**
- Create: `spikes/plan2_mechanics.py`
- Modify: `docs/findings.md` (add `## Spike 4 (Plan 2 mechanics)`)

**Interfaces:**
- Consumes: `ScriptedLLM`, `Reply` (`clinic_sms.testing`); `ReadySignal`, `ResumableTaskGroup` (`clinic_sms.workflow`); `make_claude_llm` (`clinic_sms.llm`); `GroupProgress` (`clinic_sms.state`).
- Produces: conclusions only (see the Gate in Step 4); no code is provided to later tasks.

- [ ] **Step 1: Write `spikes/plan2_mechanics.py`**

Command-line argument `--llm scripted|claude`. The script defines:

- `Probe(LLM)`: wraps the real LLM and forwards `chat()` unchanged. Each call prints one line: `tool_choice` (`default` if not passed), the names of the tools offered, and the number of `function_call` items in the context.
- `A(Agent)`: `on_enter` only does `ready.set(self)` and does not speak. Three tools:
  - `go_b()` returns `B(resuming=False)`, a handoff without a message;
  - `go_b_noted()` returns `(B(resuming=False), "moved to b")`;
  - `go_g()` returns `G()`.
- `B(Agent)`: has one tool `lookup()` that returns None. The tool exists so that the restored function_call is not filtered out by `Agent.__init__`. `on_enter` first does `ready.set(self)`; only when `resuming=False` does it call `generate_reply(instructions=ASK_B)`.
- `G(Agent)`: `on_enter` awaits a `ResumableTaskGroup(summarize_chat_ctx=False)` that contains a single step `S(AgentTask)`. S has a tool `save(value)` that returns None; S's `on_enter` calls `generate_reply(instructions=ASK_S)`.

Each check creates a new `AgentSession`, simulating a cold start: a plain `start()`, then `ready.wait`. Outbound messages are collected the way the runner does it: a `ChatMessage` with role assistant and non-empty text in `conversation_item_added`. Every `start` / `run` / `await handle` is wrapped in `asyncio.wait_for(..., 90)`.

- **H1**: start from A, `run("go b")`. Expected: the only outbound message is B's question; `current_agent` is B; the RunResult events contain an A→B `agent_handoff`.
- **H1n**: start from A, `run("go b noted")`. Expected: two outbound messages, first A's reply to "moved to b", then B's question. Print the `tool_choice` of every LLM call.
- **H2**: start from A, `run("go g")`. Expected: the only outbound message is S's question; after `run()` returns, `current_agent` is S.
- **H3**: B is restored with `resuming=True`. Its `chat_ctx` is, in order: `user "hi"`, the function_call and function_call_output of `lookup`, `assistant "What is B?"`. Once ready:

  ```python
  handle = session.generate_reply(instructions=NUDGE)
  await asyncio.wait_for(handle, 90)
  ```

  Expected: the only outbound message is the one nudge; `handle.exception() is None`; the last message in B's `chat_ctx` is this nudge.
- **H3n**: same as H3, but with `generate_reply(..., tool_choice="none")`.
- **H3e** (scripted mode only): same as H3, but NUDGE's scripted reply is `Reply(error=APIError("down", retryable=False))`. Record three things: the type of `handle.exception()` (or None), whether the outbound list is empty, and whether the SDK logged an ERROR.

Replies in scripted mode:

```python
{"go b": Reply(tools=[("go_b", {})]), ASK_B: Reply(text="What is B?"),
 "go b noted": Reply(tools=[("go_b_noted", {})]), "moved to b": Reply(text="Moving you to b."),
 "go g": Reply(tools=[("go_g", {})]), ASK_S: Reply(text="What is S?"),
 NUDGE: Reply(text="Still there? What is B?")}
```

In claude mode, A's instructions state three rules: "if the user writes `go b`, call `go_b`; if they write `go b noted`, call `go_b_noted`; if they write `go g`, call `go_g`; do not reply with text before calling a tool". `ASK_B`, `ASK_S` and `NUDGE` are replaced with real questions, e.g. asking for a color, asking for a city, or a nudge.

Each check prints `PASS` / `FAIL` / `OBSERVED` and what was seen, and at the end prints `SUMMARY`.

- [ ] **Step 2: Run scripted mode**

Run: `uv run python spikes/plan2_mechanics.py --llm scripted`
Expected: H1, H1n, H2, H3 and H3n are all PASS; H3e prints what it observed.

- [ ] **Step 3: The author runs it with real Claude (the implementer does not run it)**

Tell the author: about 12 Claude calls. Command:

```bash
mkdir -p var/spikes && uv run python spikes/plan2_mechanics.py --llm claude 2>&1 | tee var/spikes/plan2_mechanics.log
```

Expected: H1, H2 and H3 are all PASS. H1n and H3n either PASS or the verbatim 400 is recorded.

- [ ] **Step 4: Write the conclusions into `## Spike 4 (Plan 2 mechanics)` in `docs/findings.md`**

Follow the format of Spikes 1–3: date, SDK version, setup, the result of each check in both modes, and the `tool_choice` of each H1n call. The conclusions must answer three questions:

1. Can the runner collect all outbound messages along the handoff chain with a single `run()` (H1, H2)?
2. How is a proactive-message failure detected: by looking at `handle.exception()`, or only through "outbound is empty" (H3, H3e)? Task 7 does both checks; state here which one actually takes effect.
3. Does Anthropic accept a request with "`tools: []` plus a tool_use in the context" (the real results of H1n and H3n)? If it returns a 400, record it as a candidate for a new FINDING, and Task 6 Step 0 takes effect.

**Gate:** If any of H1, H2, H3 fails in either mode, first try a simple variant: after `run()` returns, also `await session.wait_for_idle()`. If the variant passes, write it into the conclusions and Tasks 6 and 7 follow it; if the variant also fails, stop and report to the author, because Tasks 6–9 all depend on these three points.

- [ ] **Step 5: Commit**

```bash
git add spikes/plan2_mechanics.py docs/findings.md
git commit -m "test: spike handoff capture and proactive replies (Spike 4)"
```

---

### Task 2: Simulator core: simulated clock, script replay and smoke check

**Files:**
- Create: `src/clinic_sms/sim.py`, `scenarios/plan1_intake.txt`, `tests/test_sim.py`
- Modify: `src/clinic_sms/store.py`, `src/clinic_sms/cli.py`, `tests/test_cli.py`, `pyproject.toml`, `docs/findings.md`

**Interfaces:**
- Consumes: `Store`, `ConversationRunner`, `FALLBACK_TEXT`, `SimClock`, `CLINIC_TZ`, `format_when`, `format_phone`, `normalize_phone`, `make_claude_llm`; `Command`, `parse_command`, `format_message_line`, `CliChannel` (cli.py).
- Produces:

```python
# store.py — new table: settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)
Store.get_setting(self, key: str) -> str | None
Store.set_setting(self, key: str, value: str | None) -> None   # None deletes the key
Store.outbound_count(self, text: str) -> int                    # outbound messages, any phone, equal to text

# sim.py
SIM_START = datetime(2026, 11, 17, 9, 55, tzinfo=CLINIC_TZ)
def parse_duration(text: str) -> timedelta   # "30m" "14h" "2d" "1h30m"; ValueError if empty, zero, negative or unknown unit
def is_comment(line: str) -> bool             # "#" then a space or the end of the line; "#1 answer" is NOT a comment
def load_clock(store: Store) -> SimClock      # setting "sim_now" (UTC ISO), else SIM_START (and saves it)
def save_clock(store: Store, clock: SimClock) -> None
class Simulator:
    def __init__(self, *, store: Store, clock: SimClock, runner: ConversationRunner,
                 phone: str, out: Callable[[str], None]) -> None
    phone: str                                   # current identity (E.164)
    def prompt(self) -> str                      # "[Tue 11/17, 9:55 AM] (555) 010-0001> "
    async def startup(self) -> None              # deliver_pending; failure is logged (type name only), not raised
    async def execute(self, cmd: Command) -> Literal["continue", "quit", "restart"]
    async def advance(self, delta: timedelta) -> None   # this task: move the clock and save it
    def smoke_summary(self) -> tuple[bool, str]  # (ok, "SMOKE: OK (fallbacks=0)" | "SMOKE: FAIL (fallbacks=2)")

# cli.py
CommandName = Literal["say", "as", "start-intake", "state", "restart", "quit", "help",
                      "advance", "time", "replay"]
# main() flags: --db, --as (as before), --script FILE (= /replay FILE, then print the smoke summary and exit 1 unless ok)
```

New commands:
- `/advance <duration>`: fast-forward.
- `/time`: prints `format_when(now)`.
- `/replay <file>`: executes a script file line by line.
- `/state [phone]`: with no argument, shows the current number.

The `/restart` argv is unchanged: `[sys.executable, "-m", "clinic_sms.cli", "--db", db, "--as", phone]`.

**Script replay and resume**: progress is stored in settings, under the keys `replay_file`, `replay_next` (index of the next line) and `replay_exit` (`"1"` in `--script` mode).

- **Before** each line is executed, increment `replay_next` and save it. So when a `/restart` is hit, the new process started by `execv` continues from the next line.
- On startup, if `replay_file` is set, resume automatically.
- Blank lines and lines for which `is_comment` is true are skipped. A comment must be `#` followed by a space or by the end of the line, because the front desk's ticket reply format `#12 ...` also starts with `#` (Task 9). Before each line is executed it is echoed as `prompt() + line`.
- When the file finishes, clear the three keys. If `replay_exit` is `"1"`, print the text of `smoke_summary()`, and raise `SystemExit(1)` if it is not ok.
- Sending `/replay` while a replay is already running prints `error: a replay is already running`.

A `/restart` under `--script` is also a real `execv`, same as in interactive mode.

- [ ] **Step 1: Change the dependencies and pytest configuration in `pyproject.toml`**

`requires-python = ">=3.13,<3.15"`. Run `uv add --dev pytest-timeout` and add `timeout = 120` to `[tool.pytest.ini_options]`.

If `uv add` fails because the uv version is too old, stop and ask the author whether to run `uv self update`; do not upgrade on your own.

- [ ] **Step 2: Write the failing tests**

`tests/test_sim.py`:

```python
@pytest.mark.parametrize("text,expected", [("30m", timedelta(minutes=30)), ("14h", timedelta(hours=14)),
                                           ("2d", timedelta(days=2)), ("1h30m", timedelta(minutes=90))])
def test_parse_duration(text, expected): assert parse_duration(text) == expected

@pytest.mark.parametrize("text", ["", "0m", "-5m", "5", "5x", "h", "1h-30m"])
def test_parse_duration_rejects(text):
    with pytest.raises(ValueError): parse_duration(text)

def test_is_comment():
    assert is_comment("# step 1") and is_comment("#")
    assert not is_comment("#1 That was your copay.") and not is_comment("hello")

def test_clock_starts_at_sim_start_and_persists(tmp_path):
    with Store(tmp_path / "db") as s:
        c = load_clock(s); assert c.now() == SIM_START
        c.advance(timedelta(hours=1)); save_clock(s, c)
    with Store(tmp_path / "db") as s:
        assert load_clock(s).now() == SIM_START + timedelta(hours=1)
```

Append the following tests to `tests/test_cli.py`. Use the existing `feed_lines` and `restore_logging`; the `input()` prompt argument is recorded by a fake `input`.

```python
def test_prompt_shows_sim_time_and_identity(...):     # first prompt == "[Tue 11/17, 9:55 AM] (555) 010-0001> "
def test_advance_and_time_survive_a_restart(...):     # run 1: ["/advance 1h", "/time"]; run 2: ["/time"]
    # both outputs contain "Tue 11/17, 10:55 AM"
def test_bad_duration_and_bad_as_change_nothing(...): # ["/advance soon", "/as 12345", "/time"] -> two "error:" lines;
    # time still 9:55 AM; prompt identity unchanged
def test_restart_closes_store_then_execs(...):        # monkeypatch os.execv to record argv and raise _Exec;
    # Store.close was called before execv; argv == [sys.executable, "-m", "clinic_sms.cli", "--db", db, "--as", P]
def test_script_resumes_after_restart(...):           # script: ["/time", "/restart", "/advance 30m", "/time"];
    # run 1 (--script f) raises _Exec after line 2; run 2 (plain --db) prints "Tue 11/17, 10:25 AM" and exits;
    # afterwards get_setting("replay_file") is None
def test_script_prints_smoke_summary_and_fails_on_fallback(...):
    # store_state(db) (intake started); script ["hello"]; make_claude_llm raises ValueError (no key)
    # -> SystemExit(1); out contains "SMOKE: FAIL" and "fallbacks=1" (Task 7 appends failed_jobs to this line)
def test_startup_delivery_failure_is_logged_not_raised(...):
    # monkeypatch ConversationRunner.deliver_pending to raise RuntimeError("x")
    # -> main returns None; one ERROR record whose message contains "RuntimeError" and not "x"
```

- [ ] **Step 3: Confirm they fail**

Run: `uv run pytest tests/test_sim.py tests/test_cli.py -q`
Expected: the new tests fail (ImportError, or assertion failures).

- [ ] **Step 4: Implement `sim.py`, settings and `outbound_count`, and turn `cli.py` into a thin shell**

- The CLI clock becomes the SimClock returned by `load_clock(store)`, and `CliChannel` uses this clock.
- `Simulator.advance` calls `save_clock` after every advance.
- The output of `/state` stays the same as in Plan 1 for now.
- The help text lists all commands.
- The name `cli.make_claude_llm` must be kept; the tests monkeypatch it.

- [ ] **Step 5: Confirm they pass**

Run: `uv run pytest -q`
Expected: all PASS, including the original Plan 1 CLI tests.

- [ ] **Step 6: Write the smoke script `scenarios/plan1_intake.txt`**

```
# Plan 1 intake end to end, with a mid-step restart, a regression and the summary (author-run smoke)
/start-intake
Aetna
W123456789
/restart
Follow-up on my knee
Actually my member ID is W999
Aetna, W999
Penicillin; ibuprofen
/state
```

- [ ] **Step 7: The author runs it with real Claude (the implementer does not run it)**

This is the first time intake is run all the way to the end with real Claude, passing through the TaskGroup's summary call and `COMPLETION`; the Plan 1 manual check stopped before step 3. About 12 Claude calls.

```bash
mkdir -p var/smoke && rm -f var/smoke/plan1.db && uv run clinic-sms --db var/smoke/plan1.db --script scenarios/plan1_intake.txt 2>&1 | tee var/smoke/plan1.log
```

Expected:
- the last line is `SMOKE: OK (fallbacks=0)`;
- after `/restart` it keeps asking, without re-asking the steps already answered;
- the last outbound message is the closing message;
- `/state` shows `done: true`, and `member_id` is `W999`.

The implementer writes the result into `### Task 2` under a new `## Manual check (Plan 2)` in `docs/findings.md`. If a fallback SMS or a 400 appears, record a FINDING per §5.10.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml uv.lock src/clinic_sms/sim.py src/clinic_sms/store.py src/clinic_sms/cli.py tests/test_sim.py tests/test_cli.py scenarios/plan1_intake.txt docs/findings.md
git commit -m "feat: simulated clock, script replay with restart, smoke summary"
```

---

### Task 3: Clinic data, effects and atomic commit

**Files:**
- Create: `src/clinic_sms/effects.py`, `src/clinic_sms/clinic_db.py`, `src/clinic_sms/jobs.py` (this task writes only the reminder-related constants and functions), `src/clinic_sms/seed.py`, `tests/test_clinic_db.py`
- Modify: `src/clinic_sms/store.py`, `src/clinic_sms/cli.py` (auto-seed when starting on a new database), `tests/conftest.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `Store`, `OutboxRow` (Plan 1); `CLINIC_TZ`, `format_phone`, `format_when`.
- Produces:

```python
# effects.py
JobKind = Literal["reminder", "intake_followup", "staff_relay"]
@dataclass(frozen=True) class ConfirmAppointment: appointment_id: int
@dataclass(frozen=True) class BookSlot: appointment_id: int; slot_id: int
@dataclass(frozen=True) class SaveIntakeResults: appointment_id: int; results: dict[str, dict]
@dataclass(frozen=True) class CreateTicket: phone: str; question: str; patient_label: str | None
@dataclass(frozen=True) class AnswerTicket: ticket_id: int; answer: str
@dataclass(frozen=True) class CloseTicket: ticket_id: int
@dataclass(frozen=True) class ScheduleJob: kind: JobKind; phone: str; due_at: datetime; payload: dict
@dataclass(frozen=True) class CancelJobs: phone: str; kinds: tuple[JobKind, ...]
@dataclass(frozen=True) class FinishJob: job_id: int; status: Literal["done", "cancelled"] = "done"
Effect = (ConfirmAppointment | BookSlot | SaveIntakeResults | CreateTicket | AnswerTicket
          | CloseTicket | ScheduleJob | CancelJobs | FinishJob)

# clinic_db.py
STAFF_PHONE = "+15550100100"
MAX_JOB_ATTEMPTS = 3
class SlotTakenError(Exception): ...
class EffectConflictError(Exception): ...   # unknown appointment, ticket not in the needed status, job not pending
@dataclass(frozen=True) class Patient: id: int; phone: str; first_name: str; last_name: str
    # property label -> "Jane D."
@dataclass(frozen=True) class Slot: id: int; provider_id: int; provider: str; start_at: datetime; taken: bool
@dataclass(frozen=True) class Appointment: id: int; patient_id: int; phone: str; slot_id: int; provider_id: int
    provider: str; start_at: datetime; status: Literal["booked", "confirmed"]; intake: dict | None
@dataclass(frozen=True) class Ticket: id: int; phone: str; question: str
    status: Literal["open", "answered", "closed"]; answer: str | None
@dataclass(frozen=True) class Job: id: int; kind: JobKind; phone: str; due_at: datetime; payload: dict
    status: Literal["pending", "done", "cancelled", "failed"]; attempts: int
SCHEMA: str
class ClinicView:                      # read-only queries on the store's connection
    def __init__(self, conn: sqlite3.Connection) -> None
    def patient_by_phone(self, phone: str) -> Patient | None
    def next_appointment(self, patient_id: int, now: datetime) -> Appointment | None   # earliest start >= now
    def appointment(self, appointment_id: int) -> Appointment | None
    def slot(self, slot_id: int) -> Slot | None
    def open_slots(self, *, start: datetime, end: datetime, provider_id: int | None, limit: int) -> list[Slot]
        # not taken, start <= start_at < end, ordered by (start_at, provider_id)
    def ticket(self, ticket_id: int) -> Ticket | None
    def tickets(self, phone: str) -> list[Ticket]
    def due_jobs(self, until: datetime) -> list[Job]           # pending, due_at <= until, ordered by (due_at, id)
    def jobs(self, phone: str) -> list[Job]                    # every status, ordered by id
    def failed_job_count(self) -> int
def apply_effect(conn: sqlite3.Connection, effect: Effect, *, at: datetime) -> list[tuple[str, str]]
    # runs inside the caller's transaction; returns the (phone, text) SMS the effect adds

# store.py
Store.clinic -> ClinicView                                     # property
Store.commit_turn(..., effects: Sequence[Effect] = ()) -> list[OutboxRow]   # effects applied after the state write
Store.log_only(..., effects: Sequence[Effect] = ()) -> list[OutboxRow]
Store.apply(self, effects: Sequence[Effect], *, at: datetime) -> list[OutboxRow]
Store.record_job_failure(self, job_id: int, *, at: datetime) -> Job   # attempts + 1; "failed" at MAX_JOB_ATTEMPTS

# jobs.py (this task)
REMINDER_LEAD = timedelta(hours=48)
def reminder_effects(*, phone: str, appointment_id: int, start_at: datetime, now: datetime) -> list[Effect]
    # [CancelJobs(phone, ("reminder",))] + [ScheduleJob("reminder", phone, start_at - REMINDER_LEAD,
    #   {"appointment_id": appointment_id})] if start_at - REMINDER_LEAD > now

# seed.py
JANE_PHONE = "+15550100001"
SEED_APPOINTMENT_AT = datetime(2026, 11, 19, 10, 0, tzinfo=CLINIC_TZ)
OPEN_SLOT_DAYS = (date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25), date(2026, 11, 27))  # 11/26: Thanksgiving
OPEN_SLOT_HOURS = range(9, 17)          # 9:00 AM ... 4:00 PM, on the hour
def seed_demo(store: Store, *, now: datetime) -> bool   # False (and no writes) if patients is not empty
```

Table schema:

```sql
patients(id INTEGER PRIMARY KEY, phone TEXT NOT NULL UNIQUE, first_name TEXT NOT NULL, last_name TEXT NOT NULL)
providers(id INTEGER PRIMARY KEY, name TEXT NOT NULL)
slots(id INTEGER PRIMARY KEY, provider_id INTEGER NOT NULL REFERENCES providers(id), start_at TEXT NOT NULL)
appointments(id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL REFERENCES patients(id),
             slot_id INTEGER NOT NULL UNIQUE REFERENCES slots(id),
             status TEXT NOT NULL CHECK (status IN ('booked','confirmed')), intake_json TEXT)
staff_tickets(id INTEGER PRIMARY KEY, phone TEXT NOT NULL, question TEXT NOT NULL,
              status TEXT NOT NULL CHECK (status IN ('open','answered','closed')), answer TEXT,
              created_at TEXT NOT NULL, closed_at TEXT)
jobs(id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('reminder','intake_followup','staff_relay')),
     phone TEXT NOT NULL, due_at TEXT NOT NULL, payload TEXT NOT NULL,
     status TEXT NOT NULL CHECK (status IN ('pending','done','cancelled','failed')),
     attempts INTEGER NOT NULL DEFAULT 0)
```

A slot counts as taken as long as any appointment references it; the UNIQUE constraint on `appointments.slot_id` guarantees there are no double bookings.

Semantics of each effect (all run inside the caller's transaction; if a condition is violated they raise, and the whole turn rolls back):
- `BookSlot`: moves the appointment to the new slot and sets the status back to `booked`. If the new slot is taken, the UNIQUE conflict is converted to `SlotTakenError`.
- `ConfirmAppointment`: sets the status to `confirmed`.
- `SaveIntakeResults`: writes `intake_json`.
- If any of the above three cannot find the appointment, it raises `EffectConflictError`.
- `CreateTicket`: creates a ticket in open status and returns an SMS to `STAFF_PHONE` with the text `f"[#{id}] {label} {format_phone(phone)}: {question}"`. When `label` is None, that part is omitted, leaving no extra space.
- `AnswerTicket`: may only go from open to answered.
- `CloseTicket`: may only go from answered to closed, and writes `closed_at`.
- `CancelJobs`: cancels only the pending jobs of the given kinds under this number.
- `FinishJob`: handles only pending jobs, otherwise raises `EffectConflictError`. This guarantees that the same job is never handled twice.

The rows returned by `commit_turn` and `log_only` are first this turn's outbound messages, then the SMS produced by the effects; both are written to the messages table and the outbox.

- [ ] **Step 1: Write the failing tests `tests/test_clinic_db.py`**

Add two fixtures to `tests/conftest.py`:
- `seeded_db(tmp_path) -> Path`: creates a new database and calls `seed_demo(store, now=SIM_START)`;
- `db_rows(db, sql) -> list[tuple]`: reads data through a separate `sqlite3` connection and does not touch `Store._conn` (deferred item).

```python
def test_seed_matches_the_spec_and_is_idempotent(seeded_db):
    with Store(seeded_db) as s:
        jane = s.clinic.patient_by_phone(JANE_PHONE); assert jane.label == "Jane D."
        appt = s.clinic.next_appointment(jane.id, SIM_START)
        assert (appt.start_at, appt.provider, appt.status) == (SEED_APPOINTMENT_AT, "Dr. Lee", "booked")
        week = s.clinic.open_slots(start=datetime(2026, 11, 23, tzinfo=CLINIC_TZ),
                                   end=datetime(2026, 11, 28, tzinfo=CLINIC_TZ), provider_id=None, limit=1000)
        assert len(week) == 64 and not any(x.start_at.astimezone(CLINIC_TZ).day == 26 for x in week)
        [job] = s.clinic.jobs(JANE_PHONE)
        assert (job.kind, job.due_at, job.status) == ("reminder", datetime(2026, 11, 17, 10, 0, tzinfo=CLINIC_TZ), "pending")
        assert seed_demo(s, now=SIM_START) is False

def test_open_slots_filter_order_and_limit(seeded_db): ...
    # provider Dr. Lee, next week, limit 6: first start Mon 11/23 9:00 AM; strictly ascending; all provider Dr. Lee
    # a range covering Thu 11/19 does not list Jane's booked 10:00 AM slot

def test_times_are_stored_in_utc(seeded_db):
    assert all(due.endswith("+00:00") for (due,) in db_rows(seeded_db, "SELECT due_at FROM jobs"))
    assert all(start.endswith("+00:00") for (start,) in db_rows(seeded_db, "SELECT start_at FROM slots"))

def test_book_slot_moves_the_appointment_and_frees_the_old_slot(seeded_db): ...

def test_a_failing_effect_rolls_back_the_whole_turn(seeded_db):     # Plan 1 T4 leftover
    # a second patient (555) 010-0002 holds the Mon 11/23 9:00 AM Dr. Lee slot (inserted via db_rows' connection)
    # commit_turn(state for JANE, effects=[ConfirmAppointment(jane_appt), BookSlot(jane_appt, that slot)]) -> SlotTakenError
    # afterwards: conversations, messages and outbox rows unchanged; Jane's appointment still "booked" at its old slot

def test_create_ticket_texts_the_front_desk(seeded_db):
    rows = s.commit_turn(phone=JANE_PHONE, expected_version=None, state=ConversationState(phone=JANE_PHONE),
                         inbound="q", outbound=["sent"], at=SIM_START,
                         effects=[CreateTicket(JANE_PHONE, "Why was I charged $40? — José 😀", "Jane D.")])
    assert [(r.phone, r.text) for r in rows] == [
        (JANE_PHONE, "sent"),
        (STAFF_PHONE, "[#1] Jane D. (555) 010-0001: Why was I charged $40? — José 😀")]
    assert s.clinic.ticket(1).status == "open"

def test_ticket_status_moves_only_forward(seeded_db): ...
    # AnswerTicket twice -> EffectConflictError on the second; CloseTicket on an open ticket -> EffectConflictError

def test_jobs_cancel_finish_and_failures(seeded_db): ...
    # CancelJobs cancels only pending jobs of the given kinds for that phone; FinishJob twice -> EffectConflictError;
    # record_job_failure x3 -> attempts 3, status "failed"; due_jobs() no longer lists it

def test_reminder_effects():
    start = datetime(2026, 11, 23, 9, 0, tzinfo=CLINIC_TZ)
    assert reminder_effects(phone=JANE_PHONE, appointment_id=1, start_at=start, now=SIM_START) == [
        CancelJobs(JANE_PHONE, ("reminder",)),
        ScheduleJob("reminder", JANE_PHONE, datetime(2026, 11, 21, 9, 0, tzinfo=CLINIC_TZ), {"appointment_id": 1})]
    assert reminder_effects(phone=JANE_PHONE, appointment_id=1, start_at=SIM_START + timedelta(hours=47),
                            now=SIM_START) == [CancelJobs(JANE_PHONE, ("reminder",))]
```

Append `test_new_db_is_seeded` to `tests/test_cli.py`: after `main` exits on EOF, Jane can be found in the database.

- [ ] **Step 2: Confirm they fail**

Run: `uv run pytest tests/test_clinic_db.py -q`
Expected: fails because of an ImportError.

- [ ] **Step 3: Implement `effects.py`, `clinic_db.py`, `jobs.py` (reminder part), `seed.py`, and the Store extensions**

- Add `clinic_db.SCHEMA` to the table-creation script in `Store.__init__`.
- Times are always converted to UTC in `apply_effect` and in the seed before being written; values read back are timezone-aware `datetime`s and can be compared directly with clinic-time-zone times.
- Seed data: Jane Doe (`JANE_PHONE`); two doctors, Dr. Lee and Dr. Patel; the slot at `SEED_APPOINTMENT_AT` goes to Dr. Lee and is Jane's appointment; both doctors each have one open slot at every `OPEN_SLOT_DAYS` × `OPEN_SLOT_HOURS` combination; plus the reminder job generated by `reminder_effects`. All of it is written in one transaction.
- On startup, `cli.py` calls `seed_demo(store, now=clock.now())` after `load_clock`.

- [ ] **Step 4: Confirm they pass**

Run: `uv run pytest -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/clinic_sms/effects.py src/clinic_sms/clinic_db.py src/clinic_sms/jobs.py src/clinic_sms/seed.py src/clinic_sms/store.py src/clinic_sms/cli.py tests/test_clinic_db.py tests/conftest.py tests/test_cli.py
git commit -m "feat: clinic tables, seed data and domain effects committed with the turn"
```

---

### Task 4: Turn, agent registry and generic runner

This task is only a refactor plus runner hardening; IntakeAgent is still the only top-level agent available. The reception and rescheduling agents are plugged into the registry in Task 5 and Task 6 respectively.

**Files:**
- Create: `src/clinic_sms/turn.py`, `src/clinic_sms/logsafe.py`, `src/clinic_sms/agents/common.py`, `src/clinic_sms/agents/registry.py`
- Modify: `src/clinic_sms/state.py`, `src/clinic_sms/agents/intake.py`, `src/clinic_sms/agents/__init__.py`, `src/clinic_sms/runner.py`, `tests/conftest.py` (move the `make_runner` fixture and the `load` / `commit` helpers over from `test_runner.py`; all later new test files use them), `tests/test_runner.py`, `tests/test_store.py`

**Interfaces:**
- Consumes: `ClinicView`, `Effect` (Task 3); `ReadySignal`; `IntakeAgent`, `IntakeState`; `dump_ctx` / `load_ctx`; `Clock`.
- Produces:

```python
# state.py
AgentName = Literal["reception", "reschedule", "intake"]
class ConversationState(BaseModel):          # new fields, all defaulted; Plan 1 JSON still parses
    active: AgentName | None = None
    agent_ctx: dict | None = None            # dump_ctx of the active agent when it is reception or reschedule
    appointment_id: int | None = None
    intake_followups_sent: int = 0           # used from Task 8
    @property
    def active_agent(self) -> AgentName | None   # active, else "intake" if intake_started (Plan 1 rows), else None

# logsafe.py
def describe_exception(exc: BaseException) -> str
    # qualified type name + traceback.format_tb frames for exc and each __cause__/__context__; never str(exc)

# turn.py
@dataclass
class Turn:                                   # AgentSession userdata; one per cold-started session
    phone: str
    clock: Clock
    clinic: ClinicView
    ready: ReadySignal                        # one-shot: a new Turn (and ReadySignal) per session
    intake: IntakeState                       # the stored intake state, carried into the snapshot
    appointment_id: int | None
    patient_label: str | None                 # "Jane D."; None for a number not in patients
    summarize_intake: bool
    agent_factory: Callable[["Turn", AgentName, llm.ChatContext], Agent]
    effects: list[Effect] = field(default_factory=list)
    active: Agent | None = None               # set as the first statement of each top-level agent's on_enter
    def handoff(self, name: AgentName, chat_ctx: llm.ChatContext) -> Agent   # agent_factory(self, name, chat_ctx)

# agents/common.py
SMS_STYLE  # moved from intake.py unchanged; intake.py and agents/__init__ re-export it

# agents/registry.py
def new_agent(turn: Turn, name: AgentName, chat_ctx: llm.ChatContext) -> Agent       # a handoff target
def restore_agent(state: ConversationState, turn: Turn) -> Agent                      # ValueError on bad state
def conversation_snapshot(state: ConversationState, turn: Turn) -> ConversationState  # may add effects to turn.effects

# agents/intake.py
IntakeAgent.__init__(self, *, state: IntakeState, ready: ReadySignal, summarize: bool = True,
                     turn: Turn | None = None) -> None   # with turn: on_enter sets turn.active = self first
```

Registry behavior in this task:
- `restore_agent`: when `state.active_agent == "intake"` returns `IntakeAgent(state=state.intake, ready=turn.ready, summarize=turn.summarize_intake, turn=turn)`; when it is None raises `ValueError("no active agent")`; `"reception"` and `"reschedule"` raise `ValueError` for now, to be filled in by Tasks 5 and 6.
- `new_agent(turn, "intake", ctx)`: returns `IntakeAgent(state=IntakeState(root_ctx=dump_ctx(ctx)), ...)`.
- `conversation_snapshot`: when `turn.active` is None raises `RuntimeError("no agent took over this turn")`; when it is an IntakeAgent returns `state.model_copy(update={"active": "intake", "agent_ctx": None, "intake": agent.snapshot(), "appointment_id": turn.appointment_id})`.

Runner changes (external signatures unchanged):
- A new `Turn` per turn. `patient_label` and `appointment_id` come from `state.appointment_id`, or, if there is none, the patient's nearest appointment after `clock.now()`. The agent is constructed with `registry.restore_agent`, with `AgentSession(llm=..., userdata=turn)`, and the commit uses the result of `conversation_snapshot` and `effects=turn.effects`.
- `start_intake`: if intake has already started or is already finished, returns `[]`, as before. Otherwise changes the conversation to `state.model_copy(update={"active": "intake", "intake": IntakeState(root_ctx=state.agent_ctx), "agent_ctx": None})` (creating a new one if there is no conversation), then takes the original `capture_run` branch.
- All failure logs are written uniformly as `logger.error("<what> for %s failed: %s", mask_phone(phone), describe_exception(e))`, no longer `logger.exception`.
- `handle_inbound` never raises. If recording the fallback SMS fails (`log_only` or delivery), it logs one `describe_exception`, then sends `channel.send(Outbound(phone, FALLBACK_TEXT))` directly (bypassing the outbox) and returns `[FALLBACK_TEXT]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_store.py`:

```python
def test_plan1_state_json_still_parses():
    st = ConversationState.model_validate_json(
        '{"phone": "+15550100001", "intake": {"progress": {"pending": ["insurance"], "results": {}, "visited": ["insurance"]}}}')
    assert st.active is None and st.active_agent == "intake" and st.intake_followups_sent == 0
    assert ConversationState(phone=P).active_agent is None
```

Append the following tests to `tests/test_runner.py`. Change the limit in `test_unknown_step_in_saved_state_falls_back_fast` to 0.5 seconds; leave Plan 1's other tests alone, and they must pass unchanged.

```python
async def test_ready_timeout_falls_back_fast(...):         # monkeypatch IntakeStep.on_enter to an async no-op
    # at visit_reason; ready_timeout=0.3 -> [FALLBACK_TEXT] within 2 s; stored state unchanged
async def test_run_timeout_falls_back(...):                # monkeypatch runner.RUN_TIMEOUT_S = 0.2; Reply(delay=1)
async def test_stale_version_falls_back_and_keeps_the_other_write(...):
    # "Follow-up on my knee" replies after delay=0.3; meanwhile another Store commits version+1
    # -> [FALLBACK_TEXT]; stored state == the other writer's
async def test_failure_logs_carry_no_exception_text(...):  # Review Focus 5
    # intake_replies["boom"] = Reply(error=APIError("Jane says her knee hurts", retryable=False))
    # one ERROR from clinic_sms.runner containing "APIError" and mask_phone(P); no record (any logger,
    # WARNING+) contains "knee hurts" or P
async def test_fallback_is_sent_even_if_recording_it_fails(...):
    # Reply(error=...) and monkeypatch Store.log_only to raise sqlite3.OperationalError
    # -> handle_inbound returns [FALLBACK_TEXT]; ch.sent[-1].text == FALLBACK_TEXT; no exception escapes
async def test_close_error_after_a_failed_turn_still_sends_the_fallback(...):
    # Reply(error=...) and aclose raising as in test_close_error_after_commit_keeps_the_turn
async def test_turn_userdata_and_active_agent(...):
    # monkeypatch registry.conversation_snapshot with a wrapper that records its `turn` argument
    # -> turn.phone == P; isinstance(turn.active, IntakeAgent); the session's userdata was that same Turn
```

- [ ] **Step 2: Confirm they fail**

Run: `uv run pytest tests/test_runner.py tests/test_store.py -q`
Expected: the new tests fail.

- [ ] **Step 3: Implement `state.py`, `logsafe.py`, `turn.py`, `agents/common.py`, `agents/registry.py`, the `turn` parameter of IntakeAgent, and the runner changes above**

Output format of `describe_exception`: one section per exception; the first line is `module.QualName`, followed by its `format_tb` lines; walk down `__cause__` (or `__context__` if there is none); sections are joined with `"\n-- caused by --\n"`.

- [ ] **Step 4: Confirm they pass**

Run: `uv run pytest -q`
Expected: all PASS.

- [ ] **Step 5: The author reruns the Task 2 smoke script with real Claude (the implementer does not run it)**

This round is a regression check after the runner refactor, about 12 Claude calls.

```bash
rm -f var/smoke/t4.db && uv run clinic-sms --db var/smoke/t4.db --script scenarios/plan1_intake.txt 2>&1 | tee var/smoke/t4.log
```

Expected: same as Task 2 Step 7. In addition, `+15550100001` cannot be found in `var/smoke/t4.log`. Write the result into `### Task 4`.

- [ ] **Step 6: Commit**

```bash
git add src/clinic_sms tests docs/findings.md
git commit -m "refactor: per-turn userdata, agent registry, safe failure logging"
```

---

### Task 5: Reception agent

**Files:**
- Create: `src/clinic_sms/agents/reception.py`, `tests/test_reception.py`, `scenarios/reception.txt`
- Modify: `src/clinic_sms/agents/registry.py`, `src/clinic_sms/agents/__init__.py`, `src/clinic_sms/runner.py`, `docs/findings.md`

**Interfaces:**
- Consumes: `Turn`, the registry (Task 4); `ConfirmAppointment`, `SaveIntakeResults`, `ClinicView` (Task 3); `SMS_STYLE`; `format_when`.
- Produces:

```python
# agents/reception.py
CLINIC_FAQ = ("Maple Street Family Clinic, 123 Maple Street, Springfield. Open Monday to Friday, "
              "8:00 AM to 5:00 PM; closed weekends and on Thanksgiving (Thu 11/26).")
CONFIRMED_OUTPUT = "Confirmed: {when} with {provider}."
NO_APPOINTMENT_OUTPUT = "The patient has no upcoming appointment."
class ReceptionAgent(Agent):
    NAME: ClassVar[AgentName] = "reception"
    def __init__(self, *, turn: Turn, chat_ctx: llm.ChatContext | None = None) -> None
    async def on_enter(self) -> None      # turn.active = self; turn.ready.set(self); never speaks
    @function_tool
    async def confirm_appointment(self, ctx: RunContext[Turn]) -> str
        # appends ConfirmAppointment; returns CONFIRMED_OUTPUT.format(...) or NO_APPOINTMENT_OUTPUT
```

The instructions of ReceptionAgent are written by the implementer, and must include:
- `SMS_STYLE` and `CLINIC_FAQ`;
- the current date and time: `format_when(turn.clock.now())`;
- the patient's appointment (time, doctor, status), or "none" if there is no appointment;
- rules: if the patient replies `1` or says they want to confirm, call `confirm_appointment`; do not make up what is not known; from Task 9 on, hand such questions to the front desk instead.

Registry changes:
- `restore_agent`: `"reception"` → `ReceptionAgent(turn=turn, chat_ctx=load_ctx(state.agent_ctx) if state.agent_ctx else None)`.
- `new_agent(turn, "reception", ctx)` → `ReceptionAgent(turn=turn, chat_ctx=ctx)`.
- `conversation_snapshot`:
  - When the current agent is a ReceptionAgent, `update={"active": "reception", "agent_ctx": dump_ctx(agent.chat_ctx), "intake": turn.intake, "appointment_id": turn.appointment_id}`.
  - When the current agent is an IntakeAgent and `agent.state.done` is true, intake is over and control goes back to reception (design §5.4). This is done through state, not by calling `update_agent` in `on_enter`, to avoid a race between the handoff and the end of the turn: `update={"active": "reception", "agent_ctx": dump_ctx(agent.chat_ctx), "intake": IntakeState(done=True, results=agent.state.results), ...}`. If `turn.appointment_id` is not None and the stored state is not yet done, also add `SaveIntakeResults(turn.appointment_id, agent.state.results)` to `turn.effects`. The next SMS is then picked up by the ReceptionAgent from the stored context.

Runner change: when `_answer` meets a number with no conversation:
- the number is in the patients table: create a new `ConversationState(phone, active="reception", appointment_id=<next appointment>)` with `expected_version=None` and run the turn normally;
- it is not: reply `NO_CONVERSATION_TEXT`, as before.

In addition, a conversation whose `state.active_agent is None` (the empty state that Plan 1 only ever wrote) is also treated as "no conversation", but the commit reuses the version number already on that row and must not use `expected_version=None`, otherwise it hits `StaleStateError`. Task 7's `may_start` uses the same rule.

- [ ] **Step 1: Write the failing tests `tests/test_reception.py`**

The runner uses `make_runner(seeded_db, llm, ch, clock=SimClock(SIM_START))`; `make_runner` gains a keyword argument `clock`, still defaulting to `SystemClock()`.

From this task on, the small functions in test assertions such as `jane_appointment()`, `pending_followups()` and `staff_texts()` are one-row helpers that read from the same database. They live in `tests/conftest.py` and are added by the first task that uses them. They only read data and contain no logic.

```python
async def test_known_patient_first_message_starts_reception(...):
    # "What are your hours on Friday?" -> Reply(text="We're open 8 to 5 on Friday.")
    assert sent == ["We're open 8 to 5 on Friday."]
    state, version = load(seeded_db)
    assert (state.active, version, state.appointment_id) == ("reception", 1, jane_appt_id)
    assert ("message", "user", "What are your hours on Friday?") in normalize_ctx(load_ctx(state.agent_ctx))

async def test_reception_restored_after_restart_sees_earlier_messages(...):
    # second message through a new runner; its LLM call context contains the first exchange

async def test_confirm_appointment_commits_with_the_turn(...):
    # "1" -> Reply(tools=[("confirm_appointment", {})]);
    # CONFIRMED_OUTPUT.format(when="Thu 11/19, 10:00 AM", provider="Dr. Lee") -> Reply(text="You're confirmed!")
    # appointment status "confirmed"; conversation version 1

async def test_effects_are_dropped_when_the_turn_fails(...):
    # same, but the CONFIRMED_OUTPUT reply is Reply(error=APIError(..., retryable=False))
    # -> [FALLBACK_TEXT]; appointment still "booked"; no conversation row

async def test_intake_completion_hands_back_to_reception_and_saves_results(...):
    # seeded; start_intake; three answers (each a new runner)
    state = load(seeded_db)[0]
    assert (state.active, state.intake.done, state.intake.progress) == ("reception", True, None)
    assert jane_appointment().intake == {"insurance": {...}, "visit_reason": {...}, "allergies_meds": {...}}
    # next message "Thanks!" -> Reply(text="Anytime!"): answered by ReceptionAgent;
    # its LLM context contains the completion message "You're all set. See you Thursday!"
```

- [ ] **Step 2: Confirm they fail**

Run: `uv run pytest tests/test_reception.py -q`
Expected: fails because of an ImportError.

- [ ] **Step 3: Implement ReceptionAgent, the registry and the runner changes**

- [ ] **Step 4: Confirm they pass**

Run: `uv run pytest -q`
Expected: all PASS.

- [ ] **Step 5: Write `scenarios/reception.txt`; the author runs it with real Claude (the implementer does not run it)**

```
# Reception smoke (author-run): known patient texts first, restart, confirm
What are your hours on Friday?
/restart
When is my appointment again?
Please confirm it
/state
```

About 5 Claude calls:

```bash
rm -f var/smoke/t5.db && uv run clinic-sms --db var/smoke/t5.db --script scenarios/reception.txt 2>&1 | tee var/smoke/t5.log
```

Expected:
- `SMOKE: OK (fallbacks=0)`;
- the second reply states Thu 11/19, 10:00 AM and Dr. Lee;
- the reply after the confirmation says it is confirmed;
- querying `sqlite3 var/smoke/t5.db "select status from appointments"` returns `confirmed`.

Write the result into `### Task 5`.

- [ ] **Step 6: Commit**

```bash
git add src/clinic_sms tests scenarios/reception.txt docs/findings.md
git commit -m "feat: reception agent; intake hands back to reception when done"
```

---

### Task 6: Rescheduling agent and the handoff chain

**Files:**
- Create: `src/clinic_sms/agents/reschedule.py`, `tests/test_handoffs.py`, `scenarios/reschedule.txt`
- Modify: `src/clinic_sms/agents/reception.py`, `src/clinic_sms/agents/registry.py`, `src/clinic_sms/agents/intake.py`, `src/clinic_sms/agents/__init__.py`, `docs/findings.md`; depending on the Step 0 condition, also `src/clinic_sms/llm.py`, `repro/finding_<n>.py`, `tests/test_llm.py`

**Interfaces:**
- Consumes: `Turn.handoff`, the registry (Task 4); `ReceptionAgent` (Task 5); `BookSlot`, `ClinicView.slot/open_slots/appointment` (Task 3); `reminder_effects` (Task 3).
- Produces:

```python
# agents/reschedule.py
MAX_SLOTS_LISTED = 6
NO_SLOTS_OUTPUT = "No open slots in that range."
BOOKED_OUTPUT = "Booked: {when} with {provider}."
SLOT_UNAVAILABLE = "That slot is not available. Offer other open slots."      # ToolError message
BAD_DATES = "Dates must be YYYY-MM-DD, and end_date must not be before start_date."  # ToolError message
def format_slot_list(slots: list[Slot]) -> str
    # "slot {id}: {format_when(start_at)} with {provider}" per line; NO_SLOTS_OUTPUT if empty
class RescheduleAgent(Agent):
    NAME: ClassVar[AgentName] = "reschedule"
    OPENING: ClassVar[str] = ("Find open slots that match what the patient asked for with list_open_slots, "
                              "and offer up to three of them.")
    def __init__(self, *, turn: Turn, chat_ctx: llm.ChatContext | None = None, resuming: bool) -> None
    async def on_enter(self) -> None      # turn.active = self; turn.ready.set(self); OPENING unless resuming
    @function_tool
    async def list_open_slots(self, ctx: RunContext[Turn], start_date: str, end_date: str) -> str
        # clinic-local dates, both inclusive; the current appointment's provider (all providers if none)
    @function_tool
    async def book_slot(self, ctx: RunContext[Turn], slot_id: int) -> tuple[Agent, str]

# agents/reception.py
    @function_tool
    async def transfer_to_reschedule(self, ctx: RunContext[Turn]) -> Agent   # bare handoff
        # return turn.handoff("reschedule", self.chat_ctx)
```

Behavior of `book_slot`:
1. If `turn.appointment_id` is None, raise `ToolError(NO_APPOINTMENT_OUTPUT)`.
2. If the slot does not exist, is already taken, or has `start_at <= now`, raise `ToolError(SLOT_UNAVAILABLE)`.
3. Append `BookSlot` and `reminder_effects(...)` to `turn.effects`.
4. Next agent: hand back to `"reception"` when `turn.intake.done`, otherwise hand off to `"intake"` (a fresh intake).
5. Return `(next_agent, BOOKED_OUTPUT.format(when=format_when(slot.start_at), provider=slot.provider))`. The old agent confirms the rescheduling with one SMS first, and only then does the new agent start talking (Spike 4 H1n).

Add one rule to the ReceptionAgent instructions: if the patient replies `2` or says they want to change the time, call `transfer_to_reschedule`.

The implementer writes the rescheduling agent's instructions. They must include:
- `SMS_STYLE`;
- the current date and time, plus the ISO date (for example `2026-11-17`), so the LLM can work out "next week";
- the current appointment;
- the rules: do not show the slot id to the patient; only book the slot the patient explicitly chose.

Registry changes:
- `restore_agent`: `"reschedule"` → `RescheduleAgent(turn=turn, chat_ctx=load_ctx(...), resuming=True)`.
- `new_agent(turn, "reschedule", ctx)` → `resuming=False`.
- `conversation_snapshot`: handle RescheduleAgent, written the same way as for ReceptionAgent.

Deferred items for IntakeAgent (this file has to change anyway):
- After `on_enter` has used up the group context, set `self._group_ctx` to None;
- move `update_chat_ctx` into the `try`;
- when the group fails, set `self._failed = True`, and make `snapshot()` check this flag instead of relying on `group.done()`.

- [ ] **Step 0 (conditional): only do this if Spike 4's H1n got a 400 with real Claude**

`<n>` is the next unused FINDING number; if no new entries were added before this, it is 8.

1. Write `repro/finding_<n>.py`. Follow the style of `repro/finding_7.py`: standalone, offline, no API key needed. Use `httpx.MockTransport` to record the request body the plugin sends, and show that `tool_choice="none"` combined with a context that contains tool_use sends `"tools": []` and no `tool_choice`. Quote the real 400 verbatim from the Spike 4 log.
2. In `llm.py`, add `class ClaudeLLM(anthropic.LLM)` (`# SDK-FINDING-<n>`): when `chat()` receives `tool_choice == "none"` and the tools are not empty, it switches to `tool_choice=NOT_GIVEN` and puts `{"tool_choice": {"type": "none"}}` in `extra_kwargs`. `make_claude_llm()` now returns `ClaudeLLM`.
3. In `tests/test_llm.py`, test with the same MockTransport: tools in the request body are not empty, and `tool_choice == {"type": "none"}`.
4. Write FINDING-<n> following §5.10, including a search of upstream with a read-only `gh search`.
5. Ask the author to rerun Spike 4 and confirm that H1n passes.

- [ ] **Step 1: Write the failing test `tests/test_handoffs.py`**

The tests use `jane_slot(clinic, day, hour)` to find Dr. Lee's slot id by date and time. `restart` means a fresh runner for each step.

```python
async def test_handoff_chain_persists_each_step(...):                    # spec §5.9 #4
    # 1) "2, can I move it to next week?" -> reception Reply(tools=[("transfer_to_reschedule", {})]);
    #    RescheduleAgent.OPENING -> Reply(tools=[("list_open_slots", {"start_date": "2026-11-23", "end_date": "2026-11-27"})]);
    #    format_slot_list(<first 6 Dr. Lee slots next week>) -> Reply(text="I have Mon 11/23 at 9, 10 or 11 AM.")
    assert sent == ["I have Mon 11/23 at 9, 10 or 11 AM."]
    assert load(db)[0].active == "reschedule"
    # 2) restart; "9 AM please" -> Reply(tools=[("book_slot", {"slot_id": mon9})]);
    #    BOOKED_OUTPUT.format(when="Mon 11/23, 9:00 AM", provider="Dr. Lee") -> Reply(text="Booked for Mon 11/23, 9 AM.");
    #    InsuranceTask.OPENING -> as in intake_replies
    assert sent == ["Booked for Mon 11/23, 9 AM.", "Hi Jane! What's your insurance carrier and member ID?"]
    state = load(db)[0]
    assert state.active == "intake" and state.intake.progress.pending[0] == "insurance"
    assert jane_appointment().slot_id == mon9
    assert [(j.kind, j.due_at, j.status) for j in clinic.jobs(JANE_PHONE) if j.kind == "reminder"] == [
        ("reminder", datetime(2026, 11, 17, 10, 0, tzinfo=CLINIC_TZ), "cancelled"),
        ("reminder", datetime(2026, 11, 21, 9, 0, tzinfo=CLINIC_TZ), "pending")]
    assert llm.calls.count(RescheduleAgent.OPENING) == 1          # not re-offered after the restart
    # 3) restart; three intake answers -> active "reception", appointment intake saved

@pytest.mark.parametrize("which", ["unknown", "taken", "past"])
async def test_book_slot_rejects_unknown_taken_or_past_slot(which, ...):  # Review Focus 1
    # unknown: 9999; taken: a slot held by a second patient's appointment; past: a slot inserted at SIM_START - 1h
    # Reply(tools=[("book_slot", {"slot_id": x})]); SLOT_UNAVAILABLE -> Reply(text="Sorry, that one is gone. How about 10 AM?")
    assert sent == ["Sorry, that one is gone. How about 10 AM?"]
    # appointment slot, jobs and state.active ("reschedule") unchanged; conversation version + 1

async def test_list_open_slots_rejects_bad_dates(...):     # "2026-13-01" or end < start -> BAD_DATES output reaches the LLM

async def test_rescheduling_after_intake_returns_to_reception(...):
    # intake done, active reception; transfer -> book -> next agent is reception, no InsuranceTask.OPENING call
```

- [ ] **Step 2: Confirm it fails**

Run: `uv run pytest tests/test_handoffs.py -q`
Expected: fails with an ImportError.

- [ ] **Step 3: Implement `reschedule.py`, `transfer_to_reschedule`, the registry changes and the IntakeAgent deferred items**

- [ ] **Step 4: Confirm it passes**

Run: `uv run pytest -q`
Expected: all PASS. At teardown, conftest's `scripted_llm` checks the tools offered on every call against the Anthropic format, and the new tools' schemas are covered by that check.

- [ ] **Step 5: Write `scenarios/reschedule.txt`; the author runs it with real Claude (the implementer does not run it)**

```
# Reschedule smoke (author-run): reception -> reschedule -> intake -> back to reception
I need to move my appointment to next week
/restart
The first one you listed works
Aetna, member W123456789
/restart
Follow-up on my knee
Penicillin; ibuprofen
Thanks!
/state
```

This calls Claude about 15 times:

```bash
rm -f var/smoke/t6.db && uv run clinic-sms --db var/smoke/t6.db --script scenarios/reschedule.txt 2>&1 | tee var/smoke/t6.log
```

Expected:
- `SMOKE: OK (fallbacks=0)`;
- the slots offered fall within 11/23–11/27, all with Dr. Lee, none on 11/26, and no slot id is exposed;
- after a slot is chosen, a confirmation SMS arrives first, then the insurance question;
- after intake finishes, "Thanks!" is answered by reception;
- `/state` shows the new appointment time.

If Claude does not call a tool, or makes up a slot, record it under `### Task 6`, then decide whether to change the prompt or record a FINDING.

- [ ] **Step 6: Commit**

```bash
git add src/clinic_sms tests scenarios/reschedule.txt docs/findings.md
git commit -m "feat: reschedule agent and reception -> reschedule -> intake handoffs"
```

If Step 0 was done, also add `repro/finding_<n>.py` and `tests/test_llm.py` to this commit.

---

### Task 7: Proactive messages, job scheduling and appointment reminders

**Files:**
- Modify: `src/clinic_sms/runner.py`, `src/clinic_sms/jobs.py`, `src/clinic_sms/sim.py`, `src/clinic_sms/cli.py`, `docs/findings.md`
- Create: `tests/test_jobs.py`, `scenarios/reminder.txt`

**Interfaces:**
- Consumes: the registry, `Turn` (Tasks 4–6); `ClinicView.due_jobs/appointment`, `Store.apply/record_job_failure`, `FinishJob`, `MAX_JOB_ATTEMPTS` (Task 3); `Simulator` (Task 2).
- Produces:

```python
# runner.py
@dataclass(frozen=True)
class Proactive:
    instructions: str
    effects: tuple[Effect, ...] = ()
    followup: bool = False      # Task 8: this message counts toward MAX_FOLLOWUPS
    may_start: bool = False     # no conversation yet: start one with the reception agent (reminders)
class NoConversationError(RuntimeError): ...
ConversationRunner.run_proactive(self, phone: str, req: Proactive) -> list[str]
    # same lock and cold start; generate_reply(instructions=req.instructions) instead of run();
    # raises on failure (never sends FALLBACK_TEXT); returns the delivered texts

# jobs.py
REMINDER_INSTRUCTIONS = ("Send the patient a reminder of their appointment on {when} with {provider}. "
                         "Ask them to reply 1 to confirm or 2 to reschedule.")
class Scheduler:
    def __init__(self, *, store: Store, runner: ConversationRunner, clock: Clock) -> None
    def next_due(self, until: datetime) -> Job | None
    async def run_due(self) -> int     # every pending job due by clock.now(), in (due_at, id) order, once each

# sim.py
Simulator.__init__(..., scheduler: Scheduler)    # new required keyword
```

The flow of one proactive turn:
1. A plain `start(agent)`, then `ready.wait`.
2. `handle = session.generate_reply(instructions=...)`, then `await asyncio.wait_for(handle, RUN_TIMEOUT_S)`.
3. If `handle.exception()` is not None, raise it; if the outbound is empty, raise `EmptyReplyError`.
4. Snapshot, commit with `req.effects`; deliver after the commit.

If there is no session: when `may_start` is true and the number is in the patients table, create a new reception session; otherwise raise `NoConversationError`.

For each due job, `Scheduler.run_due` does the following:
- **reminder**: if the appointment that `payload["appointment_id"]` points to does not exist or has already started, `store.apply([FinishJob(id, "cancelled")], at=now)`. Otherwise `run_proactive(phone, Proactive(REMINDER_INSTRUCTIONS.format(when=format_when(appt.start_at), provider=appt.provider), effects=(FinishJob(id),), may_start=True))`.
- Other kinds are added by Tasks 8 and 9. Until then, hitting one is recorded as a failure.
- Any exception: log `logger.error("job %d (%s) for %s failed: %s", id, kind, mask_phone(phone), describe_exception(e))`, then `store.record_job_failure(id, at=now)`.

Changes to `Simulator`:
- `advance(delta)` first computes the target time `target`. As long as `scheduler.next_due(target)` still returns a job, move the clock to that job's `due_at` (never backwards), `save_clock`, then `await scheduler.run_due()`. Finally move the clock to `target` and `save_clock`. A job that keeps failing is retried at most `MAX_JOB_ATTEMPTS` times and then becomes failed, so the loop ends naturally.
- `startup()` calls `run_due()` once after `deliver_pending`; it is also called once after each command finishes.
- `smoke_summary` gains `failed_jobs`, taken from `store.clinic.failed_job_count()`.
- `/state` changes to its final format:

  ```json
  {"active": ..., "appointment": {"when": ..., "provider": ..., "status": ...},
   "intake": {"done", "progress", "results"},
   "jobs": [{"kind", "due", "status"} pending only],
   "tickets": [{"id", "status"}]}
  ```

  All times use `format_when`.

`cli.py` has to construct the `Scheduler` and pass it to `Simulator`. This fits Plan 1's Ruling C8-3: scheduling runs only inside the command loop and never concurrently with input handling; delivery happens after the commit, and rows whose delivery fails stay in the outbox and are resent on the next startup.

- [ ] **Step 1: Write the failing test `tests/test_jobs.py`**

```python
async def test_reminder_fires_48h_before_and_starts_reception(...):
    # seeded; SimClock(SIM_START); scheduler.run_due() == 0
    # clock.advance(5m); key REMINDER_INSTRUCTIONS.format(when="Thu 11/19, 10:00 AM", provider="Dr. Lee")
    #   -> Reply(text="Reminder: Thu 11/19, 10:00 AM with Dr. Lee. Reply 1 to confirm or 2 to reschedule.")
    assert await scheduler.run_due() == 1
    assert ch.sent[-1] == Outbound(JANE_PHONE, "Reminder: ...")
    state, version = load(db); assert (state.active, version) == ("reception", 1)
    assert [m.at for m in messages(JANE_PHONE)] == [datetime(2026, 11, 17, 10, 0, tzinfo=CLINIC_TZ)]
    assert clinic.jobs(JANE_PHONE)[0].status == "done"

async def test_reply_2_after_the_reminder_reaches_reschedule(...):
    # after the reminder, "2" -> transfer_to_reschedule -> the OPENING reply is sent; state.active == "reschedule"

async def test_failed_proactive_turn_is_retried_then_given_up(...):
    # the reminder reply is Reply(error=APIError(..., retryable=False)); run_due three times
    # attempts 1, 2, 3 -> status "failed"; no SMS to Jane (no fallback); no conversation row;
    # Simulator.smoke_summary() == (False, "SMOKE: FAIL (fallbacks=0, failed_jobs=1)")

async def test_advance_runs_each_due_job_once_at_its_due_time(...):     # Review Focus 2
    # second patient (555) 010-0002 with an appointment Thu 11/19 2:00 PM -> reminder due Tue 2:00 PM
    # Simulator.advance(parse_duration("1d"))
    # each reminder sent once; message times Tue 10:00 AM and Tue 2:00 PM; clock == SIM_START + 1d
    # both jobs "done"; the scripted LLM saw each reminder key exactly once

async def test_startup_runs_overdue_jobs(...):
    # save_clock at Tue 11:00 AM before the Simulator exists; Simulator.startup() sends the 10:00 AM reminder

async def test_proactive_without_a_conversation_needs_may_start(...):
    # run_proactive(JANE_PHONE, Proactive("x")) -> NoConversationError; no LLM call, nothing written
```

- [ ] **Step 2: Confirm it fails**

Run: `uv run pytest tests/test_jobs.py -q`
Expected: fails with an ImportError.

- [ ] **Step 3: Implement `run_proactive`, `Scheduler`, and the `Simulator` and `cli` changes**

- [ ] **Step 4: Confirm it passes**

Run: `uv run pytest -q`
Expected: all PASS.

- [ ] **Step 5: Write `scenarios/reminder.txt`; the author runs it with real Claude (the implementer does not run it)**

```
# Reminder smoke (author-run): demo steps 1-3 up to the first intake question
/advance 5m
2, can I move it to next week?
The first one works
/state
```

This calls Claude about 7 times:

```bash
rm -f var/smoke/t7.db && uv run clinic-sms --db var/smoke/t7.db --script scenarios/reminder.txt 2>&1 | tee var/smoke/t7.log
```

Expected:
- `SMOKE: OK (fallbacks=0, failed_jobs=0)`;
- the reminder goes out at 10:00 AM and mentions Thu 11/19, 10:00 AM, Dr. Lee, and replying 1 / replying 2;
- `/state` shows `active: intake`, and the pending jobs include a Sat 11/21 9:00 AM reminder.

Write the results into `### Task 7`.

- [ ] **Step 6: Commit**

```bash
git add src/clinic_sms tests/test_jobs.py scenarios/reminder.txt docs/findings.md
git commit -m "feat: proactive turns, job scheduler and 48-hour reminders"
```

---

### Task 8: Intake follow-ups

**Files:**
- Modify: `src/clinic_sms/jobs.py`, `src/clinic_sms/runner.py`, `docs/findings.md`
- Create: `tests/test_followups.py`, `scenarios/followup.txt`

**Interfaces:**
- Consumes: `Proactive`, `run_proactive`, `Scheduler` (Task 7); `ConversationState.intake_followups_sent`, `active_agent` (Task 4); `ScheduleJob`, `CancelJobs`, `FinishJob` (Task 3).
- Produces:

```python
# jobs.py
FOLLOWUP_AFTER = timedelta(hours=4)
QUIET_START = time(21, 0)          # 9:00 PM, quiet from here (inclusive)
QUIET_END = time(8, 0)             # 8:00 AM, sending allowed from here (inclusive)
MAX_FOLLOWUPS = 2                  # per intake
FOLLOWUP_INSTRUCTIONS = ("The patient has not replied for a while. Send one short, friendly nudge "
                         "that repeats only your current question.")
def shift_out_of_quiet_hours(at: datetime) -> datetime   # in CLINIC_TZ: >= 21:00 -> next day 8:00; < 8:00 -> same day 8:00
def followup_due(now: datetime) -> datetime              # shift_out_of_quiet_hours(now + FOLLOWUP_AFTER)
def intake_pending(state: ConversationState) -> bool     # active_agent == "intake" and not intake.done
def followup_effects(state: ConversationState, *, now: datetime, patient_replied: bool) -> list[Effect]
```

Rules for `followup_effects`. Design §5.6 only says "no reply for 4 hours"; here that is interpreted as "counted from the last committed turn":
- Whenever `patient_replied` is true or `intake_pending(state)` is true, add `CancelJobs(phone, ("intake_followup",))`.
- If `intake_pending(state)` and `state.intake_followups_sent < MAX_FOLLOWUPS`, also add `ScheduleJob("intake_followup", phone, followup_due(now), {})`.

Runner change: before each turn is committed, `effects = turn.effects + req.effects + followup_effects(new_state, now=clock.now(), patient_replied=inbound is not None)`. "Each turn" here covers inbound turns, proactive messages and `start_intake`. When `req.followup` is true, `new_state`'s `intake_followups_sent` is incremented by 1 before the effects are computed.

When the Scheduler hits an `intake_followup`:
- If the conversation does not exist, `not intake_pending(state)`, or `MAX_FOLLOWUPS` follow-ups have already been sent: `store.apply([FinishJob(id, "cancelled")], at=now)`, with no LLM call.
- Otherwise: `run_proactive(phone, Proactive(FOLLOWUP_INSTRUCTIONS, effects=(FinishJob(id),), followup=True))`.

- [ ] **Step 1: Write the failing test `tests/test_followups.py`**

```python
@pytest.mark.parametrize("local,expected", [
    ((17, 0), (1, 8, 0)),       # 5:00 PM + 4h = 9:00 PM -> next day 8:00 AM (spec §5.6 example)
    ((16, 59), (0, 20, 59)),    # 8:59 PM stays
    ((19, 30), (1, 8, 0)),      # 11:30 PM -> next day 8:00 AM
    ((3, 0), (0, 8, 0)),        # 7:00 AM -> same day 8:00 AM
    ((4, 0), (0, 8, 0)),        # 8:00 AM stays
    ((8, 0), (0, 12, 0)),       # noon stays
])
def test_followup_due(local, expected):  # local = time "now" on Tue 11/17; expected = (day offset, hour, minute)

def test_followup_effects_rules(): ...
    # in intake + patient replied -> [CancelJobs, ScheduleJob]; not in intake + replied -> [CancelJobs];
    # not in intake, proactive -> []; in intake with intake_followups_sent == 2 -> [CancelJobs]

async def test_followup_next_morning_after_patient_stops_at_5pm(...):   # spec §5.9 #5, demo step 4
    # SimClock Tue 5:00 PM; start_intake; "Aetna, member W123456789"; "Follow-up on my knee"
    assert pending_followups() == [datetime(2026, 11, 18, 8, 0, tzinfo=CLINIC_TZ)]
    # restart (new Store, runner, Scheduler, Simulator with the same clock); advance 15h
    #   FOLLOWUP_INSTRUCTIONS -> Reply(text="Morning! Any allergies or current medications?")
    assert last_message_at(JANE_PHONE) == datetime(2026, 11, 18, 8, 0, tzinfo=CLINIC_TZ)
    assert load(db)[0].intake_followups_sent == 1
    assert pending_followups() == [datetime(2026, 11, 18, 12, 0, tzinfo=CLINIC_TZ)]
    # "Penicillin; ibuprofen" at 8:30 AM -> intake done; pending_followups() == []
    assert llm.calls.count(InsuranceTask.OPENING) == 1 and llm.calls.count(VisitReasonTask.OPENING) == 1

async def test_reply_cancels_pending_followups_and_schedules_one(...):
async def test_at_most_two_followups_per_intake(...):
    # silent for 3 days via advance("3d") -> exactly 2 follow-up SMS; no pending follow-up afterwards
async def test_stale_followup_is_cancelled_without_llm_call(...):        # Review Focus 3
    # a pending intake_followup job, but the stored intake is done (active reception)
    # advance past it -> job "cancelled"; llm.calls unchanged; no SMS
```

- [ ] **Step 2: Confirm it fails**

Run: `uv run pytest tests/test_followups.py -q`
Expected: fails with an ImportError.

- [ ] **Step 3: Implement the functions above, the runner change and the Scheduler's follow-up branch**

- [ ] **Step 4: Confirm it passes**

Run: `uv run pytest -q`
Expected: all PASS.

- [ ] **Step 5: Write `scenarios/followup.txt`; the author runs it with real Claude (the implementer does not run it)**

```
# Follow-up smoke (author-run): stop at 5 PM, restart, follow-up at 8 AM the next day
/advance 7h
/start-intake
Aetna, member W123456789
Follow-up on my knee
/restart
/advance 15h
Penicillin; ibuprofen
/state
```

The `/advance 7h` at the start first fires the seeded reminder at 10:00 AM; this is expected. This calls Claude about 10 times:

```bash
rm -f var/smoke/t8.db && uv run clinic-sms --db var/smoke/t8.db --script scenarios/followup.txt 2>&1 | tee var/smoke/t8.log
```

Expected:
- `SMOKE: OK (fallbacks=0, failed_jobs=0)`;
- a follow-up goes out at Wed 11/18 8:00 AM, asking only about allergies and medications;
- after the reply, intake finishes, and `/state` shows no pending follow-up.

Write the results into `### Task 8`.

- [ ] **Step 6: Commit**

```bash
git add src/clinic_sms tests/test_followups.py scenarios/followup.txt docs/findings.md
git commit -m "feat: intake follow-ups with quiet hours, a cap of two and cancel on reply"
```

---

### Task 9: Escalation to a human, relaying front desk answers, and the full main-line script

**Files:**
- Modify: `src/clinic_sms/agents/common.py`, `src/clinic_sms/agents/reception.py`, `src/clinic_sms/agents/reschedule.py`, `src/clinic_sms/agents/intake.py`, `src/clinic_sms/runner.py`, `src/clinic_sms/jobs.py`, `tests/conftest.py`, `docs/findings.md`
- Create: `tests/test_escalation.py`, `tests/test_mainline_scripted.py`, `scenarios/mainline.txt`

**Interfaces:**
- Consumes: `Turn.patient_label`, `Turn.effects` (Task 4); `CreateTicket`, `AnswerTicket`, `CloseTicket`, `ScheduleJob`, `STAFF_PHONE`, `ClinicView.ticket` (Task 3); `Proactive`, `Scheduler` (Task 7).
- Produces:

```python
# agents/common.py
ESCALATED_OUTPUT = ("Sent to the front desk. Tell the patient the front desk will text them back, "
                    "then continue with your current task.")
@function_tool
async def escalate_to_staff(ctx: RunContext[Turn], question: str) -> str
    # appends CreateTicket(turn.phone, question, turn.patient_label); returns ESCALATED_OUTPUT

# runner.py
STAFF_USAGE_TEXT = "To answer a question, reply with its ticket number and your answer, e.g. #12 Your copay was $25."
STAFF_UNKNOWN_TICKET_TEXT = "There is no open ticket #{ticket_id}."
def parse_staff_reply(text: str) -> tuple[int, str] | None    # r"^\s*#(\d+)\s+(\S.*)$" with DOTALL; answer stripped

# jobs.py
RELAY_INSTRUCTIONS = ("The front desk answered the patient's earlier question. Pass the answer on in your own "
                      "words, then continue where you left off.\nQuestion: {question}\nAnswer: {answer}")
```

Changes in each place:

- **Tool wiring**: add `escalate_to_staff` to the `tools=[...]` of ReceptionAgent and RescheduleAgent, and to `IntakeStep.__init__`'s `super().__init__(instructions=..., tools=[escalate_to_staff])`. It is available in every intake step (design §5.4). Add one sentence to the instructions of every agent and step: for questions it cannot handle itself, such as billing or insurance reimbursement, call `escalate_to_staff`. This is where the rule in Task 5 that says "from Task 9 on, hand these to the front desk" gets implemented.
- **Front desk inbound**: when `handle_inbound` receives a message with `phone == STAFF_PHONE`, it goes through `_handle_staff(text)`, handled under the front desk number's lock:
  - Parse failure: `log_only(inbound=text, outbound=[STAFF_USAGE_TEXT])`;
  - The ticket does not exist or is not open: `log_only(inbound=text, outbound=[STAFF_UNKNOWN_TICKET_TEXT.format(ticket_id=n)])`;
  - Otherwise `log_only(inbound=text, outbound=[], effects=[AnswerTicket(n, answer), ScheduleJob("staff_relay", ticket.phone, now, {"ticket_id": n})])`, and return `[]`, with no message back to the front desk.
- **Relay**: when the Scheduler hits `staff_relay`:
  - The ticket does not exist or is not answered: `store.apply([FinishJob(id, "cancelled")], at=now)`;
  - Otherwise `run_proactive(ticket.phone, Proactive(RELAY_INSTRUCTIONS.format(question=t.question, answer=t.answer), effects=(FinishJob(id), CloseTicket(t.id))))`. The relay is sent by the patient's currently active agent: in the middle of intake, that is the step that is waiting for an answer, and after relaying it goes on to ask its original question.

`scenarios/mainline.txt` is the script for the demo main line (design §2.1). Its timing matches the example in design §5.6 exactly: the patient stops at 5 PM and gets a follow-up at 8:00 AM the next day.

```
# Demo main line (spec §2.1), about 3 minutes
# 1. the clinic texts first: the 48-hour reminder at 10:00 AM
/advance 5m
# 2. handoff to reschedule; the patient replies at 5:00 PM
/advance 7h
2, can I move it to next week?
The first one works
# 3. intake (TaskGroup): two answers, then silence
Aetna, member W123456789
Follow-up on my knee
# 4. a real restart, then the next morning: the follow-up at 8:00 AM
/restart
/advance 15h
# 5. regression to the insurance step
Actually my member ID is wrong, it's W999
Aetna, W999
# 6. a billing question goes to the front desk, who answers hours later
Also, why was I charged $40 last time?
/advance 3h
/as (555) 010-0100
#1 That was your copay from your 10/2 visit. Nothing else is due.
/as (555) 010-0001
No allergies, and I take ibuprofen daily
/state
```

The line that starts with `#1` is the front desk's reply, not a comment (Task 2's `is_comment` rule).

- [ ] **Step 1: Write the failing tests**

`tests/test_escalation.py`:

```python
async def test_escalation_mid_intake_texts_staff_and_keeps_the_step(...):
    # at allergies_meds; "Why was I charged $40?" -> Reply(tools=[("escalate_to_staff", {"question": "Why was I charged $40?"})]);
    # ESCALATED_OUTPUT -> Reply(text="I've sent that to our front desk. Meanwhile, any allergies or medications?")
    assert [(m.to, m.text) for m in ch.sent[-2:]] == [
        (JANE_PHONE, "I've sent that to our front desk. Meanwhile, any allergies or medications?"),
        (STAFF_PHONE, "[#1] Jane D. (555) 010-0001: Why was I charged $40?")]
    assert clinic.ticket(1).status == "open" and load(db)[0].intake.progress.pending[0] == "allergies_meds"

async def test_staff_answer_is_relayed_mid_intake_then_intake_continues(...):   # spec §5.9 #7
    # then: handle_inbound(STAFF_PHONE, "#1 That was your copay.") == []   (nothing sent to staff)
    # scheduler.run_due(): RELAY_INSTRUCTIONS.format(question="Why was I charged $40?", answer="That was your copay.")
    #   -> Reply(text="Front desk: that was your copay. Any allergies or medications?")
    # ticket "closed"; then "Penicillin; ibuprofen" completes the intake (allergies_meds recorded, active reception)

async def test_bad_staff_replies_get_usage_or_unknown_ticket_text(...):          # Review Focus 4
    # "hello" -> [STAFF_USAGE_TEXT]; "#99 x" -> ["There is no open ticket #99."];
    # "#1 a" then "#1 b" -> [] then ["There is no open ticket #1."]
    # exactly one staff_relay job ever; Jane received nothing from these; ticket answer == "a"

async def test_reception_can_escalate_too(...):
async def test_logs_carry_no_question_or_answer(...):
    # run the two tests' flows under caplog (DEBUG); no record line contains the question, the answer or P
```

`tests/test_mainline_scripted.py`: use the Simulator and ScriptedLLM to run `scenarios/mainline.txt`. The test reads the script line by line itself (skipping blank lines and `is_comment` lines) and calls `Simulator.execute(parse_command(line))`. When it returns `"restart"`, close the Store and build a new Store, runner, Scheduler and Simulator on the same database with the same LLM instance, simulating a process restart. This is the regression part of design §5.8, "used for both the demo and regression".

```python
async def test_mainline_script_with_scripted_llm(...):
    # summarize_intake=False; replies: tests/conftest.py fixture `mainline_replies` (one per LLM call the
    # script causes: reminder, transfer, OPENING, slot list, book, booked note, three intake answers with
    # their openings, follow-up, out_of_scope, re-asked insurance, escalate, ESCALATED_OUTPUT, relay,
    # last answer, COMPLETION)
    assert FALLBACK_TEXT not in all_sent_texts
    appt = jane_appointment()
    assert (appt.start_at, appt.provider) == (datetime(2026, 11, 23, 9, 0, tzinfo=CLINIC_TZ), "Dr. Lee")
    assert appt.intake["insurance"]["member_id"] == "W999" and appt.intake["allergies_meds"]["medications"] == ["ibuprofen"]
    assert followup_message_times() == [datetime(2026, 11, 18, 8, 0, tzinfo=CLINIC_TZ)]
    assert clinic.ticket(1).status == "closed"
    assert staff_texts() == ["[#1] Jane D. (555) 010-0001: Why was I charged $40 last time?"]
    state = load(db)[0]; assert (state.active, state.intake.done) == ("reception", True)
    assert [(j.kind, j.status) for j in clinic.jobs(JANE_PHONE) if j.status == "pending"] == [("reminder", "pending")]
    assert llm.calls.count(InsuranceTask.OPENING) == 2 and llm.calls.count(VisitReasonTask.OPENING) == 1
```

- [ ] **Step 2: Confirm it fails**

Run: `uv run pytest tests/test_escalation.py tests/test_mainline_scripted.py -q`
Expected: fails with an ImportError.

- [ ] **Step 3: Implement the escalation tool, front desk routing, the relay branch, the comment rule and the `mainline_replies` fixture**

- [ ] **Step 4: Confirm it passes**

Run: `uv run pytest -q`
Expected: all PASS.

- [ ] **Step 5: The author runs the full main line with real Claude (the implementer does not run it)**

This calls Claude about 25 times:

```bash
rm -f var/smoke/mainline.db && uv run clinic-sms --db var/smoke/mainline.db --script scenarios/mainline.txt 2>&1 | tee var/smoke/mainline.log
```

Expected:
- `SMOKE: OK (fallbacks=0, failed_jobs=0)`;
- each of the six steps behaves as in the scripted test above: the follow-up comes at Wed 8:00 AM; after going back, the visit reason is not asked again; the front desk receives `[#1] ...`; after the relay, the agent goes on to ask about allergies; at the end, intake is finished and the appointment is Mon 11/23 9:00 AM.

`"The first one works"` depends on the first slot Claude lists. It does not have to be 9:00 AM, but record which slot was actually chosen. Write the results into `### Task 9 (mainline)`.

- [ ] **Step 6: Commit**

```bash
git add src/clinic_sms tests scenarios/mainline.txt docs/findings.md
git commit -m "feat: escalate to the front desk and relay answers; demo main-line script"
```

---

### Task 10: LLM record and replay

**Files:**
- Create: `src/clinic_sms/recording.py`, `tests/test_recording.py`; `scenarios/mainline.jsonl` (recorded by the author)
- Modify: `src/clinic_sms/cli.py`, `src/clinic_sms/sim.py`, `src/clinic_sms/ctx.py`, `tests/test_scripted_llm.py`, `tests/test_cli.py`, `docs/findings.md`

**Interfaces:**
- Consumes: `normalize_ctx`, `tool_names`, how `ScriptedLLMStream` emits chunks (Plan 1); `Store.get_setting/set_setting` (Task 2); `make_claude_llm`.
- Produces:

```python
# recording.py
def request_fingerprint(chat_ctx: llm.ChatContext, tools: list[llm.Tool]) -> dict
    # {"ctx": [list(t) for t in normalize_ctx(chat_ctx)], "tools": tool_names(tools)}
class RecordReplayLLM(llm.LLM):
    def __init__(self, *, mode: Literal["record", "replay"], path: Path,
                 inner_factory: Callable[[], llm.LLM] | None = None,     # record only; built on first chat()
                 cursor: int = 0, on_cursor: Callable[[int], None] | None = None) -> None
    cursor: int              # calls recorded or replayed so far
    mismatch: str | None     # first replay problem, e.g. "replay mismatch at call 7: request differs at item 12"
# cli.py: --llm live|record|replay (default live), --recording PATH (default var/recordings/session.jsonl);
#         the /restart argv always appends ["--llm", mode, "--recording", path]
#         (update the expected argv in Task 2's test_restart_closes_store_then_execs)
```

- **Recording file**: JSONL, one call per line: `{"n": int, "request": fingerprint, "response": {"text": str, "tools": [[name, args_obj], ...]}}`.
- **record mode**: forward the inner stream's chunks unchanged while accumulating the text and tool calls, and append one line when the stream ends normally. On error nothing is written, and the error is re-raised as is.
- **replay mode**: take line number `cursor`. If the fingerprint differs, or there are no recorded calls left, set `mismatch` and raise `APIError(mismatch, retryable=False)`. `mismatch` contains only the call number and the item index, never any message text. When it matches, emit the recorded reply the way ScriptedLLM does.
- **cursor**: after each call, `on_cursor(cursor)`. The CLI stores it in the setting `llm_cursor`, so replay can continue after a real `/restart`. When record mode starts, `cursor` equals the number of lines already in the file, and new lines are appended after them.
- **CLI**: after each command finishes, check `llm.mismatch`. If it is set, print `REPLAY MISMATCH: <mismatch>`, then `SystemExit(2)`.
- **live mode**: no wrapper; each turn still calls `make_claude_llm()`.

Deferred item for `normalize_ctx`: when a function_call's arguments are not JSON, keep the string as is instead of crashing.

- [ ] **Step 1: Write the failing tests**

`tests/test_recording.py`:

```python
async def test_record_then_replay_gives_the_same_turns(tmp_path, intake_replies, ...):
    # record: inner = ScriptedLLM(intake_replies); start_intake + 2 answers through runners on db1
    # replay: no inner, fresh db2, same inputs -> identical ch.sent texts; recorder.cursor == number of lines
async def test_replay_mismatch_is_loud_and_carries_no_text(...):
    # replay with the 2nd answer changed -> that turn falls back; recorder.mismatch starts with "replay mismatch at call"
    # and contains neither the old nor the new patient text; no log record contains them
async def test_replay_past_the_recording_is_a_mismatch(...):
async def test_replay_cursor_survives_a_real_restart(tmp_path, monkeypatch, ...):
    # CLI with --llm replay and a script containing /restart (execv monkeypatched as in Task 2) -> same output as recorded
```

Additions to `tests/test_scripted_llm.py` (deferred items):
- `Reply.error` makes the call fail, `Reply.delay` delays the chunks, `default` catches keys that have no script, tool call ids within one instance are all distinct, and `contexts` records copies;
- add `match="unscripted input"` to `test_unscripted_input_raises`;
- test that `normalize_ctx` keeps the original string when the arguments are not JSON.

- [ ] **Step 2: Confirm it fails**

Run: `uv run pytest tests/test_recording.py tests/test_scripted_llm.py -q`
Expected: the new tests fail.

- [ ] **Step 3: Implement `recording.py`, the CLI arguments, cursor persistence, the exit on mismatch, and the `normalize_ctx` fix**

- [ ] **Step 4: Confirm it passes**

Run: `uv run pytest -q`
Expected: all PASS.

- [ ] **Step 5: The author records the demo main line (the implementer does not run it)**

This calls Claude about 25 times:

```bash
rm -f scenarios/mainline.jsonl var/demo/rec.db && mkdir -p var/demo && uv run clinic-sms --db var/demo/rec.db --llm record --recording scenarios/mainline.jsonl --script scenarios/mainline.txt 2>&1 | tee var/demo/record.log
```

Expected: `SMOKE: OK (fallbacks=0, failed_jobs=0)`, and all six steps of the main line are right. If not satisfied, record again.

- [ ] **Step 6: Offline replay (no key needed; the implementer can run it)**

Run:

```bash
rm -f var/demo/replay.db && env -u ANTHROPIC_API_KEY uv run clinic-sms --db var/demo/replay.db --llm replay --recording scenarios/mainline.jsonl --script scenarios/mainline.txt 2>&1 | tee var/demo/replay.log
```

Expected:
- `SMOKE: OK (fallbacks=0, failed_jobs=0)`, with no `REPLAY MISMATCH`;
- compare the two databases with `sqlite3`: the results of `select phone, direction, text, at from messages order by id` are identical.

- [ ] **Step 7: Write how to use replay into `### Task 10` of the findings, then commit**

Before committing, read through `scenarios/mainline.jsonl` and confirm it contains only fictional data.

```bash
git add src/clinic_sms tests scenarios/mainline.jsonl docs/findings.md
git commit -m "feat: record and replay LLM turns; recorded demo main line"
```

---

### Task 11: Behavior eval with the real model, and wrap-up

**Files:**
- Create: `evals/conftest.py`, `evals/test_mainline_eval.py`; depending on the Step 1 result, also `repro/finding_<n>.py`, `tests/test_llm.py`
- Modify: `src/clinic_sms/runner.py`, `src/clinic_sms/llm.py`, `README.md`, `docs/findings.md`

**Interfaces:**
- Consumes: all previous tasks; `livekit.agents.evals.task_completion_judge`; `RunResult.expect`, `ChatMessageAssert.judge`.
- Produces:

```python
# runner.py
ConversationRunner.__init__(..., run_listener: Callable[[str, RunResult], None] | None = None)
    # called with (phone, result) after each successful session.run(), before the commit
# llm.py (only if Step 1 reproduces)
def make_judge_llm() -> llm.LLM
```

Reading the 1.8.11 source raises one suspicion that has to be confirmed offline first. When the SDK's judge calls the LLM, it passes `extra_kwargs={"temperature": 0.0}` (`evals/judge.py` lines 153–156) and forces `tool_choice="required"` (lines 158–164). The plugin merges `extra_kwargs` into `messages.create(**extra)` (`plugins/anthropic/llm.py` lines 175–176, 299–304), but `messages.create` in `anthropic` 1.17 has no `temperature` parameter (checked with `inspect.signature` on 2026-11-13). So when Claude is used as the judge, it will likely raise `TypeError` before the request is even sent. Also, `claude-opus-5-5` is on the plugin's "does not support forced tool_choice" list, so `"required"` gets downgraded to `auto` with a warning (lines 50–55, 239–247).

- [ ] **Step 1: Confirm the judge problem offline**

Write `repro/finding_<n>.py`, using the next unused number for `<n>`. The script is standalone, offline, and needs no key:

- Construct the plugin's `LLM(model="claude-opus-5-5", client=...)` with `anthropic.AsyncClient(api_key="offline-repro", http_client=httpx.AsyncClient(transport=httpx.MockTransport(record)))`; this api_key is only a placeholder string, not a credential.
- Call `accuracy_judge(llm).evaluate(chat_ctx=<two messages>)`.
- Verdict: if a `TypeError` whose message mentions `temperature` is raised before MockTransport receives any request, print `REPRODUCED` and exit with code 1. If the request was sent, print whether the request body contains `temperature`, and exit with code 0.

Run: `uv run python repro/finding_<n>.py`

- **Reproduced**:
  1. Write the FINDING following §5.10, search upstream with a read-only `gh search`, and attach the links;
  2. Add `make_judge_llm()` to `llm.py`, returning a subclass of `anthropic.LLM` whose `chat()` first removes `temperature` from `extra_kwargs` (`# SDK-FINDING-<n>`);
  3. `tests/test_llm.py` tests with the same MockTransport: the request is sent, and it has no `temperature`.
- **Not reproduced**: delete this script; the eval uses `make_claude_llm()` directly; write one line in findings saying "suspicion ruled out", with the evidence.

- [ ] **Step 2: Write the `run_listener` test and implement it**

In `tests/test_runner.py`, add `test_run_listener_sees_each_inbound_run`: the `RunResult` the listener receives passes `.expect.contains_function_call(name="record_insurance")`; `start_intake` and proactive messages do not call it.

- [ ] **Step 3: Write the eval in `evals/`**

`evals/conftest.py`: `skip` the whole directory when there is no `ANTHROPIC_API_KEY`. `evals/` is not in `testpaths`, so `uv run pytest` does not run it.

`evals/test_mainline_eval.py` has a single test, `test_mainline_six_steps`, with `@pytest.mark.timeout(1200)`:
- A seeded database, SimClock, real Claude (`make_claude_llm`), `summarize_intake=True`;
- Like Task 9's `test_mainline_scripted.py`, read `scenarios/mainline.txt` line by line and call `Simulator.execute`; `"restart"` becomes an in-process restart, because `execv` cannot be used inside pytest;
- `run_listener` stores each inbound RunResult keyed by the input text; the judge uses `make_judge_llm()` (when Step 1 reproduces) or `make_claude_llm()`.

The assertions use two kinds of tools: `RunResult.expect` for structure and `judge` for wording.

| Step | Structural assertions | judge intent |
|---|---|---|
| 1 Reminder | one outbound at 10:00 AM; job done | call `task_completion_judge` once on the reminder text alone: "reminds of Thu 11/19 10:00 AM with Dr. Lee and offers 1 to confirm, 2 to reschedule" |
| 2 Handoff | the `"2, can I..."` run: `contains_function_call(name="transfer_to_reschedule")`, `contains_agent_handoff(new_agent_type=RescheduleAgent)`; the `"The first one works"` run: `contains_function_call(name="book_slot")`, `contains_agent_handoff(new_agent_type=IntakeAgent)` | the rescheduling reply: "offers specific open times next week, without slot ids" |
| 3 Intake | after the two answers, the progress results contain `insurance` and `visit_reason` | none |
| 4 Follow-up | the follow-up message time is Wed 8:00 AM; later calls contain no OPENING of InsuranceTask / VisitReasonTask | the follow-up: "asks only about allergies and medications" |
| 5 Going back | `contains_function_call(name="out_of_scope")`; at the end `member_id == "W999"` and pending is back to `allergies_meds` | none |
| 6 Escalation | `contains_function_call(name="escalate_to_staff")`; the front desk SMS starts with `[#1] Jane D. (555) 010-0001:`; the ticket is closed after the relay; at the end intake is done and the appointment has intake | the relay: "passes on that the $40 was a copay and nothing else is due, then asks about allergies/medications" |

Finally, use `task_completion_judge(judge_llm).evaluate(chat_ctx=<the whole conversation as a ChatContext of user/assistant messages>)` and require the verdict to be `pass`. When the judge returns `maybe` or `fail`, the test fails and the reasoning is printed.

- [ ] **Step 4: The author runs the eval (the implementer does not run it)**

This calls Claude about 35 times, including the judge:

```bash
mkdir -p var/evals && uv run pytest evals -s 2>&1 | tee var/evals/mainline.log
```

Expected: `1 passed`. If the judge reports `LLM did not return verdict arguments` because forced tool_choice was downgraded, record it in findings; whether to file a separate FINDING is up to the author after reading the log.

- [ ] **Step 5: Update the README and findings**

The README becomes:
- a one-sentence introduction;
- installation;
- tests: `uv run pytest`, and `uv run pytest evals`, which needs a key;
- all simulator commands;
- the demo: how to run `scenarios/mainline.txt` live, and how to run the offline replay (the command from Task 10 Step 6);
- the list of smoke scripts;
- links to the findings and the design document.

Add `## Behavior eval (Plan 2)` to findings, with the eval results and, for each judge call, the verdict and a one-sentence summary of the reasoning, without pasting the judge's output in full.

- [ ] **Step 6: Run all tests**

Run: `uv run pytest -q`
Expected: all PASS, and `evals/` is not collected.

- [ ] **Step 7: Commit**

```bash
git add src/clinic_sms evals tests README.md docs/findings.md
git commit -m "test: real-model behavior eval of the six-step main line; docs"
```

If Step 1 reproduced, also add `repro/finding_<n>.py` to this commit.
