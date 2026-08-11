from __future__ import annotations

import socket
import unittest

from camera.discovery.ports import parse_ports, scan_tcp_ports


class PortTests(unittest.TestCase):
    def test_parse_ports(self) -> None:
        self.assertEqual(parse_ports("80,443,8000-8002"), [80, 443, 8000, 8001, 8002])
        with self.assertRaises(ValueError):
            parse_ports("9000-8000")

    def test_find_local_listener(self) -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        try:
            result = scan_tcp_ports("127.0.0.1", [port], timeout=0.2, workers=1)
        finally:
            listener.close()
        self.assertEqual([item.port for item in result], [port])


if __name__ == "__main__":
    unittest.main()
