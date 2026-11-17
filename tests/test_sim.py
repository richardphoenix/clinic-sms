from datetime import timedelta

import pytest

from clinic_sms.sim import SIM_START, is_comment, load_clock, parse_duration, save_clock
from clinic_sms.store import Store


@pytest.mark.parametrize("text,expected", [("30m", timedelta(minutes=30)), ("14h", timedelta(hours=14)),
                                           ("2d", timedelta(days=2)), ("1h30m", timedelta(minutes=90))])
def test_parse_duration(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "0m", "-5m", "5", "5x", "h", "1h-30m"])
def test_parse_duration_rejects(text):
    with pytest.raises(ValueError):
        parse_duration(text)


def test_is_comment():
    assert is_comment("# step 1") and is_comment("#")
    assert not is_comment("#1 That was your copay.") and not is_comment("hello")


def test_clock_starts_at_sim_start_and_persists(tmp_path):
    with Store(tmp_path / "db") as s:
        c = load_clock(s)
        assert c.now() == SIM_START
        c.advance(timedelta(hours=1))
        save_clock(s, c)
    with Store(tmp_path / "db") as s:
        assert load_clock(s).now() == SIM_START + timedelta(hours=1)
