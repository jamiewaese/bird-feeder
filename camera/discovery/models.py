"""Data models shared by discovery commands."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class LocalInterface:
    name: str
    address: str
    network: str | None
    source: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

@dataclass(frozen=True)
class Neighbor:
    address: str
    mac: str | None
    interface: str | None
    state: str | None
    source: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PortResult:
    port: int
    transport: str
    state: str
    service_hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProbeResult:
    protocol: str
    port: int
    outcome: str
    detail: str
    request_summary: str
    response_excerpt: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
