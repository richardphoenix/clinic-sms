# Clinic SMS agent · Plan 1: cold-start conversation engine + resumable intake TaskGroup

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a conversation engine that cold-starts on every text message. The intake TaskGroup's progress and the conversation are stored in SQLite, so the process can restart between any two messages and resume asking from where it left off. Also add a minimal terminal simulator for trying it by hand.

**Architecture:** Each inbound message is processed in this order: lock by phone number → read state from SQLite → create a new `AgentSession` → rebuild the `IntakeAgent → ResumableTaskGroup → current step` chain from the state → wait for the innermost step to take over → `session.run()` → snapshot → write state and outbox in one transaction → deliver → close the session. `ResumableTaskGroup` is a subclass of the SDK's `TaskGroup` that can start from saved progress.

**Tech Stack:** Python 3.13 (managed by uv), `livekit-agents==1.8.11`, `livekit-plugins-anthropic==1.8.11`, pydantic v2, stdlib `sqlite3`, pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-11-12-clinic-sms-agent-design.md`

**Scope:** This is the first of two plans. It covers steps 1 and 2 of design §5.12, plus a minimal CLI. The second plan covers steps 3 to 7 of §5.12: the reception and rescheduling agents and their handoffs, reminders and follow-ups, escalation to a human, the full simulator with record and replay, and behavior evals. The second plan waits for the spike conclusions from Tasks 2, 3 and 7 of this plan, because the spikes may change how the resume mechanism is written.

## Global Constraints

- Python version: 3.13, installed with `uv python install 3.13`. The machine's `python3` is 3.15.2, but livekit-agents 1.8.11 requires `>=3.10,<3.15`, so it cannot be used.
- Dependencies are pinned exactly: `livekit-agents==1.8.11`, `livekit-plugins-anthropic==1.8.11`. Use only official PyPI releases. No patches, no local clone.
- Model: `claude-opus-5-5`, `max_tokens=8000`, no temperature.
- Credentials are read only from the `ANTHROPIC_API_KEY` environment variable. Never write them into code, and never ask for them in the conversation.
- All data is fictional. Phone numbers use only the 555-0100 to 555-0199 range, are stored internally as E.164 (`+15550100001`), and are displayed as `(555) 010-0001`. Times are displayed as `Thu 11/19, 10:00 AM`, in the `America/Los_Angeles` time zone.
- Text messages sent to patients are always in English.
- Fallback SMS text: `Sorry, something went wrong on our end — please reply again.`
- All code that works around an SDK problem is tagged `# SDK-FINDING-<n>` and has a matching entry in `docs/findings.md`.
- Do not file issues or PRs against livekit/agents.
- If a spike shows that cold start cannot work on the official SDK release and no workaround can be found, stop and report the conclusion to the author.
- Code comments are in English.

## Review Focus

