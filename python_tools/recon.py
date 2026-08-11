"""Safe Phase 1 reconnaissance CLI for a camera on the local IPv4 network."""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from camera.discovery.neighbors import populate_neighbor_cache, read_neighbors
from camera.discovery.network import local_interfaces, parse_local_target, parse_sweep_network
from camera.discovery.ports import parse_ports, scan_tcp_ports
from camera.discovery.protocols import discover_onvif, discover_ssdp, fingerprint_open_ports


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _positive_float(value: str) -> float:
    number = float(value)
    if number <= 0 or number > 10:
        raise argparse.ArgumentTypeError("must be greater than 0 and at most 10 seconds")
    return number


def _write_or_print(report: dict[str, Any], output: str | None, human: bool) -> None:
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if output:
        destination = Path(output).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8")
        print(f"Wrote {destination}")
    elif human:
        _print_human(report)
    else:
        print(rendered, end="")


def _print_human(report: dict[str, Any]) -> None:
    command = report["command"]
    if command == "local":
        interfaces = report["interfaces"]
        if not interfaces:
            print("No non-loopback IPv4 interface was detected.")
        for item in interfaces:
            print(f"{item['name']:<12} {item['address']:<15} {item['network'] or 'network unknown'}")
    elif command in {"neighbors", "sweep"}:
        if command == "sweep":
            print(
                f"ARP trigger datagrams: {report['datagrams_sent']}/"
                f"{report['attempted_hosts']} submitted; {report['send_errors']} errors."
            )
        entries = report["neighbors"]
        if not entries:
            print("No matching neighbors with resolved MAC addresses were found.")
        for item in entries:
            print(
                f"{item['address']:<15} {item['mac'] or '-':<17} "
                f"{item['interface'] or '-':<10} {item['state'] or '-'}"
            )
    elif command == "discover":
        for name in ("onvif", "ssdp"):
            print(f"{name.upper()}: {len(report[name])} response(s)")
            for item in report[name]:
                print(f"  {json.dumps(item, sort_keys=True)}")
    elif command == "scan":
        print(f"Target: {report['target']}")
        neighbor = report.get("neighbor")
        if neighbor:
            print(f"MAC: {neighbor.get('mac') or 'unresolved'}")
        if not report["open_tcp_ports"]:
            print("No scanned TCP ports accepted a connection.")
        for item in report["open_tcp_ports"]:
            hint = f" ({item['service_hint']})" if item.get("service_hint") else ""
            print(f"TCP {item['port']}: open{hint}")
        for item in report["protocol_probes"]:
            print(f"{item['protocol']} on {item['port']}: {item['outcome']} — {item['detail']}")
    elif command == "compare-neighbors":
        for label in ("appeared", "disappeared"):
            entries = report[label]
            print(f"{label.capitalize()}: {len(entries)}")
            for item in entries:
                print(f"  {item.get('address', '-'):<15} {item.get('mac') or '-'}")


def _base_report(command: str) -> dict[str, Any]:
    return {"schema_version": 1, "captured_at": _timestamp(), "command": command}


def command_local(args: argparse.Namespace) -> None:
    report = _base_report("local")
    report["interfaces"] = [item.to_dict() for item in local_interfaces()]
    _write_or_print(report, args.output, args.human)


def command_neighbors(args: argparse.Namespace) -> None:
    report = _base_report("neighbors")
    report["neighbors"] = [item.to_dict() for item in read_neighbors()]
    _write_or_print(report, args.output, args.human)


