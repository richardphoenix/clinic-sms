from datetime import datetime

import pytest

from clinic_sms.clock import CLINIC_TZ
from clinic_sms.state import ConversationState, IntakeState
from clinic_sms.store import StaleStateError, Store

P = "+15550100001"
T = datetime(2026, 11, 17, 9, 55, tzinfo=CLINIC_TZ)


@pytest.fixture
def s(tmp_path):
    with Store(tmp_path / "db") as store:
        yield store


def test_commit_and_load_round_trip(s):
    st = ConversationState(phone=P)
    s.commit_turn(phone=P, expected_version=None, state=st, inbound=None, outbound=["hi"], at=T)
    assert s.load(P) == (st, 1)
    s.commit_turn(phone=P, expected_version=1, state=st, inbound="yo", outbound=[], at=T)
    assert s.load(P)[1] == 2
    assert [(m.direction, m.text) for m in s.messages(P)] == [("out", "hi"), ("in", "yo")]
    assert s.messages(P)[0].at == T


def test_stale_version_rejected_and_nothing_written(s):
    st = ConversationState(phone=P)
    s.commit_turn(phone=P, expected_version=None, state=st, inbound=None, outbound=[], at=T)
    with pytest.raises(StaleStateError) as ei:
        s.commit_turn(phone=P, expected_version=7, state=st, inbound="x", outbound=["y"], at=T)
    assert P not in str(ei.value)  # the runner logs this message
    assert s.messages(P) == [] and s.pending_outbox() == [] and s.load(P)[1] == 1


def test_insert_when_already_exists_is_stale(s):
    st = ConversationState(phone=P)
    s.commit_turn(phone=P, expected_version=None, state=st, inbound=None, outbound=[], at=T)
    with pytest.raises(StaleStateError) as ei:
        s.commit_turn(phone=P, expected_version=None, state=st, inbound="x", outbound=["y"], at=T)
    assert P not in str(ei.value)  # the runner logs this message
    assert s.messages(P) == [] and s.pending_outbox() == [] and s.load(P)[1] == 1


def test_outbox_pending_until_delivered(s):
    [row] = s.log_only(phone=P, inbound=None, outbound=["a"], at=T)
    assert s.pending_outbox() == [row]
    s.mark_delivered(row.id, T)
    assert s.pending_outbox() == []


def test_mark_delivered_unknown_id_raises(s):
    with pytest.raises(KeyError):
        s.mark_delivered(999, T)


def test_mark_delivered_twice_keeps_first_timestamp(s):
    [row] = s.log_only(phone=P, inbound=None, outbound=["a"], at=T)
    s.mark_delivered(row.id, T)
    s.mark_delivered(row.id, T.replace(hour=11))
    delivered_at = s._conn.execute(
        "SELECT delivered_at FROM outbox WHERE id = ?", (row.id,)
    ).fetchone()[0]
    assert delivered_at == T.isoformat()


def test_log_only_does_not_touch_state(s):
    s.log_only(phone=P, inbound="x", outbound=["y"], at=T)
    assert s.load(P) is None
    assert [(m.direction, m.text) for m in s.messages(P)] == [("in", "x"), ("out", "y")]


def test_log_only_empty_writes_nothing(s):
    assert s.log_only(phone=P, inbound=None, outbound=[], at=T) == []
    assert s.messages(P) == []


def test_outbox_rows_in_order(s):
    rows = s.log_only(phone=P, inbound=None, outbound=["a", "b"], at=T)
    assert [r.text for r in rows] == ["a", "b"]
    assert s.pending_outbox() == rows


def test_naive_datetime_rejected(s):
    naive = datetime(2026, 11, 17, 9, 55)
    st = ConversationState(phone=P)
    with pytest.raises(ValueError):
        s.commit_turn(phone=P, expected_version=None, state=st, inbound=None, outbound=[], at=naive)
    with pytest.raises(ValueError):
        s.log_only(phone=P, inbound="x", outbound=[], at=naive)
    [row] = s.log_only(phone=P, inbound=None, outbound=["a"], at=T)
    with pytest.raises(ValueError):
        s.mark_delivered(row.id, naive)


def test_phone_mismatch_rejected_before_db(s):
    st = ConversationState(phone="+15550100002")
    with pytest.raises(ValueError):
        s.commit_turn(phone=P, expected_version=None, state=st, inbound="x", outbound=["y"], at=T)
    assert s.messages(P) == [] and s.load(P) is None


def test_non_ascii_survives(s):
    text = "José says 😀 — ok"
    st = ConversationState(phone=P, intake=IntakeState(results={"visit_reason": {"reason": text}}))
    s.commit_turn(phone=P, expected_version=None, state=st, inbound=text, outbound=[text], at=T)
    assert s.load(P)[0] == st
    assert {m.text for m in s.messages(P)} == {text}


def test_intake_started_property():
    from clinic_sms.state import GroupProgress

    assert not ConversationState(phone=P).intake_started
    done = ConversationState(phone=P, intake=IntakeState(done=True))
    assert done.intake_started
    prog = IntakeState(progress=GroupProgress(pending=["a"], results={}, visited=[]))
    assert ConversationState(phone=P, intake=prog).intake_started


def test_plan1_state_json_still_parses():
    st = ConversationState.model_validate_json(
        '{"phone": "+15550100001", "intake": {"progress": {"pending": ["insurance"],'
        ' "results": {}, "visited": ["insurance"]}}}'
    )
    assert st.active is None and st.active_agent == "intake" and st.intake_followups_sent == 0
    assert ConversationState(phone=P).active_agent is None
