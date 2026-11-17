import math
import re
from datetime import datetime

from clinic_sms.clock import CLINIC_TZ

# GSM 03.38 basic character set (each char is 1 septet).
_GSM_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
# GSM 03.38 extension table (each char is 2 septets: escape + char).
_GSM_EXT = frozenset("^{}\\[~]|€\f")


def normalize_phone(raw: str) -> str:
    """Normalize a US phone number to E.164; raise ValueError if unrecognized."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) != 11 or not digits.startswith("1"):
        raise ValueError(f"unrecognized phone number: {raw!r}")
    return "+" + digits


def format_phone(e164: str) -> str:
    m = re.fullmatch(r"\+1(\d{3})(\d{3})(\d{4})", e164)
    if not m:
        raise ValueError(f"not a US E.164 number: {e164!r}")
    return f"({m.group(1)}) {m.group(2)}-{m.group(3)}"


def mask_phone(e164: str) -> str:
    """Phone number for logs: '+15550100009' -> '+1555***0009'.

    Keeps the country code, the area code and the last 4 digits. Anything that is not a
    US E.164 number is masked completely ('***'), so a raw value never reaches a log.
    """
    m = re.fullmatch(r"\+1(\d{3})\d{3}(\d{4})", e164)
    if not m:
        return "***"
    return f"+1{m.group(1)}***{m.group(2)}"


def format_when(dt: datetime) -> str:
    """Format as e.g. 'Thu 11/19, 10:00 AM' in clinic local time (hour not zero-padded)."""
    local = dt.astimezone(CLINIC_TZ)
    hour = local.hour % 12 or 12
    ampm = "AM" if local.hour < 12 else "PM"
    return f"{local:%a} {local.month}/{local.day}, {hour}:{local:%M} {ampm}"


def sms_segments(text: str) -> int:
    """Count SMS segments: GSM-7 (160/153) if possible, otherwise UCS-2 (70/67)."""
    if all(c in _GSM_BASIC or c in _GSM_EXT for c in text):
        septets = len(text) + sum(1 for c in text if c in _GSM_EXT)
        return 1 if septets <= 160 else math.ceil(septets / 153)
    units = len(text.encode("utf-16-le")) // 2
    return 1 if units <= 70 else math.ceil(units / 67)
