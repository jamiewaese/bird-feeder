# Phase 1 results — 2026-08-03

## Outcome

The B4 is identified with high confidence as:

```text
IPv4: 192.168.1.30
MAC:  02:00:00:00:00:30
OUI:  sanitized in the public copy
```

The device appeared in the ARP table only after UBox live view was invoked. It
first appeared in wake capture 8, about 32 seconds after monitoring began, and
remained present through capture 12. The only other non-router peer in the
dormant baseline was `192.168.1.60`; its stable presence makes
it an unlikely B4 candidate.

## Timeline and evidence

All timestamps below are UTC. Raw JSON is kept locally under `recon-output/`
and intentionally ignored by Git because it contains LAN identifiers.

| Time | Evidence | Result |
| --- | --- | --- |
| 20:40:34 | `01-camera-dormant.json` | Router, Mac, and `.26`; no `.57` |
| 20:41:30–20:41:58 | `wake-01.json`–`wake-07.json` | No `.57` |
| 20:42:02–20:42:21 | `wake-08.json`–`wake-12.json` | `.57` / `02:00:00:00:00:30` present |
| 20:46:18–20:46:54 | `retry-live-common-01.json`–`12.json` | No common TCP port open |
| 20:46–20:47 | `09-retry-live-standard-discovery.json` | No ONVIF; SSDP only from router |
| 20:47:33 | `10-retry-live-camera-all-tcp.json` | Zero open ports among TCP 1–65,535 |

The initial post-wake scans (`03`–`06`) were performed after the first live-view
monitor ended and could have hit a sleeping camera. They are preserved but are
not used as the strongest negative evidence. The synchronized retry is the
relevant service test.

## Protocol conclusions

| Protocol | Phase 1 result | Confidence/limit |
| --- | --- | --- |
| RTSP | Not exposed on a reachable inbound TCP port | High for tested live window; hidden/cloud-mediated RTSP remains possible |
| ONVIF | No discovery response | Moderate; an app setting or unicast-only endpoint could still exist |
| HTTP/HTTPS | No reachable inbound TCP server | High for tested live window |
| MQTT | No reachable inbound TCP broker | High; outbound MQTT or MQTT-over-WebSocket remains unknown |
| WebSocket | No reachable inbound TCP server | High; outbound cloud WebSocket remains unknown |
| Proprietary TCP | No reachable inbound listener on any port | High for tested live window; outbound connections unknown |
| UDP/P2P | Unknown | Active UDP scanning cannot classify silence; capture is required |

This pattern is consistent with—but does not yet prove—a camera that initiates
outbound cloud/P2P traffic rather than accepting LAN connections.

## Phase 1 checkpoint

Known:

- a repeatable camera-identification method;
- likely stable IP and MAC for the B4;
- no exposed inbound TCP service during the synchronized UBox test;
- no observed ONVIF or B4 SSDP discovery response.

Additionally known:

- UBox displayed a current camera frame during the synchronized retry.
- The later Phase 2A capture observed sustained media-like UDP datagrams during
  the viewer window, strongly supporting continuous live transport.

Unknown:

- DNS names, cloud/P2P endpoints, UDP ports, and connection direction;
- authentication/pairing tokens and cryptographic transport;
- video codec/container and snapshot/event mechanisms;
- camera microSD access and firmware-update path.

## Next experiment

Phase 2 should begin with a passive, metadata-only capture of five clearly timed
actions:

1. UBox app launch with no live view.
2. Live-view start and 30 seconds of video.
3. Controlled visible motion in front of the camera.
4. One snapshot action.
5. Live-view stop and app close.

Record DNS queries, peer IPs, ports, TCP/UDP, start/stop times, and byte counts.
Do not begin with TLS interception. The selected first vantage point is Apple's
USB remote virtual interface for the paired iPhone, documented in
`docs/phase-2-iphone-capture.md`.

This experiment was completed on 2026-08-03. Results are in
`docs/phase-2a-results-2026-08-03.md`.
