"""Read-only protocol fingerprinting and standards-based multicast discovery."""

from __future__ import annotations

import base64
import os
import socket
import ssl
import time
import uuid
import xml.etree.ElementTree as ET

from .models import ProbeResult


HTTP_PORTS = {80, 443, 5000, 8000, 8001, 8080, 8081, 8443, 8888, 9000}
TLS_PORTS = {443, 8443, 8883}
RTSP_PORTS = {554, 7447, 8554}
MQTT_PORTS = {1883, 8883}


def _exchange(
    target: str,
    port: int,
    payload: bytes,
    *,
    timeout: float,
    use_tls: bool = False,
) -> bytes:
    raw = socket.create_connection((target, port), timeout=timeout)
    stream: socket.socket
    if use_tls:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        stream = context.wrap_socket(raw, server_hostname=target)
    else:
        stream = raw
    try:
        stream.settimeout(timeout)
        stream.sendall(payload)
        chunks: list[bytes] = []
        size = 0
        while size < 4096:
            try:
                chunk = stream.recv(min(1024, 4096 - size))
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if b"\r\n\r\n" in b"".join(chunks):
                break
        return b"".join(chunks)
    finally:
        stream.close()


def _excerpt(data: bytes) -> str | None:
    if not data:
        return None
    text = data.decode("utf-8", errors="backslashreplace")
    return text[:600].replace("\x00", "\\x00")


def probe_http(target: str, port: int, *, path: str = "/", timeout: float = 1.5) -> ProbeResult:
    tls = port in TLS_PORTS
    scheme = "https" if tls else "http"
    request = (
        f"GET {path} HTTP/1.0\r\nHost: {target}\r\n"
        "User-Agent: bird-feeder-recon/0.1\r\nConnection: close\r\n\r\n"
    ).encode("ascii")
    summary = f"GET {scheme}://{target}:{port}{path} (no credentials)"
    try:
        response = _exchange(target, port, request, timeout=timeout, use_tls=tls)
    except OSError as exc:
        return ProbeResult("HTTPS" if tls else "HTTP", port, "inconclusive", str(exc), summary)
    first_line = response.split(b"\r\n", 1)[0]
    identified = first_line.startswith(b"HTTP/")
    detail = first_line.decode("ascii", errors="replace") if first_line else "No response bytes"
    return ProbeResult(
        "HTTPS" if tls else "HTTP",
        port,
        "identified" if identified else "not-identified",
        detail,
        summary,
        _excerpt(response),
    )


def probe_onvif_path(target: str, port: int, *, timeout: float = 1.5) -> ProbeResult:
    result = probe_http(target, port, path="/onvif/device_service", timeout=timeout)
    evidence = (result.response_excerpt or "").lower()
    status = result.detail.split(" ", 2)[1] if result.detail.startswith("HTTP/") else ""
    identified = "onvif" in evidence or status in {"401", "405"}
    return ProbeResult(
        "ONVIF HTTP endpoint",
        port,
        "possible" if identified else "not-identified",
        result.detail,
        result.request_summary,
        result.response_excerpt,
    )


def probe_websocket(target: str, port: int, *, timeout: float = 1.5) -> ProbeResult:
    tls = port in TLS_PORTS
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET / HTTP/1.1\r\nHost: {target}:{port}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    ).encode("ascii")
    summary = f"WebSocket upgrade for {'wss' if tls else 'ws'}://{target}:{port}/"
    try:
        response = _exchange(target, port, request, timeout=timeout, use_tls=tls)
    except OSError as exc:
        return ProbeResult("WebSocket", port, "inconclusive", str(exc), summary)
    first_line = response.split(b"\r\n", 1)[0]
    identified = b" 101 " in first_line
    return ProbeResult(
        "WebSocket",
        port,
        "identified" if identified else "not-identified",
        first_line.decode("ascii", errors="replace") or "No response bytes",
        summary,
        _excerpt(response),
    )


def probe_rtsp(target: str, port: int, *, timeout: float = 1.5) -> ProbeResult:
    request = (
        f"OPTIONS rtsp://{target}:{port}/ RTSP/1.0\r\n"
        "CSeq: 1\r\nUser-Agent: bird-feeder-recon/0.1\r\n\r\n"
    ).encode("ascii")
    summary = f"RTSP OPTIONS rtsp://{target}:{port}/ (no credentials)"
    try:
        response = _exchange(target, port, request, timeout=timeout)
    except OSError as exc:
        return ProbeResult("RTSP", port, "inconclusive", str(exc), summary)
    first_line = response.split(b"\r\n", 1)[0]
    identified = first_line.startswith(b"RTSP/")
    return ProbeResult(
        "RTSP",
        port,
        "identified" if identified else "not-identified",
        first_line.decode("ascii", errors="replace") or "No response bytes",
        summary,
        _excerpt(response),
    )


