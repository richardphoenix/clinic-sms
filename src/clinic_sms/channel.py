"""Where outbound SMS go: the simulator now, a Twilio adapter later (spec §5.2)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Outbound:
    to: str  # E.164
    text: str


class Channel(Protocol):
    async def send(self, msg: Outbound) -> None: ...


class RecordingChannel:
    """Keeps every sent message in `sent`, in order."""

    def __init__(self) -> None:
        self.sent: list[Outbound] = []

    async def send(self, msg: Outbound) -> None:
        self.sent.append(msg)
