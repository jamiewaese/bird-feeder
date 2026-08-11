"""Bounded TCP connection scanning for an explicitly selected camera."""

from __future__ import annotations

import socket
from concurrent.futures import ThreadPoolExecutor

from .models import PortResult


COMMON_CAMERA_PORTS = (
    53,
    80,
    443,
    554,
    1883,
    3702,
    5000,
    7447,
    8000,
    8001,
    8080,
    8081,
    8443,
    8554,
    8883,
    8888,
    9000,
    10000,
    34567,
    37777,
    49152,
)

SERVICE_HINTS = {
    53: "DNS",
    80: "HTTP / ONVIF",
    443: "HTTPS / secure WebSocket",
    554: "RTSP",
    1883: "MQTT",
    3702: "WS-Discovery (normally UDP)",
    5000: "HTTP / vendor service",
    7447: "RTSP alternative",
    8000: "HTTP / vendor service",
    8001: "HTTP / vendor service",
    8080: "HTTP alternative",
    8081: "HTTP alternative",
    8443: "HTTPS alternative",
    8554: "RTSP alternative",
    8883: "MQTT over TLS",
    8888: "HTTP alternative",
    9000: "vendor service",
    10000: "vendor service",
    34567: "common camera vendor service",
    37777: "common camera vendor service",
    49152: "dynamic/vendor service",
}


def parse_ports(specification: str) -> list[int]:
    """Parse comma-separated ports and inclusive ranges."""
    if specification.strip().lower() == "common":
        return list(COMMON_CAMERA_PORTS)
    ports: set[int] = set()
    for item in specification.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            if "-" in item:
                start_text, end_text = item.split("-", 1)
                start, end = int(start_text), int(end_text)
                if start > end:
                    raise ValueError
                ports.update(range(start, end + 1))
            else:
                ports.add(int(item))
        except ValueError as exc:
            raise ValueError(f"Invalid port or range: {item!r}") from exc
    if not ports or min(ports) < 1 or max(ports) > 65535:
        raise ValueError("Ports must be in the range 1-65535")
    return sorted(ports)


def _scan_one(target: str, port: int, timeout: float) -> PortResult | None:
    try:
        with socket.create_connection((target, port), timeout=timeout):
            return PortResult(port, "tcp", "open", SERVICE_HINTS.get(port))
    except OSError:
        return None


def scan_tcp_ports(
    target: str,
    ports: list[int],
    *,
    timeout: float = 0.35,
    workers: int = 64,
) -> list[PortResult]:
    """Return open TCP ports; closed and filtered ports are intentionally omitted."""
    bounded_workers = max(1, min(workers, 128, len(ports)))
    with ThreadPoolExecutor(max_workers=bounded_workers) as pool:
        results = pool.map(lambda port: _scan_one(target, port, timeout), ports)
    return sorted((item for item in results if item is not None), key=lambda item: item.port)