def compare_neighbor_lists(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return neighbor records that appeared and disappeared, ignoring order."""
    def key(item: dict[str, Any]) -> tuple[str | None, str | None]:
        return item.get("address"), item.get("mac")

    def order(item: tuple[str | None, str | None]) -> tuple[str, str]:
        return item[0] or "", item[1] or ""

    before_by_key = {key(item): item for item in before}
    after_by_key = {key(item): item for item in after}
    appeared_keys = sorted(after_by_key.keys() - before_by_key.keys(), key=order)
    disappeared_keys = sorted(before_by_key.keys() - after_by_key.keys(), key=order)
    appeared = [after_by_key[item] for item in appeared_keys]
    disappeared = [before_by_key[item] for item in disappeared_keys]
    return appeared, disappeared


def command_compare_neighbors(args: argparse.Namespace) -> None:
    try:
        before = json.loads(Path(args.before).read_text(encoding="utf-8"))
        after = json.loads(Path(args.after).read_text(encoding="utf-8"))
        before_neighbors = before["neighbors"]
        after_neighbors = after["neighbors"]
        if not isinstance(before_neighbors, list) or not isinstance(after_neighbors, list):
            raise ValueError("neighbors must be JSON arrays")
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"error: could not compare reports: {exc}") from exc
    appeared, disappeared = compare_neighbor_lists(before_neighbors, after_neighbors)
    report = _base_report("compare-neighbors")
    report.update(
        {
            "before": str(Path(args.before)),
            "after": str(Path(args.after)),
            "appeared": appeared,
            "disappeared": disappeared,
        }
    )
    _write_or_print(report, args.output, args.human)


def command_sweep(args: argparse.Namespace) -> None:
    try:
        network = parse_sweep_network(args.subnet)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc
    attempted, sent, errors = populate_neighbor_cache(network, wait=args.wait)
    entries = [
        item
        for item in read_neighbors()
        if ipaddress.ip_address(item.address) in network and item.mac is not None
    ]
    report = _base_report("sweep")
    report.update(
        {
            "subnet": str(network),
            "attempted_hosts": attempted,
            "datagrams_sent": sent,
            "send_errors": errors,
            "method": "one UDP byte to discard port 9, followed by OS neighbor-cache read",
            "neighbors": [item.to_dict() for item in entries],
        }
    )
    _write_or_print(report, args.output, args.human)


def command_discover(args: argparse.Namespace) -> None:
    report = _base_report("discover")
    report["requests"] = [
        "ONVIF NetworkVideoTransmitter WS-Discovery Probe to 239.255.255.250:3702/udp",
        "SSDP M-SEARCH ssdp:all to 239.255.255.250:1900/udp",
    ]
    report["onvif"] = discover_onvif(timeout=args.timeout)
    report["ssdp"] = discover_ssdp(timeout=args.timeout)
    _write_or_print(report, args.output, args.human)


def command_scan(args: argparse.Namespace) -> None:
    try:
        target = parse_local_target(args.target)
        ports = list(range(1, 65536)) if args.all_ports else parse_ports(args.ports)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc

    open_ports = scan_tcp_ports(
        str(target), ports, timeout=args.connect_timeout, workers=args.workers
    )
    probes = [] if args.no_fingerprint else fingerprint_open_ports(
        str(target), [item.port for item in open_ports], timeout=args.probe_timeout
    )
    matching_neighbors = [item for item in read_neighbors() if item.address == str(target)]
    report = _base_report("scan")
    report.update(
        {
            "target": str(target),
            "neighbor": matching_neighbors[0].to_dict() if matching_neighbors else None,
            "scan": {
                "transport": "tcp",
                "ports_tested": len(ports),
                "port_specification": "1-65535" if args.all_ports else args.ports,
                "connect_timeout_seconds": args.connect_timeout,
                "workers": args.workers,
            },
            "open_tcp_ports": [item.to_dict() for item in open_ports],
            "protocol_probes": [item.to_dict() for item in probes],
            "limitations": [
                "A silent TCP port may be closed or filtered.",
                "UDP services are not inferred from silence.",
                "Unknown open ports are not sent application data automatically.",
            ],
        }
    )
    _write_or_print(report, args.output, args.human)


def _add_output_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--human", action="store_true", help="print a compact table instead of JSON")
    parser.add_argument("--output", metavar="PATH", help="write the complete JSON report to PATH")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bird-feeder-recon",
        description="Dependency-free, local-only reconnaissance for an owned bird feeder camera.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    local = subparsers.add_parser("local", help="show local IPv4 interfaces and likely subnets")
    _add_output_options(local)
    local.set_defaults(handler=command_local)

    neighbors = subparsers.add_parser("neighbors", help="read the current IP-to-MAC neighbor cache")
    _add_output_options(neighbors)
    neighbors.set_defaults(handler=command_neighbors)

    compare = subparsers.add_parser(
        "compare-neighbors", help="compare two JSON neighbor/sweep reports"
    )
    compare.add_argument("before", help="baseline JSON report")
    compare.add_argument("after", help="later JSON report")
    _add_output_options(compare)
    compare.set_defaults(handler=command_compare_neighbors)

    sweep = subparsers.add_parser(
        "sweep", help="populate and read the neighbor cache for an explicit local subnet"
    )
    sweep.add_argument("--subnet", required=True, help="local IPv4 CIDR, for example 192.168.1.0/24")
    sweep.add_argument("--wait", type=_positive_float, default=1.0, help="seconds to wait for ARP (default: 1)")
    _add_output_options(sweep)
    sweep.set_defaults(handler=command_sweep)

    discover = subparsers.add_parser("discover", help="send standard ONVIF and SSDP discovery requests")
    discover.add_argument("--timeout", type=_positive_float, default=2.0, help="response window per protocol")
    _add_output_options(discover)
    discover.set_defaults(handler=command_discover)

    scan = subparsers.add_parser("scan", help="scan one explicit local IPv4 target")
    scan.add_argument("target", help="camera IPv4 address; hostnames and public addresses are rejected")
    selection = scan.add_mutually_exclusive_group()
    selection.add_argument(
        "--ports",
        default="common",
        help="comma-separated ports/ranges, or 'common' (default)",
    )
    selection.add_argument(
        "--all-ports", action="store_true", help="explicitly test all 65,535 TCP ports"
    )
    scan.add_argument("--connect-timeout", type=_positive_float, default=0.35)
    scan.add_argument("--probe-timeout", type=_positive_float, default=1.5)
    scan.add_argument("--workers", type=int, choices=range(1, 129), default=64, metavar="1-128")
    scan.add_argument(
        "--no-fingerprint",
        action="store_true",
        help="connect-scan only; send no HTTP, RTSP, WebSocket, or MQTT handshakes",
    )
    _add_output_options(scan)
    scan.set_defaults(handler=command_scan)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.handler(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
