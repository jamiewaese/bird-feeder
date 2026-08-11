"""Minimal, dependency-free pcapng and UDP metadata parser.

This module intentionally exposes only packet metadata and short payload-derived
statistics. It does not print raw application payloads, tokens, or credentials.

Apple ``tcpdump`` can prepend a 24-byte classic-pcap global header before a
pcapng stream when capturing PKTAP interfaces. That specific wrapper is
recognized, but ordinary classic-pcap packet records are not parsed here.
"""

from __future__ import annotations

import hashlib
import ipaddress
import math
import statistics
import struct
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


class PcapFormatError(ValueError):
    """Raised when a capture is malformed or uses an unsupported byte order."""


@dataclass(frozen=True)
class _Interface:
    link_type: int
    timestamp_base: int = 10
    timestamp_exponent: int = 6

    def timestamp_seconds(self, ticks: int) -> float:
        return ticks / (self.timestamp_base**self.timestamp_exponent)


@dataclass(frozen=True)
class UDPPacket:
    timestamp: float
    source: str
    source_port: int
    destination: str
    destination_port: int
    payload_length: int
    captured_payload: bytes


def _parse_interface(block: bytes) -> _Interface:
    if len(block) < 20:
        raise PcapFormatError("Truncated pcapng interface description")
    link_type = struct.unpack_from("<H", block, 8)[0]
    base, exponent = 10, 6
    offset = 16
    while offset + 4 <= len(block) - 4:
        code, length = struct.unpack_from("<HH", block, offset)
        offset += 4
        if code == 0:
            break
        value = block[offset : offset + length]
        if code == 9 and value:
            resolution = value[0]
            if resolution & 0x80:
                base, exponent = 2, resolution & 0x7F
            else:
                base, exponent = 10, resolution
        offset += (length + 3) & ~3
    return _Interface(link_type, base, exponent)


def _iter_packet_records(path: Path) -> Iterator[tuple[_Interface, float, bytes]]:
    data = path.read_bytes()
    classic_pcap_magics = {
        b"\xd4\xc3\xb2\xa1",
        b"\xa1\xb2\xc3\xd4",
        b"\x4d\x3c\xb2\xa1",
        b"\xa1\xb2\x3c\x4d",
    }
    if (
        len(data) >= 36
        and data[:4] in classic_pcap_magics
        and data[24:28] == b"\x0a\x0d\x0d\x0a"
    ):
        data = data[24:]
    if len(data) < 12 or data[:4] != b"\x0a\x0d\x0d\x0a":
        raise PcapFormatError(
            "Expected pcapng, optionally preceded by Apple's 24-byte pcap header"
        )
    if data[8:12] != b"\x4d\x3c\x2b\x1a":
        raise PcapFormatError("Only little-endian pcapng captures are supported")

    interfaces: list[_Interface] = []
    offset = 0
    while offset + 12 <= len(data):
        block_type, block_length = struct.unpack_from("<II", data, offset)
        if block_length < 12 or offset + block_length > len(data):
            raise PcapFormatError(f"Invalid pcapng block length at byte {offset}")
        trailing_length = struct.unpack_from("<I", data, offset + block_length - 4)[0]
        if trailing_length != block_length:
            raise PcapFormatError(f"Mismatched pcapng block length at byte {offset}")
        block = data[offset : offset + block_length]

        if block_type == 1:
            interfaces.append(_parse_interface(block))
        elif block_type == 6:
            if len(block) < 32:
                raise PcapFormatError("Truncated enhanced packet block")
            interface_id, timestamp_high, timestamp_low, captured_length = struct.unpack_from(
                "<IIII", block, 8
            )
            if interface_id >= len(interfaces):
                raise PcapFormatError(f"Unknown interface ID {interface_id}")
            packet_start = 28
            packet_end = packet_start + captured_length
            if packet_end > len(block) - 4:
                raise PcapFormatError("Captured packet extends beyond its pcapng block")
            interface = interfaces[interface_id]
            ticks = (timestamp_high << 32) | timestamp_low
            yield interface, interface.timestamp_seconds(ticks), block[packet_start:packet_end]
        offset += block_length

    if offset != len(data):
        raise PcapFormatError("Trailing bytes after final pcapng block")


def _ip_offset(link_type: int, packet: bytes) -> int | None:
    if link_type in {0, 108} and len(packet) >= 5:
        return 4
    if link_type == 1 and len(packet) >= 14:
        ether_type = int.from_bytes(packet[12:14], "big")
        if ether_type == 0x8100 and len(packet) >= 18:
            return 18
        return 14
    if link_type == 101:
        return 0
    if link_type == 113 and len(packet) >= 16:
        return 16
    # Apple captures can describe a null-header stream with a private link type.
    # Restrict fallback inspection to conventional offsets and require an IP
    # version nibble so arbitrary payload bytes are not treated as packets.
    for offset in (0, 4, 14, 16):
        if len(packet) > offset and packet[offset] >> 4 in {4, 6}:
            return offset
    return None


