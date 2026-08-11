# Phase 2A — passive iPhone traffic metadata

## Purpose

The first Phase 2 experiment will observe the official UBox client's network
connections without decrypting them. It should answer:

- Does UBox talk directly to `192.168.1.30`, to Internet services, or both?
- Does the app use TCP, UDP/QUIC, or a mixture?
- Which DNS names and destination ports correlate with camera wake and viewing?
- Does sustained traffic follow visible motion, suggesting continuous video?

It will not reveal credentials, plaintext API messages, video codec, or packet
contents protected by TLS. Those are later questions and may not require
decryption if endpoint and protocol behavior reveals an interoperability path.

## Selected capture method

Connect the paired iPhone to this Mac by USB and use Apple's remote virtual
interface (`rvictl`). This captures the iPhone's packets at the Mac without Wi-Fi
monitor mode, router modification, camera modification, or TLS interception.

Prerequisites verified on 2026-08-03:

```text
rvictl:  /Library/Apple/usr/bin/rvictl
xctrace: /Applications/Xcode.app/Contents/Developer/usr/bin/xctrace
tcpdump: /usr/sbin/tcpdump
iPhone:  paired, currently offline
```

The paired-device identifier is intentionally omitted from tracked
documentation. It can be read locally from `xcrun xctrace list devices` after
the phone is connected.

## Privacy boundary

- Close unrelated iPhone apps before capture.
- Keep the capture under two minutes.
- Keep captures under the Git-ignored local `captures/` directory.
- Do not install a CA certificate, configure an HTTPS proxy, or attempt TLS
  interception.
- Use a 256-byte snapshot length: enough for packet headers, DNS, and many TLS
  handshakes, while avoiding full encrypted payload retention.
- Review endpoints before sharing any result outside this workspace.

## Controlled action script

Once the phone is online and capture is running, record these markers using the
Mac's clock:

| Offset | Action |
| --- | --- |
| 0–15 s | UBox fully closed; baseline iPhone traffic |
| 15 s | Launch UBox but do not open the camera |
| 30 s | Open the B4 viewer |
| 45 s | Create unmistakable motion in frame for 10 seconds |
| 60 s | Stop moving; observe whether the displayed frame changed |
| 70 s | Use UBox's snapshot action once, if available |
| 80 s | Exit viewer and fully close UBox |
| 95 s | Stop capture |

The motion can be a person waving or a large object moving across the field of
view. Do not infer continuous video merely from a current-looking still scene.

## Capture commands

These commands are documented for reproducibility. Codex can run them after the
iPhone is connected, unlocked, and trusted:

```bash
xcrun xctrace list devices
sudo rvictl -s PHONE_UDID
sudo tcpdump -i rvi0 -nn -s 256 -U \
  -w captures/ubox-controlled-2026-08-03.pcap
sudo rvictl -x PHONE_UDID
```

Stop `tcpdump` with Control-C at the 95-second marker. The cleanup command must
run even if capture fails, so the temporary `rvi0` interface does not linger.

Do not run these commands until `xctrace` lists the iPhone under `Devices`, not
`Devices Offline`.

## First-pass analysis

The initial pass will extract only:

- timestamp;
- source and destination IP/port;
- TCP versus UDP;
- DNS query/answer names;
- TLS SNI when visible;
- packet and captured-byte count per flow; and
- which flows begin or materially change near each action marker.

Candidate camera traffic is traffic involving `192.168.1.30`. Internet peers
that begin only after the 30-second viewer marker are candidate cloud/P2P
services, not automatically camera services. Repeating the experiment is
required before attributing a shared CDN endpoint to UBox.

## Stop condition

Phase 2A ends with a flow table and one of these outcomes:

1. direct LAN flow to `.57` identified;
2. cloud/relay flow strongly correlated with viewing;
3. UDP/P2P negotiation followed by direct media traffic;
4. no attributable flow, requiring a Raspberry Pi access-point/router capture.

## Completed capture note

The experiment ran on 2026-08-03. The first permission-denied `tcpdump` attempt
left a 24-byte classic-pcap header before the successful pcapng stream. The raw
file was preserved, and a normalized copy was made by removing exactly those
24 bytes:

```text
raw:        captures/ubox-controlled-2026-08-03.pcap
normalized: captures/ubox-controlled-2026-08-03.pcapng
```

Apple process metadata identified UBox traffic without decrypting it. See
`docs/phase-2a-results-2026-08-03.md`.
