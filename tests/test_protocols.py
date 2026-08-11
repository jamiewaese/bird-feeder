from __future__ import annotations

import socket
import threading
import unittest

from camera.discovery.protocols import (
    parse_ssdp_response,
    parse_ws_discovery_response,
    probe_http,
    probe_mqtt,
    probe_rtsp,
)


def serve_once(response: bytes) -> tuple[int, threading.Thread]:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve() -> None:
        connection, _ = listener.accept()
        try:
            connection.recv(4096)
            connection.sendall(response)
        finally:
            connection.close()
            listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return port, thread


class ProtocolTests(unittest.TestCase):
    def test_http_identification(self) -> None:
        port, thread = serve_once(b"HTTP/1.0 401 Unauthorized\r\nServer: camera\r\n\r\n")
        result = probe_http("127.0.0.1", port, timeout=0.3)
        thread.join(1)
        self.assertEqual(result.outcome, "identified")

    def test_rtsp_identification(self) -> None:
        port, thread = serve_once(b"RTSP/1.0 200 OK\r\nCSeq: 1\r\n\r\n")
        result = probe_rtsp("127.0.0.1", port, timeout=0.3)
        thread.join(1)
        self.assertEqual(result.outcome, "identified")

    def test_mqtt_identification(self) -> None:
        port, thread = serve_once(b"\x20\x02\x00\x05")
        result = probe_mqtt("127.0.0.1", port, timeout=0.1)
        thread.join(1)
        self.assertEqual(result.outcome, "identified")
        self.assertEqual(result.detail, "CONNACK return code 5")

    def test_parse_ssdp(self) -> None:
        response = b"HTTP/1.1 200 OK\r\nLOCATION: http://192.168.1.2/device.xml\r\n\r\n"
        parsed = parse_ssdp_response(response, "192.168.1.2")
        self.assertEqual(parsed["location"], "http://192.168.1.2/device.xml")

    def test_parse_ws_discovery(self) -> None:
        response = b"""<Envelope xmlns:a="urn:a" xmlns:d="urn:d">
<Body><d:ProbeMatches><d:ProbeMatch><a:EndpointReference>
<a:Address>urn:uuid:test-camera</a:Address></a:EndpointReference>
<d:XAddrs>http://192.168.1.2/onvif/device_service</d:XAddrs>
</d:ProbeMatch></d:ProbeMatches></Body></Envelope>"""
        parsed = parse_ws_discovery_response(response, "192.168.1.2")
        self.assertEqual(parsed["endpoint"], "urn:uuid:test-camera")
        self.assertIn("onvif", parsed["xaddrs"])


if __name__ == "__main__":
    unittest.main()
