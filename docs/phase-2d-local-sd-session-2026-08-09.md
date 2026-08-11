# Phase 2D results — direct LAN live view and SD playback

## Outcome

Direct LAN operation is confirmed. On the stable PRIMARY_LAN network, UBox discovered
the B4 by UDP broadcast, received a camera response in approximately 123 ms,
and used one high-volume UDP socket directly between the iPhone and camera for
live view, SD-card listing, and recorded-video playback.

```text
UBox/iPhone 192.168.1.40:49376
    ├─ UDP broadcast ─► 192.168.1.255:32762
    │                     36-byte discovery message, sent twice
    │
    ◄─ UDP response ─── B4 192.168.1.30:32762
    │                     408-byte response, sent twice
    │
    └─ bidirectional UDP ─ B4 192.168.1.30:46195
                           live view, control, SD list, SD playback
```

The camera kept its known MAC address, `02:00:00:00:00:30`, after reset and
re-pairing. No local RTSP, ONVIF, HTTP, MQTT, WebSocket, SMB, or FTP endpoint was
observed. “Connect to the camera root folder” should therefore be treated as a
user-facing goal, not as the current protocol model: the observed route is a
proprietary direct UDP media/index protocol.

## Controlled actions

| Approximate local marker | Action/result |
| --- | --- |
| 15:23:37 | baseline marker; UBox was on/near its device view |
| 15:23:45 | master, access, and LAN discovery exchange |
| 15:23:56 | owner confirmed visibly updating live video |
| 15:24:17 | manual snapshot control pressed; no error reported |
| 15:24:42 | manual video recording started |
| 15:25:21 | manual video recording stopped |
| 15:26:29 | TF/SD view showed two recording thumbnails |
| 15:27:20–15:27:35 | newest SD recording played with poor frame rate |
| 15:27:40 | teardown/capture end |

User-response markers follow each physical action by an unknown small delay.
The direct socket was already active at capture start, probably because UBox
retained or prefetched a local session from the device screen. This prevents a
clean first-session boundary but does not affect the direct-LAN conclusion.

## Capture boundary

| Property | Value |
| --- | --- |
| Local time | 2026-08-09 15:23:22–15:27:40 America/Toronto |
| Camera | `192.168.1.30`, PRIMARY_LAN |
| Capture source | iPhone through Apple `rvi0`/PKTAP |
| Filter | known UBox UDP ports plus all traffic involving the camera IP |
| Snapshot length | 4,096 bytes; all observed UDP payloads retained |
| File | `captures/ubox-live-sd-actions-2026-08-09.pcapng` |
| File size | approximately 34 MB |
| Decoded UDP packets | 31,978 |
| Direct camera UDP packets | 31,954 |
| TLS interception | none |

Capture SHA-256:

```text
c735b96911685e280cc2db725199dc5f88b2edfbafa7e6602f5b02abebfca7db
```

The file contains Apple's 24-byte classic-pcap stub before its pcapng stream.
The project parser recognizes that wrapper directly.

## Cloud negotiation versus local selection

At 15:23:45 UBox performed its normal cloud discovery, but it did not select a
cloud media relay:

- six UDP/10240 master requests and six responses;
- four fixed UDP/20001 access probes and four responses;
- one additional 124-byte/468-byte access exchange;
- no sustained UDP/20001 media burst; and
- no UDP/43818 public-address hairpin attempt.

In parallel, UBox sent two identical 36-byte broadcasts to
`192.168.1.255:32762`. The B4 returned two identical 408-byte responses from
`192.168.1.30:32762` to the iPhone's source port. The first reply followed the
first broadcast by about 123 ms.

The 408-byte body does not contain the camera/phone IPv4 addresses or the later
UDP port numbers in straightforward big- or little-endian form. Its field
format remains opaque.

## Direct socket

| Direction | Packets | UDP payload bytes |
| --- | ---: | ---: |
| B4 → iPhone | 26,968 | 30,839,566 |
| iPhone → B4 | 4,986 | 946,224 |

