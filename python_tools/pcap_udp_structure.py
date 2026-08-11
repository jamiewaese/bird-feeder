"""Report non-sensitive UDP structure statistics from a pcapng capture."""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from pathlib import Path

from camera.analysis.pcapng import PcapFormatError, iter_udp_packets, summarize_udp_services


NAT64_WELL_KNOWN_PREFIX = ipaddress.ip_network("64:ff9b::/96")


def address_matches_peer(value: str, peer: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    address = ipaddress.ip_address(value)
    if address == peer:
        return True
    if (
        isinstance(peer, ipaddress.IPv4Address)
        and isinstance(address, ipaddress.IPv6Address)
        and address in NAT64_WELL_KNOWN_PREFIX
    ):
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF) == peer
    return False


def parse_service_ports(value: str) -> set[int]:
    try:
        ports = {int(item.strip()) for item in value.split(",") if item.strip()}
    except ValueError as exc:
        raise argparse.ArgumentTypeError("ports must be comma-separated integers") from exc
    if not ports or min(ports) < 1 or max(ports) > 65535:
        raise argparse.ArgumentTypeError("ports must be in the range 1-65535")
    return ports


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Summarize UDP framing without emitting raw application payloads."
    )
    parser.add_argument("capture", help="normalized pcapng file")
    parser.add_argument(
        "--ports",
        type=parse_service_ports,
        default={10240, 20001, 32762, 43818},
        help="comma-separated service ports (default: 10240,20001,32762,43818)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="write the JSON report to this path instead of standard output",
    )
    parser.add_argument(
        "--peer",
        type=ipaddress.ip_address,
        help="include only packets to or from this IP address",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        packets = iter_udp_packets(args.capture)
        if args.peer:
            packets = (
                packet
                for packet in packets
                if address_matches_peer(packet.source, args.peer)
                or address_matches_peer(packet.destination, args.peer)
            )
        report = summarize_udp_services(packets, args.ports)
        if args.peer:
            report["peer_filter"] = str(args.peer)
    except (OSError, PcapFormatError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