def _decode_udp(interface: _Interface, timestamp: float, packet: bytes) -> UDPPacket | None:
    offset = _ip_offset(interface.link_type, packet)
    if offset is None or len(packet) <= offset:
        return None
    version = packet[offset] >> 4

    if version == 4:
        if len(packet) < offset + 20:
            return None
        header_length = (packet[offset] & 0x0F) * 4
        if header_length < 20 or len(packet) < offset + header_length + 8:
            return None
        if packet[offset + 9] != 17:
            return None
        source = str(ipaddress.IPv4Address(packet[offset + 12 : offset + 16]))
        destination = str(ipaddress.IPv4Address(packet[offset + 16 : offset + 20]))
        udp_offset = offset + header_length
    elif version == 6:
        if len(packet) < offset + 48 or packet[offset + 6] != 17:
            return None
        source = str(ipaddress.IPv6Address(packet[offset + 8 : offset + 24]))
        destination = str(ipaddress.IPv6Address(packet[offset + 24 : offset + 40]))
        udp_offset = offset + 40
    else:
        return None

    source_port, destination_port, udp_length = struct.unpack_from("!HHH", packet, udp_offset)
    if udp_length < 8:
        return None
    payload_start = udp_offset + 8
    captured_payload = packet[payload_start : min(len(packet), payload_start + udp_length - 8)]
    return UDPPacket(
        timestamp,
        source,
        source_port,
        destination,
        destination_port,
        udp_length - 8,
        captured_payload,
    )


def iter_udp_packets(path: str | Path) -> Iterator[UDPPacket]:
    """Yield decoded IPv4/IPv6 UDP metadata from a pcapng capture."""
    capture = Path(path)
    for interface, timestamp, packet in _iter_packet_records(capture):
        decoded = _decode_udp(interface, timestamp, packet)
        if decoded is not None:
            yield decoded


def _entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = Counter(data)
    total = len(data)
    return -sum((count / total) * math.log2(count / total) for count in counts.values())


def _payload_structure(packets: list[UDPPacket]) -> dict[str, Any]:
    payloads = [packet.captured_payload for packet in packets if packet.captured_payload]
    combined = b"".join(payloads)
    fingerprints = {hashlib.sha256(payload).hexdigest() for payload in payloads}
    common_prefix = 0
    if payloads:
        shortest = min(len(payload) for payload in payloads)
        for index in range(shortest):
            if len({payload[index] for payload in payloads}) != 1:
                break
            common_prefix += 1
    printable = sum(byte in b"\t\n\r" or 32 <= byte <= 126 for byte in combined)
    annex_b = sum(
        payload.startswith(b"\x00\x00\x01") or payload.startswith(b"\x00\x00\x00\x01")
        for payload in payloads
    )
    # A two-bit RTP version match alone has a 1-in-4 false-positive rate. A
    # plausible stream also needs a stable SSRC and changing sequence numbers.
    rtp_candidates = [payload for payload in payloads if len(payload) >= 12 and payload[0] >> 6 == 2]
    plausible_rtp = False
    if len(rtp_candidates) >= 3 and len(rtp_candidates) == len(payloads):
        sequence_numbers = [int.from_bytes(payload[2:4], "big") for payload in rtp_candidates]
        ssrcs = {payload[8:12] for payload in rtp_candidates}
        payload_types = {payload[1] & 0x7F for payload in rtp_candidates}
        plausible_rtp = (
            len(ssrcs) == 1
            and len(payload_types) <= 2
            and len(set(sequence_numbers)) >= min(3, len(sequence_numbers))
        )
    ordered_times = sorted(packet.timestamp for packet in packets)
    intervals = [
        later - earlier for earlier, later in zip(ordered_times, ordered_times[1:])
    ]
    return {
        "captured_payload_bytes": len(combined),
        "unique_captured_payload_hashes": len(fingerprints),
        "common_captured_prefix_bytes": common_prefix,
        "aggregate_shannon_entropy_bits_per_captured_byte": round(_entropy(combined), 3),
        "printable_captured_byte_ratio": round(printable / len(combined), 4) if combined else 0.0,
        "median_interarrival_seconds": round(statistics.median(intervals), 6) if intervals else None,
        "plausible_rtp_stream": plausible_rtp,
        "annex_b_start_code_ratio": round(annex_b / len(payloads), 4) if payloads else 0.0,
    }


def _group_summary(packets: list[UDPPacket]) -> dict[str, Any]:
    sizes = Counter(packet.payload_length for packet in packets)
    by_size: dict[str, Any] = {}
    for size in sorted(sizes):
        same_size = [packet for packet in packets if packet.payload_length == size]
        by_size[str(size)] = {"packets": len(same_size), **_payload_structure(same_size)}
    return {
        "packets": len(packets),
        "payload_bytes_on_wire": sum(packet.payload_length for packet in packets),
        **_payload_structure(packets),
        "payload_size_counts": {str(size): count for size, count in sorted(sizes.items())},
        "size_classes": by_size,
        "first_seen": datetime.fromtimestamp(min(packet.timestamp for packet in packets)).astimezone().isoformat(),
        "last_seen": datetime.fromtimestamp(max(packet.timestamp for packet in packets)).astimezone().isoformat(),
    }


def summarize_udp_services(
    packets: Iterator[UDPPacket], service_ports: set[int]
) -> dict[str, Any]:
    """Summarize traffic to and from selected service ports without raw payloads."""
    grouped: dict[tuple[int, str], list[UDPPacket]] = {}
    for packet in packets:
        if packet.destination_port in service_ports:
            key = (packet.destination_port, "to_service")
        elif packet.source_port in service_ports:
            key = (packet.source_port, "from_service")
        else:
            continue
        grouped.setdefault(key, []).append(packet)

    services: dict[str, dict[str, Any]] = {}
    for (port, direction), values in sorted(grouped.items()):
        services.setdefault(str(port), {})[direction] = _group_summary(values)
    return {"service_ports": sorted(service_ports), "services": services}