1. **The LLM returns nothing at all for this turn** (the symptom of #7726). The patient should receive the fallback SMS, and state stays unchanged. Tested by `test_empty_reply_is_a_failure` in Task 8.
2. **The process is killed after state is committed but before the SMS is delivered.** After restart, the SMS must be re-sent, and sent only once. Tested by `test_pending_outbox_delivered_once_after_crash` in Task 8.
3. **Progress in the database points to a step id that no longer exists in the code** (common after code changes). It should quickly reply with the fallback SMS and leave state unchanged, not hang on the ready-signal timeout. Tested by `test_unknown_step_rejected_at_construction` in Task 6 and `test_unknown_step_in_saved_state_falls_back_fast` in Task 8.
4. **The patient sends a blank message.** Ignore it: no LLM call, no recording, no reply. Tested by `test_blank_inbound_is_ignored` in Task 8.
5. **Non-ASCII text (accented letters, emoji, dashes) goes through persistence.** What is stored must be read back exactly the same. Tested by `test_non_ascii_survives` in Task 4.

---

## File Structure

```
pyproject.toml / .python-version / .gitignore / README.md
src/clinic_sms/
  clock.py                         Clock protocol, SystemClock, SimClock, CLINIC_TZ
  fmt.py                           phone normalization and display, time display, SMS segment count
  ctx.py                           ChatContext serialization, normalization for comparison, extracting assistant text
  llm.py                           make_claude_llm()
  state.py                         persisted pydantic models
  store.py                         SQLite: conversations / messages / outbox
  channel.py                       Channel protocol, Outbound, RecordingChannel
  runner.py                        ConversationRunner (cold start on every message)
  cli.py                           minimal terminal simulator
  workflow/ready.py                ReadySignal: the innermost agent has taken over
  workflow/resumable_task_group.py ResumableTaskGroup
  agents/intake.py                 three intake steps + IntakeAgent (including snapshot)
  testing/scripted_llm.py          fake LLM that replies from a script (shared by tests and spikes)
tests/                             one test file per module; conftest.py holds the intake script
spikes/                            throwaway spike scripts, not maintained long term
docs/findings.md                   SDK problems and spike conclusions
```

---

### Task 1: Project skeleton, clock and formatting

**Files:**
- Create: `pyproject.toml` (generated by uv), `.python-version`, `.gitignore`, `src/clinic_sms/__init__.py`, `src/clinic_sms/clock.py`, `src/clinic_sms/fmt.py`
- Test: `tests/test_basics.py`

**Interfaces:**
- Produces:
  - `clock.CLINIC_TZ: ZoneInfo`, with the value `America/Los_Angeles`.
  - `clock.Clock` (Protocol): `now() -> datetime`, returns a timezone-aware time.
  - `clock.SystemClock()`.
  - `clock.SimClock(start: datetime)`: `.now()`, `.advance(delta: timedelta) -> None`.
  - `fmt.normalize_phone(raw: str) -> str`: returns E.164 format, raises `ValueError` if the input cannot be recognized.
  - `fmt.format_phone(e164: str) -> str`.
  - `fmt.format_when(dt: datetime) -> str`: converts to `CLINIC_TZ` first, then formats.
  - `fmt.sms_segments(text: str) -> int`.

- [ ] **Step 1: Initialize the repo and project**

```bash
git init
uv python install 3.13
uv init --package --python 3.13 --name clinic-sms .
uv add "livekit-agents==1.8.11" "livekit-plugins-anthropic==1.8.11" "pydantic>=2"
uv add --dev pytest pytest-asyncio
```

Add `[tool.pytest.ini_options]` to `pyproject.toml`: `asyncio_mode = "auto"`, `testpaths = ["tests"]`. Write `.venv/ var/ __pycache__/ .pytest_cache/` into `.gitignore`.

If `uv add` fails to resolve because the uv version is too old (the machine has 0.5.0), stop and ask the author whether to run `uv self update`. Do not upgrade on your own.

- [ ] **Step 2: Confirm the dependencies are installed**

Run: `uv run python -c "import livekit.agents as a, livekit.plugins.anthropic; print(a.__version__)"`
Expected: `1.8.11`

- [ ] **Step 3: Write the failing test `tests/test_basics.py`**

```python
def test_normalize_phone_accepts_common_us_formats():
    for raw in ["(555) 010-0001", "555-010-0001", "5550100001", "+15550100001", "1 555 010 0001"]:
        assert normalize_phone(raw) == "+15550100001"

def test_normalize_phone_rejects_garbage():
    with pytest.raises(ValueError):
        normalize_phone("12345")

def test_format_phone():
    assert format_phone("+15550100001") == "(555) 010-0001"

def test_format_when_uses_clinic_time():
    assert format_when(datetime(2026, 11, 19, 18, 0, tzinfo=timezone.utc)) == "Thu 11/19, 10:00 AM"
    assert format_when(datetime(2026, 11, 17, 17, 55, tzinfo=timezone.utc)) == "Tue 11/17, 9:55 AM"

@pytest.mark.parametrize("text,expected", [
    ("a" * 160, 1), ("a" * 161, 2), ("a" * 306, 2), ("a" * 307, 3),
    ("{" * 80, 1), ("{" * 81, 2),            # GSM-7 extension chars count as 2 septets
    ("😀" * 35, 1), ("😀" * 36, 2),          # UCS-2: 70 units / 67 per concatenated segment
    ("—" * 70, 1), ("—" * 71, 2),
])
def test_sms_segments(text, expected):
    assert sms_segments(text) == expected

def test_sim_clock_advances():
    c = SimClock(datetime(2026, 11, 17, 9, 55, tzinfo=CLINIC_TZ))
    c.advance(timedelta(minutes=5))
    assert c.now() == datetime(2026, 11, 17, 10, 0, tzinfo=CLINIC_TZ)
```

- [ ] **Step 4: Confirm the tests fail**

Run: `uv run pytest tests/test_basics.py -q`
Expected: fails with an ImportError.

- [ ] **Step 5: Implement the interfaces above in `clock.py` and `fmt.py`**

Algorithm for `sms_segments`:
- If every character is in the GSM 03.38 basic character set plus the extension table: septets = character count + extension character count; ≤160 counts as 1 segment, otherwise `ceil(septets / 153)`.
- Otherwise use UCS-2: units = number of UTF-16 code units; ≤70 counts as 1 segment, otherwise `ceil(units / 67)`.

The hour in `format_when` is not zero-padded.

- [ ] **Step 6: Confirm the tests pass**

Run: `uv run pytest tests/test_basics.py -q`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add .
git commit -m "chore: scaffold clinic-sms with clock and formatting helpers"
```

---

### Task 2: ScriptedLLM, and Spike 1 (verify R1/R2: cold start and readiness)

**Files:**
- Create: `src/clinic_sms/testing/__init__.py`, `src/clinic_sms/testing/scripted_llm.py`, `src/clinic_sms/ctx.py` (this task writes only `assistant_texts`), `spikes/cold_start_spike.py`, `docs/findings.md`
- Test: `tests/test_scripted_llm.py`

**Interfaces:**
- Produces:

```python
@dataclass
class Reply:
    text: str = ""
    tools: list[tuple[str, dict]] = field(default_factory=list)  # (tool name, JSON-able args)
    error: Exception | None = None   # raised instead of replying
    delay: float = 0.0               # seconds before the first chunk

class ScriptedLLM(livekit.agents.llm.LLM):
    def __init__(self, replies: dict[str, Reply], *, default: Reply | None = None) -> None
    calls: list[str]                          # key of every chat() call, in order
    contexts: list[livekit.agents.llm.ChatContext]  # chat_ctx.copy() of every call

def assistant_texts(ctx: ChatContext) -> list[str]   # in clinic_sms/ctx.py
```

- The key lookup rule is the same as `_get_index_text` in upstream `tests/fake_llm.py`: take the last item; if it is a user/system message, use its text; if it is a `function_call_output`, use its `output`.
- If the key is not found and there is no `default`, raise `APIError(f"unscripted input: {key!r}", retryable=False)`.
- Tool call ids are `call_<n>`, unique within one instance.
- If code is copied directly from upstream, keep the Apache-2.0 copyright notice.

- [ ] **Step 1: Write the failing test `tests/test_scripted_llm.py`**

```python
async def test_replies_to_user_text():
    llm = ScriptedLLM({"hi": Reply(text="hello")})
    async with AgentSession(llm=llm) as s:
        await s.start(Agent(instructions="test"))
        await asyncio.wait_for(s.run(user_input="hi"), 5)
        assert assistant_texts(s.history) == ["hello"]
    assert llm.calls == ["hi"]

async def test_tool_call_is_executed():
    seen: list[int] = []
    class A(Agent):
        def __init__(self): super().__init__(instructions="test")
        @function_tool
        async def echo(self, ctx: RunContext, x: int) -> str:
            seen.append(x); return "echoed"
    llm = ScriptedLLM({"go": Reply(tools=[("echo", {"x": 7})]), "echoed": Reply(text="done")})
    async with AgentSession(llm=llm) as s:
        await s.start(A())
        await asyncio.wait_for(s.run(user_input="go"), 5)
    assert seen == [7]

async def test_unscripted_input_raises():
    async with AgentSession(llm=ScriptedLLM({})) as s:
        await s.start(Agent(instructions="test"))
        with pytest.raises(APIError):
            await asyncio.wait_for(s.run(user_input="nope"), 5)
```

- [ ] **Step 2: Confirm it fails**

Run: `uv run pytest tests/test_scripted_llm.py -q`
Expected: fails with an ImportError.

- [ ] **Step 3: Implement `ScriptedLLM`, `Reply` and `assistant_texts`**

- [ ] **Step 4: Confirm it passes**

Run: `uv run pytest tests/test_scripted_llm.py -q`
Expected: 3 PASS. If `test_unscripted_input_raises` does not raise, or times out because of retries, record a FINDING in findings.md, then change `ScriptedLLM` to make the stream fail directly (for example, raise in `_run`).

- [ ] **Step 5: Write the spike script `spikes/cold_start_spike.py`**

Use `ScriptedLLM`. In the script, define a two-step TaskGroup (steps `a` and `b`, one `save(value)` tool per step). In each step's `on_enter`, first set an `asyncio.Event`; call `generate_reply(instructions=f"ask {id}")` only when `resuming=False`.

How to simulate "resume from a": use the stock `TaskGroup`, register only the steps not yet done, and construct the first step with `resuming=True`.

Each check prints `PASS`/`FAIL` and the observed symptom:

- **S1**: a new session; the RunResult that is awaited after `await session.start(root, capture_run=True)`. The collected outbound messages should be only a's question, and `session.current_agent` should be step a.
- **S2**: open another new session and resume from a: pass the `chat_ctx` that a had at the end of S1 to a. First wait for a's event, then `run(user_input="a1")`. It should return within 5 seconds; the outbound messages should be only b's question; `current_agent` should be b.
- **S3**: same as S2, but do not wait for the event after `start`; call `run()` immediately. Record who answered and whether anything raised.
- **S4**: in S2, count whether the old messages restored into the session triggered `conversation_item_added`.
- **S5**: print the type/role/text of every item in b's `chat_ctx` at the end of S2.
- Print `llm.calls` from S2, which is the order in which the LLM was actually called while completing a step.

- [ ] **Step 6: Run the spike**

Run: `uv run python spikes/cold_start_spike.py`
Expected: S1 and S2 are PASS; S3, S4, S5 and the call order are printed.

- [ ] **Step 7: Write the conclusions into `docs/findings.md`**

Start the file with the entry format (following design §5.10): `## FINDING-<n>: <title>`, followed by Symptom / Minimal repro / SDK version / Root cause / Classification / Workaround / Upstream status, in that order.

Add a separate section `## Spike 1 (R1/R2)` with the results of S1 to S5, and give a conclusion: "Whether the step's `on_enter` being called can serve as a ready signal for feeding messages."

**Gate:** If S2 cannot pass no matter how long it waits, stop and report to the author per Global Constraints.

- [ ] **Step 8: Commit**

```bash
git add src/clinic_sms/testing src/clinic_sms/ctx.py tests/test_scripted_llm.py spikes/cold_start_spike.py docs/findings.md
git commit -m "test: add ScriptedLLM and cold-start spike for R1/R2"
```

---

### Task 3: Wire up Claude, and Spike 2 (verify R6)

**Files:**
- Create: `src/clinic_sms/llm.py`, `spikes/claude_roundtrip.py`
- Modify: `docs/findings.md`

**Interfaces:**
- Produces: `MODEL = "claude-opus-5-5"`, `MAX_TOKENS = 8000`, `make_claude_llm() -> livekit.plugins.anthropic.LLM`.

- [ ] **Step 1: Implement `make_claude_llm()`**

Call `anthropic.LLM(model=MODEL, max_tokens=MAX_TOKENS)` without passing temperature; the API key uses the plugin's default lookup (the environment variable).

- [ ] **Step 2: Confirm credentials are available**

Run: `test -n "$ANTHROPIC_API_KEY" && echo ok || ant auth status`

If neither works, ask the author to `export ANTHROPIC_API_KEY=...` in their own shell. Do not have the author paste the key into the conversation. Before the first run, tell the author that this spike calls Claude about 6 to 10 times and costs on the order of a few cents.

- [ ] **Step 3: Write `spikes/claude_roundtrip.py`**

Copy the step structure from the Task 2 spike, swap in the real Claude, and change the opening questions to real wording (for example, ask for a color or a city). Run three cold starts:
1. Start: a asks its question;
2. Answer a: call the tool to complete a, b asks its question;
3. Answer b: complete.

For each turn, print: elapsed time, outbound text, which tools were called, all warning logs, and exceptions.

- [ ] **Step 4: Run it**

Run: `uv run python spikes/claude_roundtrip.py`
Expected: every turn has non-empty outbound text, both tools are called, and there are no exceptions.

- [ ] **Step 5: Write the conclusions into `## Spike 2 (R6)` in `docs/findings.md`**

Include: elapsed time per turn; whether there were 400 errors related to thinking / tool_use; empty replies; the plugin's warning about forced tool_choice.

Problems already covered by #7726, #6868 and PR #7755 are marked "known upstream": record them only, do not open a new FINDING.

**Gate:** If it cannot work and there is no workaround, stop and report to the author.

- [ ] **Step 6: Commit**

```bash
git add src/clinic_sms/llm.py spikes/claude_roundtrip.py docs/findings.md
git commit -m "feat: wire Claude via livekit-plugins-anthropic; record R6 spike"
```

---

### Task 4: State models and SQLite store

**Files:**
- Create: `src/clinic_sms/state.py`, `src/clinic_sms/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces:

```python
# state.py
class GroupProgress(BaseModel):
    pending: list[str]           # step ids still to run, current step first
    results: dict[str, dict]     # step id -> result.model_dump(mode="json")
    visited: list[str]
class IntakeState(BaseModel):
    done: bool = False
    progress: GroupProgress | None = None   # None: intake not started (or done)
    root_ctx: dict | None = None
    group_ctx: dict | None = None
    step_ctx: dict | None = None
    results: dict[str, dict] = {}           # final results once done
class ConversationState(BaseModel):
    phone: str                              # E.164
    intake: IntakeState = IntakeState()
    # helper: intake_started -> bool  (done or progress is not None)

# store.py
@dataclass(frozen=True)
class OutboxRow: id: int; phone: str; text: str
@dataclass(frozen=True)
class MessageRow: phone: str; direction: Literal["in", "out"]; text: str; at: datetime
class StaleStateError(Exception): ...
class Store:
    def __init__(self, path: str | Path) -> None            # creates schema if missing
    def load(self, phone: str) -> tuple[ConversationState, int] | None   # (state, version)
    def commit_turn(self, *, phone: str, expected_version: int | None, state: ConversationState,
                    inbound: str | None, outbound: list[str], at: datetime) -> list[OutboxRow]
    def log_only(self, *, phone: str, inbound: str | None, outbound: list[str], at: datetime) -> list[OutboxRow]
    def pending_outbox(self) -> list[OutboxRow]
    def mark_delivered(self, outbox_id: int, at: datetime) -> None
    def messages(self, phone: str) -> list[MessageRow]
```

Table schema:
- `conversations(phone TEXT PRIMARY KEY, version INTEGER NOT NULL, state_json TEXT NOT NULL, updated_at TEXT NOT NULL)`
- `messages(id INTEGER PRIMARY KEY, phone TEXT NOT NULL, direction TEXT NOT NULL CHECK (direction IN ('in','out')), text TEXT NOT NULL, at TEXT NOT NULL)`
- `outbox(id INTEGER PRIMARY KEY, phone TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL, delivered_at TEXT)`

`commit_turn` does the following in one transaction: write state, write message records, write the outbox. Version rules:
- `expected_version=None`: INSERT with version 1; if the row already exists, raise `StaleStateError`.
- Otherwise: `UPDATE ... WHERE version = ?`; if it affects 0 rows, raise `StaleStateError`; the new version = old version + 1.

On an error the whole transaction rolls back. Times are always stored in ISO 8601 format, with a time zone.

- [ ] **Step 1: Write the failing test `tests/test_store.py`**

```python
def test_commit_and_load_round_trip(tmp_path):
    s = Store(tmp_path / "db"); st = ConversationState(phone=P)
    s.commit_turn(phone=P, expected_version=None, state=st, inbound=None, outbound=["hi"], at=T)
    assert s.load(P) == (st, 1)
    s.commit_turn(phone=P, expected_version=1, state=st, inbound="yo", outbound=[], at=T)
    assert s.load(P)[1] == 2
    assert [(m.direction, m.text) for m in s.messages(P)] == [("out", "hi"), ("in", "yo")]

def test_stale_version_rejected_and_nothing_written(tmp_path):
    s = Store(tmp_path / "db"); st = ConversationState(phone=P)
    s.commit_turn(phone=P, expected_version=None, state=st, inbound=None, outbound=[], at=T)
    with pytest.raises(StaleStateError):
        s.commit_turn(phone=P, expected_version=7, state=st, inbound="x", outbound=["y"], at=T)
    assert s.messages(P) == [] and s.pending_outbox() == [] and s.load(P)[1] == 1

def test_insert_when_already_exists_is_stale(tmp_path): ...  # second expected_version=None raises

def test_outbox_pending_until_delivered(tmp_path):
    s = Store(tmp_path / "db")
    [row] = s.log_only(phone=P, inbound=None, outbound=["a"], at=T)
    assert s.pending_outbox() == [row]
    s.mark_delivered(row.id, T)
    assert s.pending_outbox() == []

def test_log_only_does_not_touch_state(tmp_path):
    s = Store(tmp_path / "db"); s.log_only(phone=P, inbound="x", outbound=["y"], at=T)
    assert s.load(P) is None

def test_non_ascii_survives(tmp_path):  # Review Focus 5
    s = Store(tmp_path / "db"); text = "José says 😀 — ok"
    st = ConversationState(phone=P, intake=IntakeState(results={"visit_reason": {"reason": text}}))
    s.commit_turn(phone=P, expected_version=None, state=st, inbound=text, outbound=[text], at=T)
    assert s.load(P)[0] == st
    assert {m.text for m in s.messages(P)} == {text}
```

Here `P = "+15550100001"` and `T = datetime(2026, 11, 17, 9, 55, tzinfo=CLINIC_TZ)`.

- [ ] **Step 2: Confirm it fails**

Run: `uv run pytest tests/test_store.py -q`
Expected: fails with an ImportError.

- [ ] **Step 3: Implement `state.py` and `store.py`**

Use a single `sqlite3` connection, and use `with conn:` for transactions.

- [ ] **Step 4: Confirm it passes**

Run: `uv run pytest tests/test_store.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/clinic_sms/state.py src/clinic_sms/store.py tests/test_store.py
git commit -m "feat: add conversation state models and SQLite store with outbox"
```

---

### Task 5: ResumableTaskGroup and ReadySignal

**Files:**
- Create: `src/clinic_sms/workflow/__init__.py`, `src/clinic_sms/workflow/ready.py`, `src/clinic_sms/workflow/resumable_task_group.py`
- Modify: `docs/findings.md` (add FINDING-1)
- Test: `tests/test_resumable_task_group.py`

**Interfaces:**
- Consumes: `GroupProgress` (Task 4), `ScriptedLLM` and `Reply` (Task 2).
- Produces:

```python
# ready.py
class ReadySignal:
    def set(self, agent: Agent) -> None             # first call wins
    async def wait(self, timeout: float) -> Agent   # raises TimeoutError

# resumable_task_group.py
StepFactory = Callable[[bool], AgentTask]           # argument: resuming
class UnknownStepError(ValueError): ...
class ResumableTaskGroup(TaskGroup):
    def __init__(self, *, progress: GroupProgress | None = None,
                 step_chat_ctx: llm.ChatContext | None = None,
                 summarize_chat_ctx: bool = True,
                 chat_ctx: NotGivenOr[llm.ChatContext] = NOT_GIVEN,
                 on_task_completed: Callable[[TaskCompletedEvent], Coroutine[None, None, None]] | None = None) -> None
    def add_step(self, factory: StepFactory, *, id: str, description: str,
                 result_type: type[BaseModel]) -> Self
    def validate_progress(self) -> None             # raises UnknownStepError
    @property
    def progress(self) -> GroupProgress             # live snapshot
    @property
    def current_task(self) -> AgentTask | None
    @property
    def current_step_id(self) -> str | None
```

Besides recording its own registration info, `add_step` also puts a `_FactoryInfo(task_factory=lambda: factory(False), id=id, description=description)` into the parent class's `_registered_factories`, so that the inherited `_build_out_of_scope_tool` works unchanged.

`on_enter` is based on 1.8.11's `TaskGroup.on_enter`, changing only four places:
1. When `progress` is given: `task_stack = list(progress.pending)`; `task_results` is restored from JSON using each step's `result_type.model_validate`; `self._visited_tasks = set(progress.visited)`.
2. Only the first task when resuming from progress is constructed with `factory(True)`; all others use `factory(False)`.
3. For that resumed first task, if `step_chat_ctx` is given, use it in place of `self.chat_ctx.copy()`.
4. Before each task starts, update the current step id and set `pending` to "the current step plus the rest of the stack"; after each task completes, write the result's JSON into `results`.

The comment at the top of the file must list these four differences from upstream one by one, along with the private names it uses, `_OutOfScopeError` and `_FactoryInfo`.

Progress is updated live in memory. At the end of each message, the runner writes it to SQLite in one go when it commits the transaction (§5.3 step 7). Under route A this is equivalent to design §5.5's "record on every step completed and every step started".

- [ ] **Step 1: Write the failing test `tests/test_resumable_task_group.py`**

The test defines these helper classes itself:
- `Val(BaseModel): value: str`.
- `Step(AgentTask[Val])`: `on_enter` first appends `(id, resuming)` to `log`, then calls `ready.set(self)`, and calls `generate_reply(instructions=f"ask {id}")` when not resuming; the tool `save(value: str)` completes the task and returns `f"saved {id}"`.
- `Root(Agent)`: in `on_enter`, awaits the group passed in and stores the result in `self.result`.

Give `ScriptedLLM` `default=Reply()`, because whether the LLM is called again after a tool output is up to the SDK; the tests check only the results.

```python
async def test_runs_steps_in_order_and_returns_typed_results(): ...
    # inputs "a1" -> save(value="a1"), "b1" -> save(value="b1")
    assert root.result.task_results == {"a": Val(value="a1"), "b": Val(value="b1")}

async def test_progress_reflects_current_step(): ...
    # after run("a1"), while b waits:
    assert group.progress == GroupProgress(pending=["b"], results={"a": {"value": "a1"}}, visited=["a", "b"])
    assert group.current_step_id == "b"

async def test_resume_skips_completed_and_flags_first_step(): ...
    # progress=GroupProgress(pending=["b"], results={"a": {"value": "a1"}}, visited=["a", "b"])
    assert log == [("b", True)]                      # a never constructed, b not asked
    # after run("b1"):
    assert root.result.task_results == {"a": Val(value="a1"), "b": Val(value="b1")}

async def test_resume_keeps_out_of_scope_targets(): ...
    # resumed at b; "redo a" -> Reply(tools=[("out_of_scope", {"task_ids": ["a"]})])
    assert log[-1] == ("a", False)
    assert group.progress.pending == ["a", "b"]

async def test_resumed_step_uses_step_chat_ctx(): ...
    # step_chat_ctx contains a user message "earlier text"
    assert "earlier text" in assistant_or_user_texts(current_step.chat_ctx)

def test_unknown_step_in_progress_raises():
    g = ResumableTaskGroup(progress=GroupProgress(pending=["zzz"], results={}, visited=[]))
    g.add_step(make_step("a"), id="a", description="A", result_type=Val)
    with pytest.raises(UnknownStepError):
        g.validate_progress()
```

`assistant_or_user_texts` is a one-line list comprehension in the test file: it takes the `text_content` of all message items.

- [ ] **Step 2: Confirm it fails**

Run: `uv run pytest tests/test_resumable_task_group.py -q`
Expected: fails with an ImportError.

- [ ] **Step 3: Implement `ReadySignal` and `ResumableTaskGroup`**

- [ ] **Step 4: Confirm it passes**

Run: `uv run pytest tests/test_resumable_task_group.py -q`
Expected: all PASS.

- [ ] **Step 5: Write FINDING-1 in `docs/findings.md`**

- Title: "TaskGroup cannot start from saved progress".
- Root cause: progress is a local variable of `on_enter`, so a subclass can only copy the whole `on_enter` and import private names.
- Classification: feature gap.
- Upstream status: searched on 2026-11-12; no related issue or PR.
- Workaround: `workflow/resumable_task_group.py`.
- Minimal repro: a description that does not depend on this project, plus links to the relevant lines in the 1.8.11 source.

- [ ] **Step 6: Commit**

```bash
git add src/clinic_sms/workflow tests/test_resumable_task_group.py docs/findings.md
git commit -m "feat: add ResumableTaskGroup that can start from saved progress"
```

---

### Task 6: Intake agent and the three steps

**Files:**
- Create: `src/clinic_sms/agents/__init__.py`, `src/clinic_sms/agents/intake.py`, `tests/conftest.py`
- Test: `tests/test_intake_agent.py`

**Interfaces:**
- Consumes: `ResumableTaskGroup`, `StepFactory`, `ReadySignal`, `UnknownStepError` (Task 5); `IntakeState`, `GroupProgress` (Task 4).
- Produces:

```python
SMS_STYLE = ("You are texting a patient of Maple Street Family Clinic by SMS. "
             "Reply in plain English text, no markdown, at most two short sentences.")
class InsuranceInfo(BaseModel): carrier: str; member_id: str
class VisitReason(BaseModel): reason: str
class AllergiesMeds(BaseModel): allergies: list[str]; medications: list[str]

class IntakeStep(AgentTask[ResultT]):
    OPENING: ClassVar[str]
    def __init__(self, *, instructions: str, resuming: bool, ready: ReadySignal) -> None
    # on_enter: ready.set(self); if not resuming: self.session.generate_reply(instructions=self.OPENING)

class InsuranceTask(IntakeStep[InsuranceInfo]):       # tool record_insurance(carrier: str, member_id: str) -> "insurance recorded"
class VisitReasonTask(IntakeStep[VisitReason]):       # tool record_visit_reason(reason: str) -> "visit reason recorded"
class AllergiesMedsTask(IntakeStep[AllergiesMeds]):   # tool record_allergies_and_medications(allergies: list[str], medications: list[str]) -> "allergies and medications recorded"

INTAKE_STEPS = [  # (id, description, task class, result type) in order
    ("insurance", "Collect the patient's insurance carrier and member ID", InsuranceTask, InsuranceInfo),
    ("visit_reason", "Collect the reason for the visit", VisitReasonTask, VisitReason),
    ("allergies_meds", "Collect allergies and current medications", AllergiesMedsTask, AllergiesMeds),
]

class IntakeAgent(Agent):
    COMPLETION: ClassVar[str]
    def __init__(self, *, state: IntakeState, ready: ReadySignal, summarize: bool = True) -> None
    state: IntakeState                     # updated in place when the group completes
    group: ResumableTaskGroup | None       # None when state.done
```

Fixed wording:
- `InsuranceTask.OPENING = "Ask the patient for their insurance carrier and member ID."`
- `VisitReasonTask.OPENING = "Ask the patient what brings them in for this visit."`
- `AllergiesMedsTask.OPENING = "Ask the patient about any allergies and the medications they currently take."`
- `IntakeAgent.COMPLETION = "Tell the patient their intake is complete and the clinic will see them at their appointment."`

Each step's instructions are written by the implementer (design §6). Requirements: include `SMS_STYLE`; call the recording tool only when all of that step's fields have been collected.

Behavior of `IntakeAgent.__init__`:
- When `state.done` is true, do not build a group.
- Otherwise build the group here and call `validate_progress()`, so that bad state raises before the session starts.

Behavior of `on_enter`:
- `state.done`: `ready.set(self)`, then return.
- Otherwise `await self.group`, then: set `state` to `done=True`; store each step's `model_dump(mode="json")` in `results`; clear `progress`, `group_ctx` and `step_ctx`; finally `generate_reply(instructions=COMPLETION)`.

- [ ] **Step 1: Write the intake script fixture in `tests/conftest.py`**

```python
@pytest.fixture
def intake_replies() -> dict[str, Reply]:
    return {
        InsuranceTask.OPENING: Reply(text="Hi Jane! What's your insurance carrier and member ID?"),
        "Aetna, member W123456789": Reply(tools=[("record_insurance", {"carrier": "Aetna", "member_id": "W123456789"})]),
        "Aetna": Reply(text="Got it. What's your member ID?"),
        "W123456789": Reply(tools=[("record_insurance", {"carrier": "Aetna", "member_id": "W123456789"})]),
        VisitReasonTask.OPENING: Reply(text="Thanks! What brings you in?"),
        "Follow-up on my knee": Reply(tools=[("record_visit_reason", {"reason": "Follow-up on my knee"})]),
        AllergiesMedsTask.OPENING: Reply(text="Any allergies or current medications?"),
        "Penicillin; ibuprofen": Reply(tools=[("record_allergies_and_medications", {"allergies": ["penicillin"], "medications": ["ibuprofen"]})]),
        IntakeAgent.COMPLETION: Reply(text="You're all set. See you Thursday!"),
        "Actually my member ID is W999": Reply(tools=[("out_of_scope", {"task_ids": ["insurance"]})]),
        "Aetna, W999": Reply(tools=[("record_insurance", {"carrier": "Aetna", "member_id": "W999"})]),
    }
```

Put two more things in conftest: an `intake_llm(intake_replies)` fixture that returns `ScriptedLLM(intake_replies, default=Reply())`, and `P = "+15550100001"`.

- [ ] **Step 2: Write the failing test `tests/test_intake_agent.py`**

```python
async def test_full_intake_collects_typed_results(intake_llm):
    # long-lived session, summarize=False; run the three answers in order
    assert agent.state.done
    assert agent.state.results == {
        "insurance": {"carrier": "Aetna", "member_id": "W123456789"},
        "visit_reason": {"reason": "Follow-up on my knee"},
        "allergies_meds": {"allergies": ["penicillin"], "medications": ["ibuprofen"]},
    }
    assert assistant_texts(session.history)[-1] == "You're all set. See you Thursday!"

async def test_resuming_step_does_not_ask(intake_llm):
    # state.progress = GroupProgress(pending=["visit_reason", "allergies_meds"],
    #   results={"insurance": {"carrier": "Aetna", "member_id": "W123456789"}},
    #   visited=["insurance", "visit_reason"])
    assert isinstance(await ready.wait(5), VisitReasonTask)
    assert VisitReasonTask.OPENING not in intake_llm.calls

async def test_completion_with_summary(intake_replies):
    # summarize=True, ScriptedLLM(intake_replies, default=Reply(text="Patient completed intake."))
    assert agent.state.done
    assert IntakeAgent.COMPLETION in llm.calls

def test_unknown_step_rejected_at_construction():  # Review Focus 3
    bad = IntakeState(progress=GroupProgress(pending=["zzz"], results={}, visited=[]))
    with pytest.raises(UnknownStepError):
        IntakeAgent(state=bad, ready=ReadySignal())
```

- [ ] **Step 3: Confirm it fails**

Run: `uv run pytest tests/test_intake_agent.py -q`
Expected: fails with an ImportError.

- [ ] **Step 4: Implement `agents/intake.py`**

- [ ] **Step 5: Confirm it passes**

Run: `uv run pytest tests/test_intake_agent.py -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/clinic_sms/agents tests/conftest.py tests/test_intake_agent.py
git commit -m "feat: add intake agent with insurance, visit reason and allergies steps"
```

---

### Task 7: Snapshot and restore (layered storage of conversation context)

**Files:**
- Modify: `src/clinic_sms/ctx.py`, `src/clinic_sms/agents/intake.py`, `docs/findings.md`
- Create: `spikes/compare_ctx_with_stock.py`
- Test: `tests/test_snapshot_restore.py`

**Interfaces:**
- Consumes: `IntakeAgent`, `IntakeState` (Tasks 6 and 4).
- Produces:

```python
# ctx.py
def dump_ctx(ctx: llm.ChatContext) -> dict
def load_ctx(data: dict) -> llm.ChatContext
def normalize_ctx(ctx: llm.ChatContext) -> list[tuple]
    # ("message", role, text) | ("function_call", name, canonical-json args)
    # | ("function_call_output", name, output) | (item.type,) — no ids, no timestamps
# intake.py
IntakeAgent.snapshot(self) -> IntakeState   # call after a run, before session.aclose()
```

`IntakeAgent.__init__` changes to restore each layer from `state`:
- `root_ctx` becomes its own `chat_ctx`;
- `group_ctx` becomes the group's `chat_ctx`;
- `step_ctx` becomes `step_chat_ctx`.

Output of `snapshot()`:
- When not finished: `progress = group.progress`, plus a dump of the three context layers: its own, `group.chat_ctx`, and `group.current_task.chat_ctx`.
- When finished: `done=True`, with `results` and `root_ctx`.

Which items `dump_ctx` stores is up to the implementer; the equivalence tests below are the standard.

- [ ] **Step 1: Write the failing test `tests/test_snapshot_restore.py`**

```python
async def test_round_trip_between_steps(intake_llm):
    # session A: fresh IntakeState, start(capture_run=True), run("Aetna, member W123456789")
    expected = normalize_ctx(session_a.current_agent.chat_ctx)
    snap = IntakeState.model_validate_json(agent_a.snapshot().model_dump_json())
    # close A; session B: IntakeAgent(state=snap, ready=ReadySignal()), start, ready.wait(5)
    assert isinstance(session_b.current_agent, VisitReasonTask)
    assert normalize_ctx(session_b.current_agent.chat_ctx) == expected

async def test_round_trip_mid_step(intake_llm):
    # session A: start, run("Aetna") -> still InsuranceTask
    # ...same equivalence assertion, plus:
    assert ("message", "user", "Aetna") in normalize_ctx(session_b.current_agent.chat_ctx)
    assert ("message", "assistant", "Got it. What's your member ID?") in normalize_ctx(session_b.current_agent.chat_ctx)

async def test_snapshot_after_completion(intake_llm):
    snap = agent.snapshot()
    assert snap.done and snap.progress is None and set(snap.results) == {"insurance", "visit_reason", "allergies_meds"}
```

- [ ] **Step 2: Confirm it fails**

Run: `uv run pytest tests/test_snapshot_restore.py -q`
Expected: fails because `snapshot` or `dump_ctx` does not exist.

- [ ] **Step 3: Implement `dump_ctx` / `load_ctx` / `normalize_ctx` and `IntakeAgent.snapshot()`, plus the restore in the constructor**

When the equivalence does not hold (for example, duplicated instructions or an extra handoff item), adjust which items `dump_ctx` stores. **Do not loosen `normalize_ctx` just to make the tests pass.**

If the difference is imposed by the SDK and cannot be removed: record a FINDING-n, and narrow the comparison only for that kind of item, with the comment `# SDK-FINDING-n` next to it.

- [ ] **Step 4: Confirm it passes**

Run: `uv run pytest tests/test_snapshot_restore.py -q`
Expected: all PASS.

- [ ] **Step 5: Write and run the one-off comparison script `spikes/compare_ctx_with_stock.py`**

Run the same script of three questions and three answers twice:
- (a) one long-lived session, using the stock `TaskGroup`, with steps registered using `factory(False)`;
- (b) cold start on every message, using `snapshot()` plus restore.

At the end of each turn, print the `normalize_ctx` differences between the two sides for the current step and the group.

Run: `uv run python spikes/compare_ctx_with_stock.py`

Write the result into `## Spike 3 (stock vs cold-start context)` in `docs/findings.md`. This script is not maintained long term (design §5.9).

- [ ] **Step 6: Commit**

```bash
git add src/clinic_sms/ctx.py src/clinic_sms/agents/intake.py tests/test_snapshot_restore.py spikes/compare_ctx_with_stock.py docs/findings.md
git commit -m "feat: snapshot and restore intake chat context layers"
```

---

### Task 8: ConversationRunner (cold start on every message)

**Files:**
- Create: `src/clinic_sms/channel.py`, `src/clinic_sms/runner.py`
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: `Store`, `ConversationState`, `StaleStateError` (Task 4); `IntakeAgent`, `snapshot()` (Tasks 6 and 7); `ReadySignal` (Task 5); `Clock` (Task 1); `ScriptedLLM` (Task 2).
- Produces:

```python
# channel.py
@dataclass(frozen=True)
class Outbound: to: str; text: str
class Channel(Protocol):
    async def send(self, msg: Outbound) -> None
class RecordingChannel:      # sent: list[Outbound]
    async def send(self, msg: Outbound) -> None

# runner.py
FALLBACK_TEXT = "Sorry, something went wrong on our end — please reply again."
NO_CONVERSATION_TEXT = "Sorry, we don't have an open request for this number."
RUN_TIMEOUT_S = 60.0
class EmptyReplyError(RuntimeError): ...
class ConversationRunner:
    def __init__(self, *, store: Store, channel: Channel, clock: Clock,
                 llm_factory: Callable[[], llm.LLM], ready_timeout: float = 10.0,
                 summarize_intake: bool = True) -> None
    async def start_intake(self, phone: str) -> list[str]
    async def handle_inbound(self, phone: str, text: str) -> list[str]
    async def deliver_pending(self) -> int
```

Behavior of `handle_inbound` (corresponds to design §5.3):
1. If `text.strip()` is empty: return `[]` immediately, with no LLM call and nothing written.
2. Acquire the `asyncio.Lock` for this phone number.
3. Read the state. If there is no state, or intake has not started: `log_only(inbound=text, outbound=[NO_CONVERSATION_TEXT])`, then deliver and return.
4. Inside a `try`, do the following in order:
   - create a new `ReadySignal`;
   - construct `IntakeAgent(state=..., ready=..., summarize=summarize_intake)`;
   - create a new `AgentSession(llm=llm_factory())`;
   - subscribe to `conversation_item_added` and collect messages whose role is assistant and whose text is non-empty. If the Task 2 spike's S4 found that old messages restored into the session also trigger this event, collect only the items added after `start`;
   - `start`;
   - `await ready.wait(ready_timeout)`;
   - `await asyncio.wait_for(session.run(user_input=text), RUN_TIMEOUT_S)`;
   - if there is no outbound message at all, raise `EmptyReplyError`;
   - `snapshot()`;
   - `commit_turn(expected_version=version, ...)`.
5. On any exception: `logging.exception`, then `log_only(inbound=text, outbound=[FALLBACK_TEXT])`. State is left untouched.
6. `finally`: `asyncio.wait_for(session.aclose(), 10)`; ignore a timeout.
7. Deliver the outbox rows just written (`mark_delivered` after `send`), and return the outbound texts.

Behavior of `start_intake`:
- If intake has already started or is already done: return `[]`.
- Otherwise use `session.start(root, capture_run=True)` to get the RunResult and await it, then snapshot, then `commit_turn(inbound=None, ...)`.
- On error, only log it and return `[]`. The patient did not send a message, so no fallback SMS is sent.

`deliver_pending`: send every undelivered outbox row, mark it delivered, and return how many were sent.

- [ ] **Step 1: Write the failing test `tests/test_runner.py`**

The helper `make_runner(db, llm, ch)` builds a new `Store` and `ConversationRunner` every time (arguments: `SystemClock()`, `summarize_intake=False`, `ready_timeout=2`). Each call is equivalent to restarting the process once.

```python
async def test_start_intake_sends_first_question(tmp_path, intake_llm):
    assert await make_runner(db, intake_llm, ch).start_intake(P) == ["Hi Jane! What's your insurance carrier and member ID?"]

async def test_restart_between_steps_does_not_reask(tmp_path, intake_llm):       # spec §5.9 #1
    await make_runner(...).start_intake(P)
    assert await make_runner(...).handle_inbound(P, "Aetna, member W123456789") == ["Thanks! What brings you in?"]
    assert await make_runner(...).handle_inbound(P, "Follow-up on my knee") == ["Any allergies or current medications?"]
    assert intake_llm.calls.count(InsuranceTask.OPENING) == 1
    assert set(Store(db).load(P)[0].intake.progress.results) == {"insurance", "visit_reason"}

async def test_restart_mid_step(tmp_path, intake_llm):                           # spec §5.9 #2
    # start; "Aetna" -> "Got it. What's your member ID?"; restart; "W123456789"
    ctx = normalize_ctx(intake_llm.contexts[intake_llm.calls.index("W123456789")])
    assert ("message", "user", "Aetna") in ctx
    assert ("message", "assistant", "Got it. What's your member ID?") in ctx
    assert Store(db).load(P)[0].intake.progress.results["insurance"]["member_id"] == "W123456789"

async def test_regression_after_restart(tmp_path, intake_llm):                   # spec §5.9 #3
    # start; "Aetna, member W123456789"; restart; "Actually my member ID is W999"
    assert sent_last == "Hi Jane! What's your insurance carrier and member ID?"
    # restart; "Aetna, W999"
    assert sent_last == "Thanks! What brings you in?"
    assert Store(db).load(P)[0].intake.progress.results["insurance"]["member_id"] == "W999"

async def test_failure_keeps_state_and_sends_fallback(tmp_path, intake_replies): # spec §5.9 #6
    # intake_replies["boom"] = Reply(error=APIError("down", retryable=False)); at visit_reason
    before = Store(db).load(P)
    assert await make_runner(...).handle_inbound(P, "boom") == [FALLBACK_TEXT]
    assert Store(db).load(P) == before

async def test_empty_reply_is_a_failure(tmp_path, intake_replies):              # Review Focus 1
    # intake_replies["..."] = Reply(text="")
    assert await make_runner(...).handle_inbound(P, "...") == [FALLBACK_TEXT]
    assert Store(db).load(P) == before

async def test_pending_outbox_delivered_once_after_crash(tmp_path, intake_llm): # Review Focus 2
    Store(db).commit_turn(phone=P, expected_version=None, state=ConversationState(phone=P),
                          inbound=None, outbound=["hello"], at=T)            # crash before delivery
    r = make_runner(db, intake_llm, ch)
    assert await r.deliver_pending() == 1 and [m.text for m in ch.sent] == ["hello"]
    assert await r.deliver_pending() == 0

async def test_unknown_step_in_saved_state_falls_back_fast(tmp_path, intake_llm):  # Review Focus 3
    # commit a state whose intake.progress.pending == ["zzz"]
    assert await asyncio.wait_for(make_runner(...).handle_inbound(P, "hello"), 2) == [FALLBACK_TEXT]
    assert Store(db).load(P) == before

async def test_blank_inbound_is_ignored(tmp_path, intake_llm):                  # Review Focus 4
    assert await make_runner(...).handle_inbound(P, "   ") == []
    assert intake_llm.calls == [] and Store(db).messages(P) == []

async def test_same_phone_messages_are_serialized(tmp_path, intake_replies):
    # at visit_reason; intake_replies["Follow-up on my knee"].delay = 0.2; one runner, gather two inbound
    await asyncio.gather(r.handle_inbound(P, "Follow-up on my knee"), r.handle_inbound(P, "Penicillin; ibuprofen"))
    state, version = Store(db).load(P)
    assert state.intake.done and version == before_version + 2

async def test_unknown_phone_gets_fixed_reply(tmp_path, intake_llm):
    assert await make_runner(...).handle_inbound("+15550100009", "hi") == [NO_CONVERSATION_TEXT]
    assert intake_llm.calls == []
```

- [ ] **Step 2: Confirm it fails**

Run: `uv run pytest tests/test_runner.py -q`
Expected: fails with an ImportError.

- [ ] **Step 3: Implement `channel.py` and `runner.py`**

- [ ] **Step 4: Confirm it passes**

Run: `uv run pytest tests/test_runner.py -q`
Expected: all PASS.

If `test_restart_mid_step` or `test_regression_after_restart` fails and the cause is SDK behavior (not our code), record a FINDING in the §5.10 format.

- [ ] **Step 5: Run all tests**

Run: `uv run pytest -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/clinic_sms/channel.py src/clinic_sms/runner.py tests/test_runner.py
git commit -m "feat: add per-message cold-start conversation runner"
```

---

### Task 9: Minimal terminal simulator and README

**Files:**
- Create: `src/clinic_sms/cli.py`
- Modify: `README.md` (generated by `uv init` in Task 1), `pyproject.toml` (add `[project.scripts] clinic-sms = "clinic_sms.cli:main"`), `docs/findings.md`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `ConversationRunner` (Task 8), `make_claude_llm` (Task 3), `fmt.*` (Task 1), `Store` (Task 4).
- Produces:

```python
@dataclass(frozen=True)
class Command:
    name: Literal["say", "as", "start-intake", "state", "restart", "quit", "help"]
    arg: str = ""
def parse_command(line: str) -> Command          # no leading "/" -> Command("say", line); unknown "/x" -> Command("help")
def format_message_line(*, at: datetime, direction: Literal["in", "out"], phone: str, text: str) -> str
class CliChannel:                                 # Channel that prints format_message_line(direction="out")
def main(argv: list[str] | None = None) -> None   # flags: --db PATH (default var/clinic.db), --as PHONE (default +15550100001)
```

The CLI in this plan uses `SystemClock`. Simulated time is left to the second plan.

- [ ] **Step 1: Write the failing test `tests/test_cli.py`**

```python
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
```

- [ ] **Step 2: Confirm it fails**

Run: `uv run pytest tests/test_cli.py -q`
Expected: fails with an ImportError.

- [ ] **Step 3: Implement `cli.py`**

- At startup, call `runner.deliver_pending()` once first.
- Read input with `await asyncio.to_thread(input, f"{format_phone(phone)}> ")`.
- Plain text: print the inbound line first, then `handle_inbound`.
- `/as`: switch the current phone number using `normalize_phone`; on failure, print an error.
- `/start-intake`: `runner.start_intake(phone)`.
- `/state`: print `done`, `progress` and `results`; do not print the context dicts.
- `/restart`: `os.execv(sys.executable, [sys.executable, "-m", "clinic_sms.cli", "--db", db, "--as", phone])`.
- `/quit`: exit.
- `/help`: list all commands.

Create the database's directory automatically if it does not exist.

- [ ] **Step 4: Confirm it passes**

Run: `uv run pytest -q`
Expected: all PASS.

- [ ] **Step 5: Verify by hand with the real Claude (requires `ANTHROPIC_API_KEY`)**

```bash
uv run clinic-sms --db var/demo.db
```

Enter, in order: `/start-intake`, `Aetna, member W123456789`, `/restart`, `Follow-up on my knee`, `/state`.

Expected:
- After `/restart`, the agent asks about allergies and medications, and does not re-ask about insurance or the visit reason;
- `/state` shows results for `insurance` and `visit_reason`.

Write the actual results into `## Manual check (Plan 1)` in `docs/findings.md`.

- [ ] **Step 6: Write README.md**

Contents:
- a one-sentence introduction;
- environment setup: `uv python install 3.13`, `uv sync`, `export ANTHROPIC_API_KEY=...`;
- how to run the tests: `uv run pytest`;
- the demo steps from Step 5;
- links to the design doc and `docs/findings.md`.

- [ ] **Step 7: Commit**

```bash
git add src/clinic_sms/cli.py tests/test_cli.py README.md pyproject.toml docs/findings.md
git commit -m "feat: add minimal terminal simulator with restart"
```
