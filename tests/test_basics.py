from datetime import datetime, timedelta, timezone

import pytest

from clinic_sms.clock import CLINIC_TZ, SimClock
from clinic_sms.fmt import format_phone, format_when, mask_phone, normalize_phone, sms_segments


def test_normalize_phone_accepts_common_us_formats():
    for raw in ["(555) 010-0001", "555-010-0001", "5550100001", "+15550100001", "1 555 010 0001"]:
        assert normalize_phone(raw) == "+15550100001"

def test_normalize_phone_rejects_garbage():
    with pytest.raises(ValueError):
        normalize_phone("12345")

def test_format_phone():
    assert format_phone("+15550100001") == "(555) 010-0001"

def test_mask_phone_keeps_country_area_and_last_four():
    assert mask_phone("+15550100009") == "+1555***0009"

def test_mask_phone_masks_anything_else_completely():
    for raw in ["5550100009", "(555) 010-0009", "+445550100009", "+1555010000", "hello", ""]:
        assert mask_phone(raw) == "***"

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
