# Phase 2C results — failed-wake control and LAN discovery

## Outcome

Two controlled UBox viewer attempts failed with “signal too weak” / “offline.”
A concurrent LAN sweep independently found no ARP entry for the B4's previous
address or MAC. This produced a useful negative control: UBox completed master
discovery, repeatedly probed four access servers, and broadcast a local
discovery message, but it never entered the sustained media state seen in the
successful Phase 2A trace.

The most important new finding is a local discovery path:

```text
iPhone 192.168.1.40:62781
    └─ UDP broadcast ─► 192.168.1.255:32762
                        36-byte fixed message, about 10 times/second
```

No response was observed because the camera was offline. This broadcast may be
the best route to direct LAN interoperability once we capture the B4's reply.

## Independent evidence that the system works

The owner supplied a UBox screenshot from 2026-08-08. It shows a cardinal in a
detailed camera frame whose on-image timestamp reads `2026-08-08 17:07:33`,
matching the phone's displayed time. The UBox status strip shows Wi-Fi, a green
battery, and external-power indicators. The screen exposes snapshot and video
buttons but places stored-media access behind “Activate Cloud Service.”

Together with the sustained Phase 2A media flow, this confirms that the camera
and live-video system function. The immediate problem is access through UBox
and its cloud-service restriction, not failed camera optics.

## Devices visible during the experiment

| Device | IPv4 | MAC | Evidence |
| --- | --- | --- | --- |
| MacBook | `192.168.1.50` | `02:00:00:00:00:50` | active `en0` configuration |
| iPhone | `192.168.1.40` | `02:00:00:00:00:40` | PKTAP packet source and ARP |
| Raspberry Pi 3 | `192.168.1.20` | `02:00:00:00:00:20` | Sanitized MAC; TCP/22 open |
| B4 camera | absent | previous `02:00:00:00:00:30` | no ARP entry after bounded `/24` sweep |

The Pi was identified but not logged into or changed.

## Capture boundary

| Property | Value |
| --- | --- |
| Local time | 2026-08-09 14:11:50–14:13:45 America/Toronto |
| Source | Paired iPhone through Apple `rvictl`/`rvi0` |
| Filter | UDP ports 10240, 20001, 32762, and 43818 only |
| Snapshot length | 2,048 bytes; all observed UDP payloads retained completely |
| Decoded UDP packets | 623 |
| Camera viewer result | offline on both attempts |
| Packets to previous camera IP | 0 |
| Media-sized packets | 0 |
| UDP/43818 P2P attempts | 0 |

Raw and normalized files remain local and Git-ignored:

```text
captures/ubox-actions-2026-08-09.pcapng
captures/ubox-actions-2026-08-09.normalized.pcapng
```

Apple `tcpdump` prepended a 24-byte classic-pcap header before the pcapng
stream. The project analyzer now recognizes this wrapper directly, while the
normalized copy preserves the inner pcapng by itself.

Integrity hashes:

```text
raw Apple capture: c8c9495e3723e2e3e63fb035e3d69aab99e5b9d2c6f8c3bfc5ed289555f47d30
normalized pcapng: 6a4117e2a74a12cd31e8d9e514ffc8eece4a1c48defc5f6d9d85c2e2e0f50fe0
```

## Two repeatable failed-wake sequences

| Stage | Attempt 1 | Attempt 2 | Payload behavior |
| --- | ---: | ---: | --- |
| UDP/10240 requests | 6 | 6 | complete 60-byte request identical across all servers and attempts |
| UDP/10240 responses | 6 | 6 | complete 152-byte responses; two structural variants |
| UDP/20001 requests | 100 over 9.57 s | 100 over 9.58 s | complete 60-byte request identical |
| UDP/20001 responses | 100 over 9.83 s | 100 over 9.77 s | complete 68-byte response identical |
| UDP/32762 broadcasts | 99 over 10.00 s | 100 over 9.97 s | complete 36-byte message identical |
| Media or direct-P2P phase | none | none | no transition |

Each access attempt contacted the same four Phase 2A candidates:

- `149.56.108.231:20001`
- `170.101.97.156:20001`
- `43.173.75.192:20001`
- `45.125.216.146:20001`

The master and access services were therefore reachable. “Offline” means UBox
could not locate or establish a session with the camera, not that the phone
lacked Internet access.

## Comparison with the successful viewer trace

| Signal | Successful Phase 2A viewer | Phase 2C offline control |
| --- | --- | --- |
| Master discovery | yes | yes, twice |
| Access probes | yes | yes, repeated for ten seconds |
| Selected access/media peer | yes | no |
| Sustained 1,320-byte packets | yes | no |
| 36-byte one-second session keepalive | yes | no |
| UDP/43818 direct-P2P probe | yes | no |
| Local UDP/32762 discovery attempt | present but routed through NAT64 | explicit LAN broadcast |

This narrows the state transition: a camera-presence or session-establishment
response must occur after master/access discovery and before media selection,
keepalive, and P2P probing.

## Known

- UBox actively searches the local subnet with UDP broadcast while it probes
  cloud access candidates.
- The full 36-, 60-, and 68-byte discovery/probe messages are static within and
  across both failed attempts.
- Cloud master/access availability alone is insufficient to start viewing.
- The camera was not associated with this LAN during the experiment.
- The Raspberry Pi is reachable at `192.168.1.20` with SSH available.

## Unknown

- Why the B4 was offline: power state, radio signal, sleep behavior, or another
  operational issue.
- Whether a live B4 replies to UDP/32762 by broadcast or unicast, and which
  source port it uses.
- Whether the fixed 36-byte broadcast embeds a device/account identifier or is
  a generic discovery message.
- Whether a local discovery response can lead to direct LAN video without a
  cloud relay.
- Snapshot and recording command framing; those buttons could not be reached
  while the camera was offline.

## Next experiment

Completed by Phase 2D; see `docs/phase-2d-local-sd-session-2026-08-09.md`.

First restore the camera to a state where UBox can display live video, without
re-pairing or changing its network configuration. Then repeat the same
full-payload UDP capture. The decisive evidence will be any response correlated
with the UDP/32762 broadcast and the first packets that differ from this
offline-control trace.

Once that live comparison exists, capture from a Raspberry Pi access-point
vantage point to observe the camera side. Do not replay the fixed discovery
message yet: it may contain a persistent account/device token even though it is
identical across attempts.
