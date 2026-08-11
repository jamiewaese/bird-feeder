"""Non-sensitive host-flow and time-bin summaries for decoded UDP captures."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, Iterable

from .pcapng import UDPPacket


def _direction_summary(packets: list[UDPPacket]) -> dict[str, Any]:
    sizes = Counter(packet.payload_length for packet in packets)
    if not packets:
        return {
            "packets": 0,
            "payload_bytes_on_wire": 0,
            "payload_size_counts": {},
            "first_seen": None,
            "last_seen": None,
        }
    return {
        "packets": len(packets),
        "payload_bytes_on_wire": sum(packet.payload_length for packet in packets),
        "payload_size_counts": {
            str(size): count for size, count in sorted(sizes.items())
        },
        "first_seen": datetime.fromtimestamp(
            min(packet.timestamp for packet in packets)
        ).astimezone().isoformat(),
        "last_seen": datetime.fromtimestamp(
            max(packet.timestamp for packet in packets)
        ).astimezone().isoformat(),
    }


def summarize_udp_host(
    packets: Iterable[UDPPacket], host: str, bin_seconds: int = 5
) -> dict[str, Any]:
    """Summarize all UDP traffic involving one host without exposing payloads."""
    if bin_seconds < 1 or bin_seconds > 3600:
        raise ValueError("bin_seconds must be in the range 1-3600")

    selected = [
        packet
        for packet in packets
        if packet.source == host or packet.destination == host
    ]
    from_host = [packet for packet in selected if packet.source == host]
    to_host = [packet for packet in selected if packet.destination == host]

    flows = Counter(
        (
            packet.source,
            packet.source_port,
            packet.destination,
            packet.destination_port,
        )
        for packet in selected
    )
    rendered_flows = [
        {
            "source": source,
            "source_port": source_port,
            "destination": destination,
            "destination_port": destination_port,
            "packets": count,
        }
        for (source, source_port, destination, destination_port), count in sorted(
            flows.items(), key=lambda item: (-item[1], item[0])
        )
    ]

    bins: dict[int, dict[str, int]] = {}
    for packet in selected:
        bucket = int(packet.timestamp // bin_seconds * bin_seconds)
        values = bins.setdefault(
            bucket,
            {
                "from_host_packets": 0,
                "from_host_payload_bytes": 0,
                "to_host_packets": 0,
                "to_host_payload_bytes": 0,
            },
        )
        if packet.source == host:
            values["from_host_packets"] += 1
            values["from_host_payload_bytes"] += packet.payload_length
        else:
            values["to_host_packets"] += 1
            values["to_host_payload_bytes"] += packet.payload_length

    rendered_bins = [
        {
            "start": datetime.fromtimestamp(timestamp).astimezone().isoformat(),
            **values,
        }
        for timestamp, values in sorted(bins.items())
    ]

    return {
        "host": host,
        "bin_seconds": bin_seconds,
        "matching_udp_packets": len(selected),
        "from_host": _direction_summary(from_host),
        "to_host": _direction_summary(to_host),
        "flows": rendered_flows,
        "time_bins": rendered_bins,
    }
