# B4 observation log

Keep conclusions separate from guesses. Add a row only when its evidence file
or repeatable manual observation exists.

## Device identity

| Field | Observed value | Evidence | Confidence |
| --- | --- | --- | --- |
| Printed model | B4 | Owner-provided hardware label | High |
| Current client | UBox on iPhone | Owner observation | High |
| Wi-Fi | 2.4 GHz | Owner-provided specification | High |
| Setup transport | Bluetooth | Owner observation | Medium; behavior not captured |
| Power | 5 V USB | Owner-provided specification | High |
| Local recording | microSD slot present | Physical inspection | High; filesystem/API unknown |
| On-card media | Paired H.264/AAC MP4 clips and 640×360 JPEG snapshots | Read-only inspection of removed camera card | High |
| Card paths | `video/YYMMDD/*.mp4`, `snaps/YYMMDD/*.jpg` | 74 matching media pairs observed | High |
| Remote SD access | Confirmed through UBox's proprietary direct UDP session | Two TF thumbnails listed and one clip played from the clean card | High for capability; protocol unknown |
| Camera IPv4 address | `192.168.1.30` on PRIMARY_LAN; previously `192.168.1.30` on FALLBACK_LAN | Same known MAC after reset/re-pair | High |
| Camera MAC address | `02:00:00:00:00:30` | ARP in the same wake captures | High |
| MAC vendor prefix | Sanitized | Actual device identifier omitted from the public copy | Medium |

## Protocol status

| Candidate | Status | Evidence needed |
| --- | --- | --- |
| RTSP | Not observed | No reachable TCP port during synchronized live-view scan |
| ONVIF | Not observed | No WS-Discovery response during synchronized live-view scan |
| HTTP/HTTPS API | Not observed locally | No reachable TCP port during synchronized live-view scan |
| MQTT | Not observed locally | No reachable TCP port during synchronized live-view scan |
| WebSocket | Not observed locally | No reachable TCP port during synchronized live-view scan |
| Proprietary TCP | Not observed inbound | All 65,535 TCP ports tested during synchronized live-view scan |
| Proprietary UDP | Confirmed via UBox service stack | Master service on UDP/10240, access/media path on UDP/20001, and dynamic P2P probe |
| Local UDP discovery | Confirmed bidirectional behavior | UBox sent two 36-byte broadcasts; B4 returned two identical 408-byte responses from UDP/32762 |
| Local live/SD transport | Confirmed proprietary UDP | `192.168.1.40:49376` ⇄ `192.168.1.30:46195`; 31,954 packets during live, SD list, and playback |

## Intended replacement client

| Property | Decision |
| --- | --- |
| Host | Raspberry Pi 3 Model B Rev 1.2 at `192.168.1.20` on PRIMARY_LAN; Wi-Fi MAC `02:00:00:00:00:20` |
| User interface | Simple web app; no iPhone app planned |
| Media destination | Local 128 GB microSD attached to/used by the Pi |
| Retention rule | Save media classified as likely containing a bird |
| Detection model and threshold | Unknown until media format/rate is observed |
| Remote/cloud access | Not requested; LAN-only is the current assumption |

## Experiment log

### Experiment 1 — dormant/UBox-wake neighbor comparison

- Date/time: 2026-08-03 20:40–20:42 UTC
- Operator: owner plus Codex tooling
- Computer/interface: `en0`, `192.168.1.50`
- Subnet: `192.168.1.0/24`
- B4 power state and wake state: initially dormant; UBox live view then invoked
- Evidence files: `01-camera-dormant.json`, `wake-01.json` through `wake-12.json`
- Candidate IP/MAC: `192.168.1.30` / `02:00:00:00:00:30`
- Repeatability check: peer was absent in the dormant baseline and captures 1–7,
  then present in captures 8–12 after the UBox wake action
- Conclusion: `.57` is the B4 with high confidence
- Confounders: a different device could theoretically have joined at the same
  moment; the observed vendor prefix supported but did not prove
  the identification

### Experiment 2 — ONVIF and SSDP discovery

- Date/time: 2026-08-03 20:46–20:47 UTC synchronized retry
- B4 power/wake state: UBox live view invoked and held open by owner
- Evidence file: `09-retry-live-standard-discovery.json`
- Responding peers: router `192.168.1.1` via SSDP; no ONVIF peers
- Conclusion: no B4 ONVIF or SSDP advertisement observed during the test window

### Experiment 3 — targeted TCP scan and fingerprints

