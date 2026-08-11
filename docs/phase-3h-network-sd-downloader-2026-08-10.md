# Phase 3H: nightly network SD downloader (2026-08-10)

## Outcome

The Raspberry Pi can retrieve recordings from the owned B4/UBox camera's
microSD card over the local network. A live proof established a direct camera
session, listed 27 recent events, requested one event's file metadata, received
all of its data chunks, and wrote a valid 983,040-byte ISO MP4.

This removes the weekly physical-card-copy requirement. The production job runs
each night, downloads both snapshots and videos, and feeds them through the
existing idempotent gallery importer.

The deployed production path also completed a 1,769,472-byte MP4 after backward
missing-chunk recovery and a 1,966,080-byte MP4 after one automatic fresh-session
retry. `file(1)` identified both as ISO Media MP4 v2; paired downloads were
identified as baseline 640×360 JPEGs. The enabled timer's first scheduled run is
2026-08-10 at 02:38 EDT (the 02:30 schedule plus randomized delay).

## Read-only protocol

The implementation uses the same read-only SD playback messages as UBox:

- `256` requests the event list; `257` returns fixed-size event records.
- `260` requests metadata for an event's MP4 or JPEG; `261` returns its
  canonical filename and byte length.
- `262` requests file chunks; `263` returns chunks addressed by byte offset.

Chunks are normally 1,200 bytes. The downloader records each received segment,
validates its offset and exact expected length, and asks for missing data again
before accepting a file. It matches the official client's two recovery modes:
backward recovery from the highest missing offset after a full-size chunk, and
an explicit map of up to 600 indices after a short final chunk. A completed file
is written to a temporary name, synced, and renamed atomically.

The vendor's Android arm64 transport is used only for session establishment and
message delivery. `camera/ubox_native/android_compat.c` provides the small,
auditable Bionic/glibc ABI bridge needed on 64-bit Raspberry Pi OS. The
downloader's own event parsing, bounds checks, path validation, retry logic,
and file writes are implemented in `camera/ubox_native/ubox_connect.c`.

## Import and scheduling

`python_tools.download_ubox` creates a private temporary source tree beneath
`/srv/bird-feeder/incoming`, invokes the network downloader, and then runs the
existing `MediaImporter`. The temporary duplicate is removed after import.
Files already present in `/srv/bird-feeder/media/yard` with the expected size
are not transferred again.

The systemd timer runs at 02:30 local time with up to ten minutes of randomized
delay. `Persistent=true` means a missed run starts after the Pi next boots. Each
run requests a 36-hour window, providing overlap between nights without
duplicating already-imported media.

The downloader unit uses an `ExecStopPost` hook to prepare phone-compatible
share copies of all catalogued videos. Unlike `ExecStartPost`, this still runs
when an individual UBox transfer is incomplete and the main download command
reports failure, so previously imported clips do not remain stuck without a
compatible share copy.

Bird classification follows at 06:00 with up to fifteen minutes of randomized
delay. Its service is ordered after the downloader, so it waits if retrieval is
still in progress. It attempts all newly imported paired snapshots in the batch
(up to 100), with persistent scheduling after outages and the existing hard
$10/month estimated-spend ceiling.

Relevant units:

- `bird-feeder-ubox-download.service`
- `bird-feeder-ubox-download.timer`

The camera firmware is most reliable when each authenticated session retrieves
one file. The Python runner therefore opens a fresh bounded native session for
each event's JPEG and MP4, retries a failed file session up to three times, and
imports the accumulated staging tree after the batch. This is slower than one
large session but prevents one stalled transfer from poisoning every later
request.

The service has its own unprivileged account, receives only the camera
credentials in `/etc/bird-feeder/ubox.env`, and can write only within the media
library. The credentials file must be owned by `root:uboxdownloader` with mode
`0640`; it must never be committed to this repository.

## Operational checks

```bash
systemctl list-timers bird-feeder-ubox-download.timer
sudo systemctl start bird-feeder-ubox-download.service
sudo journalctl -u bird-feeder-ubox-download.service -n 100 --no-pager
```

Successful logs end with both a native download summary and an importer JSON
summary. Re-running the service should report recent files as already present.
