"""Summarize UDP flows and byte-rate bins for one host in an Apple capture."""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from pathlib import Path

from camera.analysis.pcapng import PcapFormatError, iter_udp_packets
from camera.analysis.timeline import summarize_udp_host


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Report host UDP flows and time bins without raw payloads."
    )
    parser.add_argument("capture", help="pcapng or Apple pcap-header-wrapped pcapng")
    parser.add_argument("--host", required=True, type=ipaddress.ip_address)
    parser.add_argument("--bin-seconds", type=int, default=5)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = summarize_udp_host(
            iter_udp_packets(args.capture), str(args.host), args.bin_seconds
        )
    except (OSError, PcapFormatError, ValueError) as exc:
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
