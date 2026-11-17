"""Demo data for a fresh database."""

from __future__ import annotations

from datetime import date, datetime, time

from clinic_sms.clinic_db import apply_effect, to_utc_iso
from clinic_sms.clock import CLINIC_TZ
from clinic_sms.jobs import reminder_effects
from clinic_sms.store import Store

JANE_PHONE = "+15550100001"
SEED_APPOINTMENT_AT = datetime(2026, 11, 19, 10, 0, tzinfo=CLINIC_TZ)
OPEN_SLOT_DAYS = (date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25), date(2026, 11, 27))
OPEN_SLOT_HOURS = range(9, 17)  # 9:00 AM ... 4:00 PM, on the hour
_PROVIDERS = ("Dr. Lee", "Dr. Patel")


def seed_demo(store: Store, *, now: datetime) -> bool:
    """Seed Jane, two providers, slots and her reminder; False (no writes) if already seeded."""
    conn = store._conn  # one transaction across the clinic tables
    if conn.execute("SELECT 1 FROM patients LIMIT 1").fetchone() is not None:
        return False
    with conn:
        patient_id = conn.execute(
            "INSERT INTO patients (phone, first_name, last_name) VALUES (?, 'Jane', 'Doe')",
            (JANE_PHONE,),
        ).lastrowid
        provider_ids = [
            conn.execute("INSERT INTO providers (name) VALUES (?)", (name,)).lastrowid
            for name in _PROVIDERS
        ]

        def add_slot(provider_id: int, start: datetime) -> int:
            return conn.execute(
                "INSERT INTO slots (provider_id, start_at) VALUES (?, ?)",
                (provider_id, to_utc_iso(start)),
            ).lastrowid

        jane_slot = add_slot(provider_ids[0], SEED_APPOINTMENT_AT)
        appointment_id = conn.execute(
            "INSERT INTO appointments (patient_id, slot_id, status) VALUES (?, ?, 'booked')",
            (patient_id, jane_slot),
        ).lastrowid
        for day in OPEN_SLOT_DAYS:
            for hour in OPEN_SLOT_HOURS:
                for provider_id in provider_ids:
                    add_slot(provider_id, datetime.combine(day, time(hour), tzinfo=CLINIC_TZ))
        for effect in reminder_effects(
            phone=JANE_PHONE,
            appointment_id=appointment_id,
            start_at=SEED_APPOINTMENT_AT,
            now=now,
        ):
            apply_effect(conn, effect, at=now)
    return True
