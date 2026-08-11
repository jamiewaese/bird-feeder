# Phase 2B results — UDP framing characterization

## Outcome

The UBox UDP traffic is structured binary, not an observable RTSP, ONVIF,
HTTP, MQTT, WebSocket, RTP, or raw Annex-B video stream. The strongest current
model is a proprietary session wrapper carried over the documented UBox
Master and Access services.

The selected UDP/20001 peer carried media-sized datagrams with a stable
16-byte captured prefix and changing bytes after that prefix. UBox also sent an
identical 36-byte message approximately once per second after inbound media
ended. These are useful framing boundaries, but no field meaning, codec,
authentication method, or encryption scheme has been established.

## Method and safety boundary

The dependency-free analyzer reads the normalized pcapng locally and emits only
counts, timestamps, sizes, hashes, common-prefix lengths, and statistical
indicators. It never prints captured application bytes.

```bash
python3 -m python_tools.pcap_udp_structure \
  captures/ubox-controlled-2026-08-03.pcapng \
  --output recon-output/13-udp-structure.json

python3 -m python_tools.pcap_udp_structure \
  captures/ubox-controlled-2026-08-03.pcapng \
  --peer 149.56.108.231 \
  --output recon-output/14-udp-structure-selected-peer.json
```

The peer filter recognizes an IPv4 address embedded in the standard
`64:ff9b::/96` NAT64 prefix used in the iPhone trace.

No packet was transmitted, replayed, modified, or decrypted during this phase.

## Important capture limit

The original `tcpdump` snapshot length was 256 bytes. Apple PKTAP metadata and
network headers consumed most of that allowance, leaving at most the first 48
bytes of each UDP application payload in this capture.

The UDP header still preserves each full on-wire payload length. Therefore:

- a 36- or 40-byte payload is complete in the capture;
- only the first 48 bytes of a 60-, 68-, or 1,320-byte payload are present; and
- identical hashes for truncated payloads prove only that their retained
  prefixes match, not that their uncaptured tails match.

## Master Service on UDP/10240

| Direction | Packets | On-wire size | Captured-prefix result |
| --- | ---: | ---: | --- |
| iPhone → service | 6 | 60 bytes each | all six retained 48-byte prefixes identical |
| service → iPhone | 6 | 152 bytes each | 35 common prefix bytes; two distinct retained-prefix hashes |

The same master request template was sent nearly simultaneously to six servers.
Responses divide into two structural variants within the retained prefixes.
This supports a server-selection or discovery exchange, but the fields remain
unknown.

## Selected Access/media peer on UDP/20001

The selected peer was `149.56.108.231:20001`, represented through NAT64 in the
packet capture.

### Peer to iPhone

| On-wire payload | Packets | Retained-prefix behavior | Median spacing |
| ---: | ---: | --- | ---: |
| 1,320 bytes | 92 | 16 common bytes; 92 unique 48-byte prefixes | 0.172 s |
| 68 bytes | 13 | identical retained 48-byte prefix | 0.397 s |
| other sizes | 22 | mostly 16 common bytes within each size class | varies |

The 1,320-byte class is the main media candidate. It is sustained, nearly MTU
sized, and each retained prefix changes after a stable 16-byte region.

### iPhone to peer

| On-wire payload | Packets | Retained-prefix behavior | Median spacing |
| ---: | ---: | --- | ---: |
| 36 bytes | 32 | complete payload identical in every packet | 1.004 s |
| 40 bytes | 88 | 16 common bytes; all 88 complete payloads unique | 0.184 s |
| 60 bytes | 13 | identical retained 48-byte prefix | 0.404 s |
| 64 bytes | 11 | 16 common bytes; all 11 retained prefixes unique | 0.300 s |
| other sizes | 6 | sparse control candidates | varies |

The 36-byte class is an exact repeating keepalive candidate. The changing
40-byte class is consistent with acknowledgements, counters, or flow-control
messages, but that interpretation is not yet proven.

## Standard-format checks

Across the retained payload prefixes:

- no plausible RTP stream structure was found;
- no H.264/H.265 Annex-B start code was found; and
- no plaintext request format or recognizable standard protocol header was
  observed.

This does **not** prove encryption. A codec frame may be encapsulated after the
16-byte wrapper or begin beyond the retained 48 bytes. The payload may be
encrypted, obfuscated, authenticated, compressed, or simply encoded in a
custom binary format.

## Other fixed probes

- UDP/32762: twenty identical, complete 36-byte outbound messages and no
  observed response. The later Phase 2C capture identifies this as UBox's local
  subnet broadcast-discovery message; the Phase 2A phone path represented its
  IPv4 broadcast destination through NAT64.
- UDP/43818: six 84-byte outbound P2P probes whose retained 48-byte prefixes
  were identical, with no observed response.

## Known

- UDP/10240 uses repeatable fixed-size request/response framing.
- The selected UDP/20001 flow is sustained and packetized rather than a single
  still-image transfer.
- Media-sized inbound packets and several changing outbound classes share a
  stable 16-byte captured region.
- A complete, identical 36-byte keepalive candidate repeats approximately once
  per second.
- The retained prefixes do not look like direct RTP or raw Annex-B video.

## Unknown

- The meaning of any byte or the exact boundary between header and body.
- Whether the stable 16-byte region is a magic value, session identifier,
  device identifier, cryptographic value, or a combination of fields.
- Codec, frame boundaries, keyframes, resolution, and whether audio is present.
- Authentication, encryption, integrity checking, counters, and replay
  protection.
- Whether the Access peer is a relay or a rendezvous endpoint forwarding a
  separate camera connection.
- Camera-side registration and its local UDP socket.

## Next experiment while the Raspberry Pi is unavailable

Repeat the iPhone RVI capture with a narrow UDP-only filter and a 2,048-byte
snapshot length. This will retain complete 1,320-byte datagrams without
collecting HTTPS traffic:

```bash
sudo tcpdump -i rvi0 -nn -s 2048 -U \
  '(udp port 10240 or udp port 20001 or udp port 32762 or udp port 43818)' \
  -w captures/ubox-udp-full-2026-08-03.pcapng
```

During that short capture, mark four actions separately: open the viewer, take
one snapshot, change video quality once if the control exists, and close the
viewer. Complete payloads should let us test whether the 16-byte region is a
real wrapper, locate changing counters, and search after it for codec framing.

The Raspberry Pi access-point capture remains the following experiment because
only a camera-side vantage point can reveal registration and direct-LAN
possibilities.
