# Phase 1 reconnaissance

## Purpose and stopping point

This phase answers two questions before any app code is designed:

1. Which LAN address belongs to the powered B4 camera?
2. Does that address expose an identifiable standard service?

The first experiment ends after recording its MAC address, open TCP ports, and
responses to ONVIF/SSDP/HTTP/RTSP/WebSocket/MQTT probes. It does not attempt
login, password guessing, firmware extraction, stream-path guessing, Bluetooth
pairing, packet interception, or UBox analysis.

## What each observation can prove

| Observation | Supported conclusion | Not yet proved |
| --- | --- | --- |
| New IP/MAC appears only while B4 is powered | Strong camera identity candidate | Vendor/model identity |
| TCP connection succeeds | Port is open | Which application protocol it uses |
| Response starts `RTSP/` | RTSP server identified | Stream path, codec, or credentials |
| Valid MQTT `CONNACK` | MQTT broker identified | Topics or authorization model |
| HTTP response | HTTP server identified | API existence or endpoint semantics |
| HTTP `101` to upgrade | WebSocket at `/` identified | Message schema |
| WS-Discovery response with ONVIF type/XAddr | Strong ONVIF evidence | Supported profiles and credentials |
| No response | Nothing positive | Closed vs filtered vs sleeping device |

An open nonstandard TCP port is evidence of an unknown service, not proof that
it is proprietary. Proprietary UDP cannot be ruled in or out from a silent
active scan; Phase 2 traffic capture is the appropriate way to identify it.

## Experiment 1: identify the camera

Keep the Mac/Linux computer and B4 on the same 2.4 GHz-capable LAN. Guest Wi-Fi
or wireless client isolation can prevent peers from seeing one another, so use
the normal trusted LAN if possible.

1. With the B4 powered **off**, show the computer's interface and subnet:

   ```bash
   python3 -m python_tools.recon local --human
   ```

2. Substitute the reported subnet exactly and capture a powered-off baseline:

   ```bash
   mkdir -p recon-output
   python3 -m python_tools.recon sweep \
     --subnet 192.168.1.0/24 \
     --output recon-output/01-camera-off.json
   ```

3. Power on the camera, wait two minutes for Wi-Fi association, then repeat:

   ```bash
   python3 -m python_tools.recon sweep \
     --subnet 192.168.1.0/24 \
     --output recon-output/02-camera-on.json
   ```

4. Compare resolved IP/MAC pairs without timestamp noise:

   ```bash
   python3 -m python_tools.recon compare-neighbors \
     recon-output/01-camera-off.json \
     recon-output/02-camera-on.json \
     --human
   ```

Look for a new IP/MAC under `Appeared`. If several entries appear, power the B4
off, wait two minutes, run a third sweep, and select the entry that disappears.
The router's DHCP-client page is a useful independent cross-check. Do not rely
only on a vendor name inferred from the MAC prefix, because camera vendors often
source network modules from other manufacturers.

Record the candidate IP and MAC in `docs/observations.md` before continuing.

## Experiment 2: standard discovery

With the B4 powered and awake, send the two normal LAN discovery requests:

```bash
python3 -m python_tools.recon discover \
  --output recon-output/03-standard-discovery.json
```

This sends ONVIF WS-Discovery and SSDP M-SEARCH only. A valid response is strong
positive evidence. No response does not disprove ONVIF or HTTP because multicast
may be blocked and some cameras expose ONVIF only after a setting is enabled.

## Experiment 3: scan the candidate IP

Replace the example with the candidate identified in Experiment 1:

```bash
python3 -m python_tools.recon scan 192.168.1.30 \
  --output recon-output/04-common-ports.json
```

The default scan checks the ports listed in `camera/discovery/ports.py`. On open
conventional ports it performs these bounded fingerprints:

- HTTP/HTTPS `GET /`
- HTTP/HTTPS `GET /onvif/device_service`
- WebSocket upgrade at `/`
- RTSP `OPTIONS` at `/`
- MQTT 3.1.1 clean-session connection with no username or password

Use `--no-fingerprint` if only TCP connections should be attempted. A broader
scan is a separate, explicit experiment:

```bash
python3 -m python_tools.recon scan 192.168.1.30 \
  --ports 1-1024,1883,5000,7447,8000-9000,10000,34567,37777,49152 \
  --no-fingerprint \
  --output recon-output/05-expanded-tcp.json
```

Only if that remains empty and the camera is confirmed awake should all TCP
ports be tested:

```bash
python3 -m python_tools.recon scan 192.168.1.30 \
  --all-ports --no-fingerprint \
  --output recon-output/06-all-tcp.json
```

A full scan can take time and may be logged by the device. It is still a TCP
connect scan—not an exploit scan—and never targets more than the one explicit
local address.

## If the camera does not appear

Check these in order:

1. Verify the official app can reach the B4 while the phone is on the same LAN.
2. Confirm the computer is not on guest Wi-Fi, a VPN-only route, or a different
   VLAN.
3. Check the router's DHCP lease list for a new client while power-cycling the
   B4.
4. Wake the camera using its normal button/app action, then repeat the scan
   immediately. A battery-conscious camera may sleep even when USB-powered.
5. Preserve the negative result. Sleep or cloud-only behavior is itself useful
   evidence, but it does not identify a protocol.

## Raspberry Pi implications (not yet implementation)

The Pi 3 target makes several future boundaries important:

- **Ingest:** preserve the camera's original H.264/H.265/JPEG bytes where the
  browser and storage design allow it; avoid continuous transcoding on the Pi.
- **Detection:** sample frames at a low, configurable rate and keep the model
  replaceable. A confidence threshold must be visible in the web UI.
- **Storage:** media files and metadata remain separate. The storage root,
  reserved free space, retention limit, and graceful behavior on a missing or
  read-only card must all be configurable.
- **Web:** bind to the LAN intentionally and require authentication before the
  app is treated as complete. Camera credentials must not be sent to browsers.
- **Testing:** discovery, camera transport, detection, event selection, storage,
  and HTTP presentation get independent interfaces and fixtures.

The actual codec, snapshot mechanism, event cadence, and whether the B4 exposes
SD-card contents remain unknown, so choosing a web video player or bird model
now would be premature.

## Phase 1 exit criteria

Phase 1 is complete when the experiment log contains:

- a repeatable IP/MAC identification method;
- the camera's candidate IP and MAC;
- complete JSON output from standard discovery and the selected TCP scan;
- positive protocol evidence, or a clearly recorded negative result; and
- one justified next experiment for Phase 2.