def _mqtt_connect_packet() -> bytes:
    client_id = f"bird-recon-{uuid.uuid4().hex[:8]}".encode("ascii")
    variable_header = b"\x00\x04MQTT\x04\x02\x00\x0a"
    payload = len(client_id).to_bytes(2, "big") + client_id
    remaining = len(variable_header) + len(payload)
    return b"\x10" + bytes([remaining]) + variable_header + payload


def probe_mqtt(target: str, port: int, *, timeout: float = 1.5) -> ProbeResult:
    summary = "MQTT 3.1.1 CONNECT with a random client ID, clean session, no credentials"
    try:
        response = _exchange(
            target,
            port,
            _mqtt_connect_packet(),
            timeout=timeout,
            use_tls=port in TLS_PORTS,
        )
    except OSError as exc:
        return ProbeResult("MQTT", port, "inconclusive", str(exc), summary)
    identified = len(response) >= 4 and response[0] == 0x20 and response[1] == 0x02
    detail = f"CONNACK return code {response[3]}" if identified else "No valid CONNACK"
    return ProbeResult("MQTT", port, "identified" if identified else "not-identified", detail, summary, response.hex() or None)


def fingerprint_open_ports(
    target: str, ports: list[int], *, timeout: float = 1.5
) -> list[ProbeResult]:
    """Use conventional port mappings to avoid sending bytes to unknown services."""
    results: list[ProbeResult] = []
    for port in ports:
        if port in HTTP_PORTS:
            results.append(probe_http(target, port, timeout=timeout))
            results.append(probe_onvif_path(target, port, timeout=timeout))
            results.append(probe_websocket(target, port, timeout=timeout))
        if port in RTSP_PORTS:
            results.append(probe_rtsp(target, port, timeout=timeout))
        if port in MQTT_PORTS:
            results.append(probe_mqtt(target, port, timeout=timeout))
    return results


def _receive_multicast_responses(
    payload: bytes,
    destination: tuple[str, int],
    *,
    timeout: float,
) -> list[tuple[bytes, str]]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.2)
    responses: list[tuple[bytes, str]] = []
    try:
        sock.sendto(payload, destination)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, peer = sock.recvfrom(65535)
            except socket.timeout:
                continue
            responses.append((data, peer[0]))
    finally:
        sock.close()
    return responses


def parse_ssdp_response(data: bytes, peer: str) -> dict[str, str]:
    lines = data.decode("utf-8", errors="replace").replace("\r\n", "\n").split("\n")
    result = {"peer": peer, "status": lines[0].strip() if lines else ""}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            result[key.strip().lower()] = value.strip()
    return result


def discover_ssdp(*, timeout: float = 2.0) -> list[dict[str, str]]:
    request = (
        "M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\nMX: 1\r\nST: ssdp:all\r\n\r\n'
    ).encode("ascii")
    raw = _receive_multicast_responses(request, ("239.255.255.250", 1900), timeout=timeout)
    return [parse_ssdp_response(data, peer) for data, peer in raw]


def _first_text(root: ET.Element, suffix: str) -> str | None:
    for element in root.iter():
        if element.tag.endswith(suffix) and element.text:
            return element.text.strip()
    return None


def parse_ws_discovery_response(data: bytes, peer: str) -> dict[str, str]:
    result = {"peer": peer}
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        result["parse_error"] = str(exc)
        return result
    for key, suffix in (("endpoint", "Address"), ("types", "Types"), ("scopes", "Scopes"), ("xaddrs", "XAddrs")):
        value = _first_text(root, suffix)
        if value:
            result[key] = value
    return result


def discover_onvif(*, timeout: float = 2.0) -> list[dict[str, str]]:
    message_id = f"uuid:{uuid.uuid4()}"
    request = f'''<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
 xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
 <e:Header><w:MessageID>{message_id}</w:MessageID>
 <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
 <w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>
 <e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>
</e:Envelope>'''.encode("utf-8")
    raw = _receive_multicast_responses(request, ("239.255.255.250", 3702), timeout=timeout)
    return [parse_ws_discovery_response(data, peer) for data, peer in raw]
