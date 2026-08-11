from __future__ import annotations

import ipaddress
import struct
import tempfile
import unittest

from camera.analysis.pcapng import (
    UDPPacket,
    _Interface,
    _decode_udp,
    iter_udp_packets,
    summarize_udp_services,
)
from python_tools.pcap_udp_structure import address_matches_peer


def ipv6_udp_packet(source_port: int, destination_port: int, payload: bytes) -> bytes:
    source = ipaddress.IPv6Address("2001:db8::1").packed
    destination = ipaddress.IPv6Address("2001:db8::2").packed
    udp = struct.pack("!HHHH", source_port, destination_port, len(payload) + 8, 0) + payload
    ipv6 = struct.pack("!IHBB16s16s", 6 << 28, len(udp), 17, 64, source, destination)
    return struct.pack("<I", 30) + ipv6 + udp


def minimal_pcapng(packet: bytes) -> bytes:
    section = struct.pack(
        "<IIIHHqI", 0x0A0D0D0A, 28, 0x1A2B3C4D, 1, 0, -1, 28
    )
    interface = struct.pack("<IIHHII", 1, 20, 0, 0, 65535, 20)
    padding = b"\x00" * ((-len(packet)) % 4)
    block_length = 32 + len(packet) + len(padding)
    enhanced_packet = (
        struct.pack(
            "<IIIIIII", 6, block_length, 0, 0, 1_500_000, len(packet), len(packet)
        )
        + packet
        + padding
        + struct.pack("<I", block_length)
    )
    return section + interface + enhanced_packet


def apple_pcap_stub() -> bytes:
    return struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 2048, 149)


class PcapngTests(unittest.TestCase):
    def test_iter_udp_packets_reads_minimal_pcapng(self) -> None:
        packet = ipv6_udp_packet(50000, 20001, b"example")
        with tempfile.NamedTemporaryFile(suffix=".pcapng") as capture:
            capture.write(minimal_pcapng(packet))
            capture.flush()
            decoded = list(iter_udp_packets(capture.name))
        self.assertEqual(len(decoded), 1)
        self.assertEqual(decoded[0].timestamp, 1.5)
        self.assertEqual(decoded[0].captured_payload, b"example")

    def test_iter_udp_packets_accepts_apple_pcap_stub(self) -> None:
        packet = ipv6_udp_packet(50000, 20001, b"wrapped")
        wrapped = apple_pcap_stub() + minimal_pcapng(packet)
        with tempfile.NamedTemporaryFile(suffix=".pcap") as capture:
            capture.write(wrapped)
            capture.flush()
            decoded = list(iter_udp_packets(capture.name))
        self.assertEqual(len(decoded), 1)
        self.assertEqual(decoded[0].captured_payload, b"wrapped")

    def test_peer_filter_matches_nat64_address(self) -> None:
        peer = ipaddress.ip_address("149.56.108.231")
        self.assertTrue(address_matches_peer("64:ff9b::9538:6ce7", peer))
        self.assertFalse(address_matches_peer("64:ff9b::9538:6ce8", peer))

    def test_decode_null_header_ipv6_udp(self) -> None:
        decoded = _decode_udp(
            _Interface(0), 1.5, ipv6_udp_packet(50000, 20001, b"example")
        )
        self.assertIsNotNone(decoded)
        assert decoded is not None
        self.assertEqual(decoded.destination_port, 20001)
        self.assertEqual(decoded.payload_length, 7)
        self.assertEqual(decoded.captured_payload, b"example")

    def test_safe_service_summary(self) -> None:
        packets = iter(
            [
                UDPPacket(1.0, "192.0.2.1", 50000, "192.0.2.2", 10240, 3, b"abc"),
                UDPPacket(2.0, "192.0.2.2", 10240, "192.0.2.1", 50000, 3, b"abd"),
            ]
        )
        report = summarize_udp_services(packets, {10240})
        request = report["services"]["10240"]["to_service"]
        response = report["services"]["10240"]["from_service"]
        self.assertEqual(request["payload_size_counts"], {"3": 1})
        self.assertEqual(response["packets"], 1)
        self.assertNotIn("payload", request)
        self.assertFalse(request["plausible_rtp_stream"])
        self.assertEqual(request["size_classes"]["3"]["common_captured_prefix_bytes"], 3)

    def test_rtp_requires_more_than_version_bits(self) -> None:
        repeated = b"\x80\x60\x00\x01\x00\x00\x00\x01same"
        packets = iter(
            [
                UDPPacket(
                    float(index),
                    "192.0.2.1",
                    50000,
                    "192.0.2.2",
                    20001,
                    len(repeated),
                    repeated,
                )
                for index in range(3)
            ]
        )
        report = summarize_udp_services(packets, {20001})
        self.assertFalse(report["services"]["20001"]["to_service"]["plausible_rtp_stream"])


if __name__ == "__main__":
    unittest.main()
