"""Local interface and target-scope validation helpers."""

from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
from collections.abc import Iterable

from .models import LocalInterface


def _run(command: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def parse_ip_addr(output: str) -> list[LocalInterface]:
    """Parse Linux ``ip -o -4 addr show`` output."""
    found: list[LocalInterface] = []
    pattern = re.compile(r"^\d+:\s+([^\s]+).*?\sinet\s+([0-9.]+/\d+)", re.MULTILINE)
    for interface, cidr in pattern.findall(output):
        value = ipaddress.IPv4Interface(cidr)
        found.append(
            LocalInterface(
                name=interface.split("@", 1)[0],
                address=str(value.ip),
                network=str(value.network),
                source="ip",
            )
        )
    return found


def _hex_mask_to_prefix(mask: str) -> int:
    value = int(mask, 16)
    bits = f"{value:032b}"
    if "01" in bits:
        raise ValueError(f"Non-contiguous IPv4 mask: {mask}")
    return bits.count("1")


def parse_ifconfig(output: str) -> list[LocalInterface]:
    """Parse the IPv4 subset of macOS/BSD ``ifconfig`` output."""
    found: list[LocalInterface] = []
    interface: str | None = None
    for line in output.splitlines():
        if line and not line[0].isspace() and ":" in line:
            interface = line.split(":", 1)[0]
            continue
        match = re.search(r"\binet\s+([0-9.]+)\s+netmask\s+(0x[0-9a-fA-F]+)", line)
        if interface and match:
            address, mask = match.groups()
            prefix = _hex_mask_to_prefix(mask)
            network = ipaddress.IPv4Interface(f"{address}/{prefix}").network
            found.append(
                LocalInterface(
                    name=interface,
                    address=address,
                    network=str(network),
                    source="ifconfig",
                )
            )
    return found


def _fallback_address() -> LocalInterface | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # UDP connect selects a route but sends no packet.
        sock.connect(("192.0.2.1", 9))
        address = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
    return LocalInterface("default", address, None, "route-selection")


def local_interfaces(include_loopback: bool = False) -> list[LocalInterface]:
    """Return local IPv4 interfaces without requiring third-party packages."""
    candidates: Iterable[LocalInterface] = ()
    output = _run(["ip", "-o", "-4", "addr", "show"])
    if output:
        candidates = parse_ip_addr(output)
    else:
        output = _run(["ifconfig"])
        if output:
            candidates = parse_ifconfig(output)

    result = [
        item
        for item in candidates
        if include_loopback or not ipaddress.ip_address(item.address).is_loopback
    ]
    if not result:
        fallback = _fallback_address()
        if fallback and (include_loopback or not ipaddress.ip_address(fallback.address).is_loopback):
            result.append(fallback)
    return result


def parse_local_target(value: str, *, allow_public: bool = False) -> ipaddress.IPv4Address:
    """Parse an IPv4 literal and reject unintended Internet targets by default."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError("Target must be an IPv4 address literal, not a hostname") from exc
    if not isinstance(address, ipaddress.IPv4Address):
        raise ValueError("Only IPv4 is supported in Phase 1")
    local_scope = address.is_private or address.is_link_local or address.is_loopback
    if not allow_public and not local_scope:
        raise ValueError("Refusing a public target; this tool is scoped to local devices")
    return address


def parse_sweep_network(value: str, *, max_hosts: int = 1024) -> ipaddress.IPv4Network:
    """Validate an explicit, bounded local IPv4 network."""
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError as exc:
        raise ValueError("Subnet must be an IPv4 network such as 192.168.1.0/24") from exc
    if not isinstance(network, ipaddress.IPv4Network):
        raise ValueError("Only IPv4 is supported in Phase 1")
    if not (network.is_private or network.is_link_local or network.is_loopback):
        raise ValueError("Refusing a non-local subnet")
    if network.num_addresses - 2 > max_hosts:
        raise ValueError(f"Subnet exceeds the safety cap of {max_hosts} usable addresses")
    return network
