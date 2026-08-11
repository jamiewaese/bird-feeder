from __future__ import annotations

import unittest

from camera.discovery.neighbors import normalize_mac, parse_arp, parse_ip_neigh
from camera.discovery.network import (
    parse_ifconfig,
    parse_ip_addr,
    parse_local_target,
    parse_sweep_network,
)
from python_tools.recon import compare_neighbor_lists


class NetworkParsingTests(unittest.TestCase):
    def test_parse_macos_ifconfig(self) -> None:
        output = """en0: flags=8863<UP,BROADCAST> mtu 1500
\tinet 192.168.50.12 netmask 0xffffff00 broadcast 192.168.50.255
lo0: flags=8049<UP,LOOPBACK> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
"""
        found = parse_ifconfig(output)
        self.assertEqual(found[0].name, "en0")
        self.assertEqual(found[0].network, "192.168.50.0/24")

    def test_parse_linux_ip_addr(self) -> None:
        output = "2: wlan0    inet 10.2.3.4/24 brd 10.2.3.255 scope global wlan0"
        found = parse_ip_addr(output)
        self.assertEqual(found[0].network, "10.2.3.0/24")

    def test_parse_arp(self) -> None:
        output = "? (192.168.1.27) at aa:bb:cc:dd:ee:ff on en0 ifscope [ethernet]"
        found = parse_arp(output)
        self.assertEqual(found[0].mac, "aa:bb:cc:dd:ee:ff")
        self.assertEqual(found[0].interface, "en0")

    def test_normalize_macos_short_mac_octets(self) -> None:
        self.assertEqual(normalize_mac("02:0:0:0:0:30"), "02:00:00:00:00:30")

    def test_parse_ip_neigh(self) -> None:
        output = "192.168.1.27 dev wlan0 lladdr aa:bb:cc:dd:ee:ff REACHABLE"
        found = parse_ip_neigh(output)
        self.assertEqual(found[0].state, "reachable")

    def test_scope_guards(self) -> None:
        self.assertEqual(str(parse_local_target("192.168.1.27")), "192.168.1.27")
        with self.assertRaises(ValueError):
            parse_local_target("example.com")
        with self.assertRaises(ValueError):
            parse_local_target("8.8.8.8")
        with self.assertRaises(ValueError):
            parse_sweep_network("10.0.0.0/8")

    def test_compare_neighbor_lists_ignores_order(self) -> None:
        a = {"address": "192.168.1.10", "mac": "aa:aa:aa:aa:aa:aa"}
        b = {"address": "192.168.1.20", "mac": "bb:bb:bb:bb:bb:bb"}
        c = {"address": "192.168.1.30", "mac": "cc:cc:cc:cc:cc:cc"}
        appeared, disappeared = compare_neighbor_lists([a, b], [b, c])
        self.assertEqual(appeared, [c])
        self.assertEqual(disappeared, [a])


if __name__ == "__main__":
    unittest.main()