- Date/time: 2026-08-03 20:46–20:48 UTC synchronized retry
- Target IP/MAC: `192.168.1.30` / `02:00:00:00:00:30`
- Port selection: 12 repeated common-port scans, then TCP ports 1–65,535
- Evidence files: `retry-live-common-01.json` through
  `retry-live-common-12.json`, `10-retry-live-camera-all-tcp.json`
- Open ports: none
- Positive fingerprints: none; no conventional port opened for fingerprinting
- Unknown services: outbound connections and all UDP behavior
- Conclusion: no inbound TCP service was reachable during the synchronized
  UBox live-view window
- App observation: UBox displayed a current camera frame. Continuous video was
  not confirmed because the scene contained no visible motion.

## Phase 1 summary

### Known

- Hardware and desired-client facts listed above.
- The B4 is very likely `192.168.1.30` / `02:00:00:00:00:30`.
- The camera became ARP-visible roughly 30 seconds after the UBox wake action.
- UBox displayed a current frame from the camera; snapshot versus continuous
  live video was visually unresolved. Phase 2A later observed sustained
  media-like UDP packets throughout the viewing window.
- No inbound TCP port, ONVIF response, or B4 SSDP response was observed while
  UBox live view was invoked.

### Unknown

- DHCP hostname, exact sleep timeout, and local/cloud topology.
- The outbound/UDP protocol, authentication, media formats, and event paths.
- Whether local media access exists and whether the camera's own microSD is
  addressable over the LAN.

### Next experiment

Phase 2A and the offline Phase 2B framing analysis are complete; see
`docs/phase-2a-results-2026-08-03.md` and
`docs/phase-2b-results-2026-08-03.md`. While the Raspberry Pi is unavailable,
the next experiment is a narrow UDP-only iPhone capture with full 1,320-byte
payload retention and separate viewer, snapshot, quality-change, and teardown
markers. A later camera-side Raspberry Pi capture is still needed to determine
whether a direct LAN path can replace the cloud relay.

## Phase 2A — passive iPhone metadata capture

- Date/time: 2026-08-03 16:59–17:01 America/Toronto
- Capture: `captures/ubox-controlled-2026-08-03.pcapng` (Git-ignored)
- Method: Apple remote virtual interface, 256-byte snapshot length, no TLS
  interception
- Process attribution: 505 packets directly attributed to `UBox`; 23 DNS
  response packets attributed back to UBox
- Direct camera traffic: no packet to `192.168.1.30`
- UBox DNS: `m2.ubianet.com`, `m4.ubianet.com`, `m5.ubianet.com`, and
  `m6.ubianet.com`
- Master service: request/response probes to six servers on UDP/10240
- Access service: candidate probes to four servers on UDP/20001
- Selected access/media peer: `149.56.108.231:20001`
- Selected-peer traffic: 136,795 inbound UDP payload bytes over 127 packets,
  mostly 1,320-byte datagrams from 17:00:52 through 17:01:12
- Teardown: one 36-byte outbound keepalive per second through 17:01:23
- P2P attempt: six packets to the home's own public IPv4 on UDP/43818; no reply
- Router check: UDP/43818 was not an explicit UPnP port mapping
- Conclusion: UBox uses cloud-assisted UDP negotiation and a sustained UDP
  access/media path; direct LAN operation remains unobserved

## Phase 2B — offline UDP structure analysis

- Date: 2026-08-03
- Source: existing normalized Phase 2A pcapng; no new network traffic
- Analyzer: `python_tools/pcap_udp_structure.py`
- Payload exposure: none in reports; only sizes, timing, hashes, prefix lengths,
  and statistical format checks
- Capture limit: at most 48 application bytes retained per packet because the
  original snapshot length was 256 bytes
- UDP/10240: six 60-byte requests shared the same retained 48-byte prefix;
  responses were six 152-byte packets with two retained-prefix variants
- Selected UDP/20001 inbound: 92 packets of 1,320 bytes, each with a 16-byte
  common retained prefix and a unique 48-byte retained prefix
- Selected UDP/20001 outbound: 32 identical complete 36-byte messages at a
  median 1.004-second interval; likely keepalive, not yet semantically proven
- Standard framing: no plausible RTP stream or Annex-B start code in the
  retained prefixes
- Conclusion: proprietary structured binary/session wrapper is the best model;
  codec, field meanings, authentication, and encryption remain unknown

## Phase 2C — failed-wake control and LAN discovery

- Date/time: 2026-08-09 14:11:50–14:13:45 America/Toronto
- Capture: `captures/ubox-actions-2026-08-09.pcapng` (Git-ignored)
- Camera result: UBox reported weak signal/offline twice; B4 MAC/IP absent from a
  bounded LAN sweep
