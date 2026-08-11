from __future__ import annotations

import unittest

from camera.analysis.pcapng import UDPPacket
from camera.analysis.timeline import summarize_udp_host


class TimelineTests(unittest.TestCase):
    def test_summarize_udp_host(self) -> None:
        packets = [
            UDPPacket(10.1, "192.0.2.10", 4000, "192.0.2.20", 5000, 3, b"abc"),
            UDPPacket(11.1, "192.0.2.20", 5000, "192.0.2.10", 4000, 4, b"defg"),
            UDPPacket(16.1, "192.0.2.30", 6000, "192.0.2.40", 7000, 5, b"other"),
        ]
        report = summarize_udp_host(packets, "192.0.2.20", bin_seconds=5)
        self.assertEqual(report["matching_udp_packets"], 2)
        self.assertEqual(report["from_host"]["payload_bytes_on_wire"], 4)
        self.assertEqual(report["to_host"]["payload_size_counts"], {"3": 1})
        self.assertEqual(len(report["flows"]), 2)
        self.assertEqual(len(report["time_bins"]), 1)
        self.assertNotIn("captured_payload", report)

    def test_rejects_invalid_bin_size(self) -> None:
        with self.assertRaises(ValueError):
            summarize_udp_host([], "192.0.2.20", bin_seconds=0)


if __name__ == "__main__":
    unittest.main()