The main direct flow was:

```text
192.168.1.40:49376 ⇄ 192.168.1.30:46195
```

The camera sent 16,583 datagrams with 1,320-byte payloads. Other common inbound
sizes included 1,200, 1,280, 312, 608, and 904 bytes. The iPhone returned
smaller acknowledgement/control classes led by 40-, 64-, 88-, and 112-byte
payloads.

Both directions included 36-byte messages at approximately one-second
intervals. Each direction had two full-payload variants, consistent with
session keepalive/state messages.

## Payload format

Complete direct-camera payloads showed:

- no plausible RTP stream;
- no H.264/H.265 Annex-B start code at the beginning or anywhere inside any of
  the 26,966 main camera-to-iPhone payloads;
- approximately 7.92 bits/byte aggregate entropy in the inbound stream; and
- unique contents for every observed 1,320-byte datagram.

Physical SD inspection proves the saved video is H.264/AAC, but the direct UDP
stream does not expose raw H.264. It is very likely encrypted or obfuscated
inside a proprietary wrapper, though the cryptographic construction and keys
remain unknown.

## Action comparison

Manual snapshot and manual recording caused no new cloud master/access phase.
They may be client-side operations on the already received live stream; the
encrypted local flow prevents a definitive command-level attribution from this
mixed capture.

The TF/SD screen listed two recordings created on the newly formatted card.
Opening the newest recording produced visible playback with poor frame rate.
During representative windows:

| Mode | B4 → iPhone payload rate |
| --- | ---: |
| Live-view window | approximately 138 KB/s |
| TF/SD list window | approximately 177 KB/s |
| Recorded playback window | approximately 253 KB/s |

No sustained cloud UDP media flow appeared during listing or playback. The
traffic remained on the direct B4 socket, making a LAN-only Pi downloader
technically plausible.

## Reproducible safe reports

These reports contain flow metadata and structural statistics, never raw
application bytes:

```bash
python3 -m python_tools.pcap_udp_structure \
  captures/ubox-live-sd-actions-2026-08-09.pcapng \
  --ports 46195 --peer 192.168.1.30 \
  --output recon-output/17-local-camera-udp-structure.json

python3 -m python_tools.pcap_host_timeline \
  captures/ubox-live-sd-actions-2026-08-09.pcapng \
  --host 192.168.1.30 --bin-seconds 5 \
  --output recon-output/18-local-camera-timeline.json
```

## Known

- UBox and the B4 have a working direct-LAN UDP discovery and media path.
- The camera responds to UDP/32762 broadcast when awake and reachable.
- The local session carries live view, SD index thumbnails, and SD playback.
- UBox can list and play recordings from the clean card without an active cloud
  storage subscription.
- Saved media is standard MP4/H.264/AAC, but transported media is opaque.

## Unknown

- Discovery-response fields and authentication material.
- How the direct UDP session derives its dynamic camera port and session keys.
- Exact commands for SD date/index listing, thumbnail retrieval, clip playback,
  seeking, and stopping.
- Whether a complete MP4 can be requested as a file or must be reconstructed
  from the protected playback stream.
- Whether snapshot/manual-record controls are purely iPhone-side operations.

## Next experiment

The Raspberry Pi was moved from FALLBACK_LAN to PRIMARY_LAN and wireless SSH was verified on
2026-08-09; see `docs/phase-3b-pi-network-preparation-2026-08-09.md`. The next
experiment is one shorter iPhone capture starting with UBox fully closed and
containing only:

1. launch UBox;
2. open TF/SD playback without first opening live view, if the UI permits;
3. change the SD date once;
4. open one known clip for 15 seconds; and
5. stop playback and close UBox.

This should separate discovery, SD index, playback start, and playback stop
messages. The Pi should passively capture the UDP/32762 broadcast at the same
time. Do not replay the discovery/session payloads until their persistent and
per-session fields are understood.