- Raspberry Pi: identified at `192.168.1.20`, MAC `02:00:00:00:00:20`, TCP/22
  open; no configuration change made
- Master exchange: six UDP/10240 request/response pairs per attempt
- Access exchange: 100 UDP/20001 request/response pairs per attempt over about
  ten seconds; no media-sized payload followed
- LAN discovery: 99 and 100 fixed 36-byte broadcasts to
  `192.168.1.255:32762`, approximately ten per second
- P2P/media transition: no UDP/43818 probe, selected media peer, 1,320-byte
  packet burst, or session keepalive
- Conclusion: UBox combines cloud rendezvous with explicit LAN broadcast
  discovery; a live-camera response is now the highest-value missing packet
- Full results: `docs/phase-2c-offline-control-2026-08-09.md`

## Phase 3A — physical microSD reconnaissance

- Date: 2026-08-09
- Source: camera microSD removed and mounted read-only for inspection
- Original card: repurposed 31.1 GB Raspberry Pi OS layout; camera could write
  only to its 536.9 MB FAT32 `bootfs` partition
- Inventory: 74 MP4 clips and 74 matching JPEG snapshots
- MP4: H.264, 2304×1296, approximately 15 fps, AAC mono 16 kHz
- JPEG: baseline 640×360
- Naming: dated directories plus start time, subsecond value, nominal duration,
  and an unknown `P` event flag
- Index: identical 512 KB `ubia_record.db`/`.dup` binary files; not SQLite
- Action: after owner-confirmed external backup, card repartitioned to one
  31.1 GB FAT32 `BIRDFEEDER` volume, verified, and safely ejected
- Architectural consequence: prioritize an idempotent Pi SD-media importer and
  LAN gallery, but first discover UBox's remote SD listing/download protocol
- Full results: `docs/phase-3a-sdcard-reconnaissance-2026-08-09.md`

## Phase 2D — direct LAN live view and SD playback

- Date/time: 2026-08-09 15:23:22–15:27:40 America/Toronto
- Network/camera: PRIMARY_LAN, `192.168.1.30`, original MAC retained after reset
- Discovery: two fixed 36-byte broadcasts to UDP/32762; two identical 408-byte
  B4 responses about 123 ms later
- Direct socket: iPhone `192.168.1.40:49376` to B4
  `192.168.1.30:46195`
- Direct volume: 26,968 packets / 30,839,566 payload bytes from camera; 4,986
  packets / 946,224 bytes to camera
- SD operation: two thumbnails listed on the clean card; newest clip played,
  though with poor frame rate
- Cloud comparison: only brief master/access negotiation; no sustained
  UDP/20001 media or UDP/43818 hairpin
- Payload: full capture found no RTP or Annex-B H.264 signature anywhere;
  protected proprietary framing remains
- Conclusion: direct LAN live view and remote SD playback are technically
  viable; the remaining problem is protocol/session decoding, not cloud access
- Full results: `docs/phase-2d-local-sd-session-2026-08-09.md`

## Phase 3B — Raspberry Pi PRIMARY_LAN migration and power baseline

- Date: 2026-08-09
- Hardware: Raspberry Pi 3 Model B Rev 1.2; Wi-Fi MAC
  `02:00:00:00:00:20`
- Recovery path: temporary recovery Ethernet at `192.168.1.21`; the Pi's FALLBACK_LAN
  Wi-Fi address was `192.168.1.20`
- Result: Pi joined PRIMARY_LAN as `192.168.1.20/24`, gateway `192.168.1.1`
- Wi-Fi: active PRIMARY_LAN BSSID `02:00:00:00:00:01`, channel 11, 63% signal during
  the final check
- Preference: PRIMARY_LAN autoconnect priority `100`; FALLBACK_LAN retained as a fallback at
  priority `-10`
- Validation: TCP/22 remained reachable at `192.168.1.20` after Ethernet was
  physically removed; the SSH host identity matched the Pi's earlier addresses
- New-adapter baseline: after three minutes of uptime, `get_throttled` returned
  `0x0` twice; temperatures were 50.5°C and 49.4°C
- Power conclusion: no current or since-boot under-voltage, frequency-capping,
  throttling, or soft-temperature-limit flags were set. This is a healthy
  baseline, not a guarantee under every future workload.
- Camera check: the Pi could not resolve/reach the B4's former PRIMARY_LAN address
  `192.168.1.30` during this check. The camera may have been asleep/offline or
  may have renewed its DHCP address; this does not establish client isolation.
