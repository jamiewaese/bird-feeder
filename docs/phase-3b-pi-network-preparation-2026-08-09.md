# Phase 3B: Raspberry Pi PRIMARY_LAN migration and power baseline

Date: 2026-08-09
Network: owner-controlled example home network
Pi: Raspberry Pi 3 Model B Rev 1.2

> Publication note: network names, addresses, hostnames, account names, and
> hardware identifiers in this document are sanitized examples.

## Objective

Place the Raspberry Pi on `PRIMARY_LAN` with the B4 camera, preserve
`FALLBACK_LAN` as a fallback, verify that SSH works without the recovery cable,
and establish a power-health baseline for the new USB adapter.

## Recovery and migration

The Pi initially used FALLBACK_LAN through `wlan0`:

| Interface | Address | Connection |
| --- | --- | --- |
| `wlan0` | `192.168.1.20/24` | `example-fallback-wifi` |
| `eth0` | `192.168.1.21/24` | Temporary recovery Ethernet |

Both addresses presented the same SSH host identity. The saved PRIMARY_LAN profile
existed but had no stored WPA secret, so an activation attempt could not
complete. The owner entered the PRIMARY_LAN password privately through NetworkManager's
interactive prompt; no password was recorded in the project.

After activation:

| Field | Result |
| --- | --- |
| Interface | `wlan0` |
| Connection | `PRIMARY_LAN` |
| Address | `192.168.1.20/24` |
| Gateway | `192.168.1.1` |
| Wi-Fi MAC | `02:00:00:00:00:20` |
| Active BSSID | `02:00:00:00:00:01` |
| Channel | 11 |
| Observed signal | 63% |

PRIMARY_LAN now has autoconnect priority `100`. FALLBACK_LAN remains available as a recovery
fallback with priority `-10`. After the Ethernet cable was removed, TCP/22 was
still reachable at `192.168.1.20`, and its SSH identity matched the same Pi.

## Power baseline

The owner changed the Pi's USB adapter and the Pi had been up for approximately
three minutes when checked.

| Check | Result |
| --- | --- |
| Firmware throttle flags, first read | `0x0` |
| Firmware throttle flags, second read | `0x0` |
| Temperature, first read | 50.5°C |
| Temperature, second read | 49.4°C |
| Reported core voltage | 1.3062 V |

`get_throttled=0x0` means the firmware reported none of the current or
since-boot under-voltage, frequency-capping, throttling, or soft-temperature
limit conditions. The reported core voltage is the regulated SoC core rail,
not a direct measurement of the USB 5 V input. The firmware flag is therefore
the relevant adapter-health result.

This is a healthy initial baseline. It cannot guarantee that the adapter will
remain adequate during every future CPU, storage, and Wi-Fi workload, so the
web service should eventually expose the throttle flags as a health metric.

## Camera reachability observation

The Pi attempted to reach the B4's previously observed PRIMARY_LAN address,
`192.168.1.30`, but neighbor resolution failed and ICMP returned destination
host unreachable. At that moment the camera could have been asleep/offline or
could have received a new DHCP address. This one negative check is not evidence
that PRIMARY_LAN isolates wireless clients.

## What we know

- The Pi is now on PRIMARY_LAN and remains reachable without Ethernet.
- PRIMARY_LAN is preferred automatically; FALLBACK_LAN is retained only as fallback.
- The new power adapter has a clean firmware health record for this boot.
- The Pi is positioned on the correct LAN for passive B4 capture and the later
  local web application.

## What remains unknown

- The B4's current DHCP address and wake state.
- Whether the Pi can observe the app's UDP/32762 discovery exchange while the
  camera is awake.
- Whether sustained storage and bird-detection load will reveal power issues.

## Next experiment

Wake the B4 with UBox, rediscover its current PRIMARY_LAN address by MAC, and make a
short SD-only capture. Start with UBox fully closed, then open TF/SD without live
view if possible, change the date once, play one known clip for 15 seconds, stop
playback, and close UBox. The Pi should passively capture UDP/32762 at the same
time. Do not replay proprietary payloads until persistent and per-session fields
are separated.
