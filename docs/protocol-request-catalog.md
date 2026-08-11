# Phase 1 protocol request catalog

This catalog documents every packet class the reconnaissance CLI intentionally
originates. Response excerpts and exact outcomes are preserved in JSON reports.

## `local` and `neighbors`

`local` reads operating-system interface configuration. `neighbors` reads the
operating-system ARP/neighbor cache. Neither command sends an application
request to the camera.

## `sweep`

For every usable address in the explicitly supplied local subnet:

```text
transport: UDP
destination port: 9 (discard)
payload: 00 (one byte)
expected application response: none
```

The datagram causes the operating system to perform normal ARP resolution. The
tool then reads the neighbor cache. It does **not** label UDP/9 open.

## `discover`: ONVIF WS-Discovery

```text
transport: UDP multicast
destination: 239.255.255.250:3702
action: http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe
type: dn:NetworkVideoTransmitter
message ID: a new random UUID for each request
```

The SOAP XML contains no username, password, token, device mutation, or stream
request.

## `discover`: SSDP

```http
M-SEARCH * HTTP/1.1
HOST: 239.255.255.250:1900
MAN: "ssdp:discover"
MX: 1
ST: ssdp:all
```

## `scan`: TCP connect

The scanner completes the operating system's normal TCP connection setup to
each selected port and closes successful connections. A scan with
`--no-fingerprint` sends no application bytes.

## `scan`: HTTP/HTTPS

Only conventional web ports are fingerprinted:

```http
GET / HTTP/1.0
Host: CAMERA_IP
User-Agent: bird-feeder-recon/0.1
Connection: close
```

The same request is sent to `/onvif/device_service`. TLS certificate validation
is disabled for identification because a LAN camera commonly has a self-signed
certificate. No certificate is trusted or persisted as a result.

## `scan`: WebSocket

Only conventional web ports are tried. The tool sends an RFC 6455 upgrade for
`/` with a fresh random `Sec-WebSocket-Key`. It sends no WebSocket frames after
the handshake.

## `scan`: RTSP

Only ports 554, 7447, and 8554 are tried:

```text
OPTIONS rtsp://CAMERA_IP:PORT/ RTSP/1.0
CSeq: 1
User-Agent: bird-feeder-recon/0.1
```

No media path, credentials, `DESCRIBE`, `SETUP`, or `PLAY` request is sent.

## `scan`: MQTT

Only ports 1883 and 8883 are tried. The request is MQTT 3.1.1 `CONNECT` with:

- a new `bird-recon-XXXXXXXX` client ID;
- clean-session flag set;
- 10-second keepalive; and
- no username, password, will, subscription, or publication.

A valid `CONNACK`, including “not authorized,” identifies MQTT. Port 8883 uses
TLS without trusting or retaining the presented certificate.

## Explicitly absent

Phase 1 does not send Bluetooth commands, UBox requests, login attempts,
credential guesses, firmware requests, ONVIF SOAP operations beyond discovery,
RTSP playback, MQTT subscriptions/publications, generic payloads to unknown
ports, or exhaustive UDP probes.

## Observed UBox local discovery (passive only)

Phase 2C/2D observed the official UBox app originate this separate packet
class. The project tools have **not** replayed it:

```text
transport: UDP broadcast
destination: local-subnet broadcast address, port 32762
payload length: 36 bytes
payload stability: identical within all observed attempts
```

When the B4 was offline, UBox repeated the message approximately ten times per
second for ten seconds and received no reply. When the B4 was online on PRIMARY_LAN,
UBox sent it twice and received two identical 408-byte responses from the
camera's UDP/32762 socket about 123 ms later.

The payloads are deliberately not published because the fixed request may
contain a persistent account or device identifier. No field semantics or
authentication properties have been established.