- Full results: `docs/phase-3b-pi-network-preparation-2026-08-09.md`

## Phase 3C — idempotent importer and Raspberry Pi gallery

- Date: 2026-08-09
- Source: owner-authorized offline backup of the camera card; originals
  were read-only from the importer's perspective and were not modified
- Backup inventory: 56 MP4 videos, 74 JPEG snapshots, about 217 MB
- Layout compatibility: canonical camera `video/` plus the backup's `videos/`
  alias; both normalize to `video/` in the Pi library
- Import result: 130 objects imported, zero failures, 74 distinct capture cards
- Pairing: 56 cards contain matching video and snapshot; 18 contain a snapshot
  only
- Integrity: the staged and Pi catalogs had identical SHA-256 hashes; a full
  `rsync --checksum --dry-run` reported no media differences
- Pi paths: code at `/home/birdadmin/bird-feeder`; library and SQLite catalog at
  `/srv/bird-feeder`
- Gallery: dependency-free Python HTTP service, SQLite-backed newest-first
  cards, JPEG thumbnails, JSON API, health endpoint, and MP4 byte ranges
- Service: `bird-feeder-gallery.service`, enabled at boot and listening on
  TCP/8080
- LAN URL: `http://192.168.1.20:8080/`
- Verification: health returned 74 items; index returned HTTP 200; a real MP4
  range returned HTTP 206; browser QA loaded 640×360 thumbnails with no errors
- Power after deployment: `get_throttled=0x0`, temperature 51.5°C
- Remaining blocker: the camera does not expose `video/` or `snaps/` as a
  network filesystem. The daily job cannot be honestly enabled until a UBox SD
  source adapter can list and retrieve those objects.
- Full results: `docs/phase-3c-importer-gallery-2026-08-09.md`

## Phase 3D — starring, downloads, and dedicated watch mode

- Date: 2026-08-09
- Star scope: one camera/capture pair; stored persistently in the Pi SQLite
  catalog, shared by the home card and watch page
- Home card hierarchy: capture time in the eyebrow; date-only heading reserved
  for a future `Date: Species` presentation
- Card actions: Play video, Star/Starred toggle, and Download
- Watch mode: responsive HTML5 player with snapshot poster, Back to gallery,
  Star/Starred toggle, and Download video
- Download behavior: dedicated same-origin attachment route while normal
  playback retains MP4 byte-range support
- API: JSON star mutation plus starred, watch, and download fields in the media
  listing; cross-origin form posts are rejected by requiring JSON content type
- Desktop QA: three-column cards rendered real 640×360 thumbnails and the
  watch page loaded the real MP4 player
- Mobile QA: 390×844 home and watch layouts kept controls visible without
  horizontal overflow
- Test hygiene: the real newest capture was starred and unstarred during QA;
  the star survived a gallery-service restart, then the deployed library was
  returned to zero starred items
- Full results: `docs/phase-3d-gallery-ux-2026-08-09.md`

## Phase 3E — archive controls, deletion, and visual redesign

- Gallery cards expose capture date/timestamp, star count, classification
  state, and `is_bird` state as DOM attributes for instant local sorting and
  filtering.
- Current sort modes are newest, oldest, and most-starred. Current filters are
  has-stars, identified/unclassified, and bird/no-bird; the same model can
  accept later classifier metrics.
- Delete is pair-scoped. A confirmed request permanently removes the video and
  matching snapshot, then removes their catalog, star, and classification rows.
- Files are staged before the SQLite transaction and restored if it fails, so a
  database error does not intentionally leave a half-deleted pair.
- Full results:
  `docs/phase-3e-archive-controls-and-redesign-2026-08-09.md`

## Phase 3F — compact gallery header

- Replaced generic archive copy with the location label `Toronto`.
- Removed the promotional subheader, archive heading, and visible-result
  sentence after real-browser use showed they delayed the controls and cards.
- Desktop title and summary pills now share one compact header row; mobile keeps
  a natural stacked layout.
- Full results: `docs/phase-3f-compact-gallery-header-2026-08-09.md`

## Phase 3G — simplified identification cards

- Certainty remains stored and available through the media API but is no longer
  displayed as a `Likely` or `Uncertain` badge.
- Home cards omit Observed behavior and Species Note to keep classification
  summaries compact; those details remain on the dedicated watch page.
- Live QA confirmed the backend remains localhost-only behind the authenticated
  nginx endpoint; direct TCP/8080 stays blocked by the Pi firewall.
- Full results:
  `docs/phase-3g-simplified-identification-cards-2026-08-09.md`
