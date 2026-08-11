# Phase 2A results — passive UBox metadata capture

## Outcome

UBox did not contact the B4 at `192.168.1.30`. It used an Internet-hosted,
cloud-assisted UDP stack:

```text
HTTPS/443       control and portal connections
UDP/10240       UBox "Master Service" discovery
UDP/20001       UBox "Access Service" negotiation and sustained media-like data
UDP/43818       attempted direct P2P path through the home's public IPv4
```

The service names are not guesses: a UBox manual filed with the FCC labels
UDP/10240 as “Master Service,” UDP/20001 as “Access Service,” TCP/443 as wakeup
and portal services, and TCP/20003 as push service. See the
[FCC-hosted UBox manual](https://fcc.report/FCC-ID/2a9wqzcyt09/6876952.pdf).

## Capture boundary

| Property | Value |
| --- | --- |
| Local time | 2026-08-03 16:59–17:01 America/Toronto |
| Source | Paired iPhone through Apple `rvictl`/`rvi0` |
| File | `captures/ubox-controlled-2026-08-03.pcapng` |
| Capture length | 256 bytes per packet |
| Decoded packet records | 8,336 |
| TLS interception | None |
| UBox process packets | 505 directly attributed, plus 23 attributed DNS responses |
| Direct packets to B4 LAN IP | 0 |

The capture is Git-ignored because it contains LAN and device metadata. The raw
capture and normalized pcapng remain local. Apple PKTAP process metadata allowed
UBox traffic to be separated from iOS background services.

Integrity hashes:

```text
raw pcap:        e9e7fc923c80f90b698134fdd800a055174ef3ea6050b9ee4109e3bd6d0787b5
normalized pcapng: 296b49845c521a56e71075b6483e49cf0976fe55fcae704ea7f27f758db43728
```

## Timed behavior

| Approximate time | Action/evidence |
| --- | --- |
| 17:00:24 | UBox launch; `ubianet.com` DNS and HTTPS connections begin |
| 17:00:36 | B4 viewer requested |
| 17:00:48 | UDP master/access/P2P negotiation begins |
| 17:00:52 | Sustained inbound UDP/20001 data begins |
| 17:01:12 | Sustained inbound data ends |
| 17:01:13–17:01:23 | One 36-byte outbound keepalive per second |
| after 17:01:23 | Viewer/app teardown window |

The owner visually saw a current frame but the backyard scene was still. The
network trace adds stronger evidence: mostly 1,320-byte inbound datagrams arrived
throughout roughly 20 seconds, not as a single instantaneous download. This is
strong evidence of continuous low-bitrate media transport, though codec and
framing remain unknown.

## DNS and HTTPS control plane

UBox caused DNS lookups for:

- `m2.ubianet.com` → `121.199.12.37`
- `m4.ubianet.com` → `8.208.11.50`
- `m5.ubianet.com` → `43.134.10.68`
- `m6.ubianet.com` → `43.157.31.112`

Direct UBox-attributed TCP/443 connections included:

- `170.106.173.239`
- `43.135.106.65`
- `43.135.106.77`

These encrypted flows are currently classified only as control/portal
candidates. No plaintext HTTP API or WebSocket upgrade was observed.

## UDP master discovery

At 17:00:48 UBox sent one 60-byte request to each observed master candidate on
UDP/10240. Each returned one 152-byte response:

| IPv4 endpoint | Notes |
| --- | --- |
| `121.199.12.37:10240` | DNS name `m2.ubianet.com` |
| `8.208.11.50:10240` | DNS name `m4.ubianet.com` |
| `43.134.10.68:10240` | DNS name `m5.ubianet.com` |
| `43.153.110.207:10240` | Name not recovered in the 256-byte capture |
| `43.157.31.112:10240` | DNS name `m6.ubianet.com` |
| `175.178.248.245:10240` | Name not recovered in the 256-byte capture |

This looks like latency/availability selection across regional master servers.
Payload semantics are still unknown.

## UDP access and media path

UBox probed four access candidates on UDP/20001. Three exchanged 13 small
request/response pairs over about four seconds:

- `170.101.97.156:20001`
- `43.173.75.192:20001`
- `45.125.216.146:20001`

The selected peer was `149.56.108.231:20001`:

| Direction | Packets | UDP payload bytes | Time span |
| --- | ---: | ---: | --- |
| iPhone → peer | 150 | 6,816 | 17:00:48–17:01:23 |
| peer → iPhone | 127 | 136,795 | 17:00:48–17:01:12 |

Inbound data from 17:00:52 onward consisted primarily of 1,320-byte datagrams.
The iPhone sent small acknowledgements/control packets plus one-second
keepalives. This is the strongest media-path candidate.

## Direct P2P attempt

UBox sent six 84-byte datagrams to `174.95.106.212:43818` from 17:00:52 to
17:00:57 and received no response.

A read-only UPnP query confirmed `174.95.106.212` was the Home Hub 4000's public
IPv4 during the test. A read-only lookup for an explicit UDP/43818 UPnP mapping
returned `714 NoSuchEntryInArray`. The address/port was therefore probably a
transient NAT mapping learned through the UBox access service, not a static
mapping.

Because the iPhone and camera were on the same LAN, this was a WAN hairpin
attempt rather than direct use of `192.168.1.30`. The lack of a reply may reflect
router hairpin behavior, a stale mapping, or a protocol-level rejection; it
does not show that the camera lacked a UDP socket.

### Router correlation requests

These read-only requests were made to the Home Hub's SSDP-advertised UPnP
service; no router setting or port mapping was created or changed:

```text
GET /46663774/gatedesc0a.xml
  purpose: identify the advertised WANPPPConnection control URL

POST /46663774/upnp/control/WANPPPConn1
SOAPAction: urn:schemas-upnp-org:service:WANPPPConnection:1#GetExternalIPAddress
  result: public IPv4 matched the UBox UDP/43818 destination

POST /46663774/upnp/control/WANPPPConn1
SOAPAction: urn:schemas-upnp-org:service:WANPPPConnection:1#GetSpecificPortMappingEntry
  parameters: remote host empty, external port 43818, protocol UDP
  result: 714 NoSuchEntryInArray
```

## Current protocol model

```text
iPhone UBox
  ├─ HTTPS/443 ───────► UBox cloud control/portal
  ├─ UDP/10240 ───────► multiple Master Service candidates
  ├─ UDP/20001 ───────► multiple Access Service candidates
  │                       └─ selected peer carries sustained inbound data
  └─ UDP/43818 ───────► home public IPv4 / attempted camera P2P hairpin

B4 camera (inferred)
  └─ initiates outbound registration/NAT state to UBox infrastructure
     (camera-side packets were not visible from the iPhone capture)
```

The camera-side line is an inference from the observed P2P behavior and must be
verified at a router/access-point vantage point.

## Known

- UBox uses its documented UDP master and access services.
- No direct iPhone packet targeted the B4 LAN IP.
- UDP/20001 carried sustained media-like inbound data during viewing.
- UBox attempted a dynamic direct path through the home's public IPv4.
- The observed dynamic port was not an explicit UPnP mapping.

## Unknown

- UDP message structure, authentication, encryption, replay protection, and
  device/session identifiers.
- Whether UDP/20001 data is relayed media or access-server-wrapped P2P data.
- Camera internal UDP port and exact NAT mapping behavior.
- Codec, framing, resolution, keyframe cadence, audio, and snapshot protocol.
- Camera-originated registration, wake, notification, and firmware traffic.

## Next experiment

The preferred camera-side Raspberry Pi experiment below is deferred while the
Pi is unavailable. An offline structure analysis of this capture is complete;
see `docs/phase-2b-results-2026-08-03.md`. The immediate no-Pi experiment is a
narrow UDP-only iPhone capture with a larger snapshot length so complete media
datagrams are retained.

Observe the camera side of one controlled viewer session. The preferred setup is
the Raspberry Pi 3 connected to the Home Hub by Ethernet and configured as a
temporary Wi-Fi access point for the B4. Capture only the Pi AP interface while
repeating app launch, viewer start, and viewer stop.

This does not modify camera hardware. It requires changing/re-pairing only the
camera's Wi-Fi network. It should reveal:

1. camera DNS and master/access server selection;
2. the camera's internal UDP source/listening port;
3. the public mapping advertised to UBox;
4. whether a direct Pi-to-camera UDP path is technically possible; and
5. which packets are camera registration/control versus media.

Do not replay or synthesize captured packets until that camera-side trace is
recorded and the message boundaries are documented.
