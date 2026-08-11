"""Read and gently populate the operating system's IPv4 neighbor cache."""

from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
import time

from .models import Neighbor


def normalize_mac(value: str) -> str:
    """Normalize colon-delimited MAC octets emitted without leading zeroes."""
    parts = value.lower().split(":")
    if len(parts) != 6:
        return value.lower()
    try:
        octets = [int(part, 16) for part in parts]
    except ValueError:
        return value.lower()
    if any(octet > 255 for octet in octets):
        return value.lower()
    return ":".join(f"{octet:02x}" for octet in octets)


def parse_arp(output: str) -> list[Neighbor]:
    """Parse macOS/BSD and common Linux ``arp -an`` output."""
    entries: list[Neighbor] = []
    pattern = re.compile(
        r"\((?P<ip>[0-9.]+)\)\s+at\s+(?P<mac>[^\s]+)(?:\s+on\s+(?P<interface>[^\s]+))?"
    )
    for match in pattern.finditer(output):
        mac_value = match.group("mac").lower()
        mac = (
            None
            if mac_value in {"(incomplete)", "<incomplete>", "incomplete"}
            else normalize_mac(mac_value)
        )
        entries.append(
            Neighbor(
                address=match.group("ip"),
                mac=mac,
                interface=match.group("interface"),
                state="incomplete" if mac is None else "cached",
                source="arp",
            )
        )
    return entries


def parse_ip_neigh(output: str) -> list[Neighbor]:
    """Parse Linux ``ip neigh show`` output."""
    entries: list[Neighbor] = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            ipaddress.IPv4Address(parts[0])
        except ValueError:
            continue
        interface = parts[parts.index("dev") + 1] if "dev" in parts else None
        mac = normalize_mac(parts[parts.index("lladdr") + 1]) if "lladdr" in parts else None
        state = parts[-1].lower()
        entries.append(Neighbor(parts[0], mac, interface, state, "ip-neigh"))
    return entries


def read_neighbors() -> list[Neighbor]:
    """Read known IPv4-to-MAC mappings using available platform commands."""
    commands = [(["ip", "neigh", "show"], parse_ip_neigh), (["arp", "-an"], parse_arp)]
    for command, parser in commands:
        try:
            completed = subprocess.run(
                command, check=False, capture_output=True, text=True, timeout=4
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            continue
        if completed.returncode == 0:
            return parser(completed.stdout)
    return []


def populate_neighbor_cache(
    network: ipaddress.IPv4Network, *, wait: float = 1.0
) -> tuple[int, int, int]:
    """Cause normal ARP resolution by sending one harmless UDP byte per local host.

    This does not claim that UDP port 9 is open. Its sole purpose is to let the OS
    learn MAC addresses for directly reachable peers.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    attempted = 0
    sent = 0
    errors = 0
    try:
        for address in network.hosts():
            attempted += 1
            try:
                sock.sendto(b"\x00", (str(address), 9))
            except (BlockingIOError, OSError):
                errors += 1
                continue
            sent += 1
    finally:
        sock.close()
    if wait > 0:
        time.sleep(min(wait, 5.0))
    return attempted, sent, errors
